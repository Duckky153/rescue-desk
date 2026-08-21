import hashlib
import json
import os
import shutil
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Never

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from rescue_desk.auth import Principal
from rescue_desk.config import get_settings
from rescue_desk.database import register_rollback_path
from rescue_desk.domain.hashing import hash_payload, sha256_text
from rescue_desk.extraction import (
    ClauseExtractionResult,
    ExtractedAssertion,
    PdfSafetyError,
    deterministic_extract,
    extract_pdf_pages,
    extract_with_ollama,
    validate_pdf_bytes,
)
from rescue_desk.idempotency import store_response
from rescue_desk.models import (
    AssertionReviewState,
    AssertionSource,
    CaseRevision,
    CaseStatus,
    ContractAssertion,
    ContractDocument,
    DocumentPage,
    EvidenceSpan,
    ExtractionRun,
    ExtractionRunStatus,
    FeeObligation,
    FindingSeverity,
    ProcessingStatus,
    ReadinessFinding,
    RescueCase,
    ReviewDecision,
    SafetyStatus,
)
from rescue_desk.schemas import DocumentSupersedeRequest
from rescue_desk.services.audit import append_audit_event
from rescue_desk.services.cases import (
    current_revision,
    get_case,
    require_evidence_mutable,
)
from rescue_desk.services.evidence_governance import review_governance
from rescue_desk.services.lifecycle import retire_revision_exports

CANONICAL_KEYS = {
    "contract.effective_date": "contract_start_date",
    "contract.initial_term_end_date": "contract_end_date",
    "contract.expiration_date": "contract_end_date",
    "renewal.notice_deadline": "notice_deadline",
    "renewal.auto_renews": "auto_renewal",
}


@dataclass(frozen=True, slots=True)
class ProcessingIdempotency:
    endpoint: str
    key: str
    request_hash: str


class _ProcessingUnitFailure(RuntimeError):
    def __init__(self, error: Exception) -> None:
        super().__init__(str(error))
        self.error = error


def _safe_storage_path(stored_filename: str) -> Path:
    settings = get_settings()
    if Path(stored_filename).name != stored_filename:
        raise RuntimeError("Unsafe stored filename")
    root = settings.upload_dir.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    candidate = root / stored_filename
    if candidate.is_symlink():
        raise RuntimeError("Document storage path cannot be a symbolic link")
    path = candidate.resolve(strict=False)
    if path.parent != root:
        raise RuntimeError("Unsafe document storage path")
    return path


def _write_private_file(path: Path, data: bytes) -> None:
    try:
        with path.open("xb") as handle:
            os.chmod(path, 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _copy_private_file(source: Path, target: Path) -> None:
    try:
        with source.open("rb") as source_handle, target.open("xb") as target_handle:
            os.chmod(target, 0o600)
            shutil.copyfileobj(source_handle, target_handle)
            target_handle.flush()
            os.fsync(target_handle.fileno())
    except Exception:
        target.unlink(missing_ok=True)
        raise


def verify_document_integrity(document: ContractDocument) -> Path:
    """Return the safe path only when stored bytes match the intake record."""

    path = _safe_storage_path(document.stored_filename)
    if not path.is_file():
        raise RuntimeError("Document bytes are missing from runtime storage")
    data = path.read_bytes()
    if len(data) != document.size_bytes or hashlib.sha256(data).hexdigest() != document.sha256:
        raise RuntimeError("Document bytes failed their recorded SHA-256 integrity check")
    return path


def get_document(db: Session, principal: Principal, document_id: str) -> ContractDocument:
    document = db.scalar(
        select(ContractDocument).where(
            ContractDocument.id == document_id,
            ContractDocument.organization_id == principal.organization_id,
        )
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return document


def _document_case_id(db: Session, principal: Principal, document_id: str) -> str | None:
    return db.scalar(
        select(RescueCase.id)
        .join(CaseRevision, CaseRevision.case_id == RescueCase.id)
        .join(ContractDocument, ContractDocument.case_revision_id == CaseRevision.id)
        .where(
            ContractDocument.id == document_id,
            RescueCase.organization_id == principal.organization_id,
        )
    )


def list_documents(db: Session, *, principal: Principal, case_id: str) -> list[ContractDocument]:
    case = get_case(db, principal, case_id)
    revision = current_revision(case)
    return list(
        db.scalars(
            select(ContractDocument)
            .where(ContractDocument.case_revision_id == revision.id)
            .order_by(ContractDocument.created_at, ContractDocument.id)
        )
    )


def clone_current_revision(
    db: Session,
    *,
    case: RescueCase,
    principal: Principal,
    reason: str,
) -> CaseRevision:
    old = current_revision(case)
    new = CaseRevision(
        case=case,
        number=old.number + 1,
        reason=reason,
        previous_revision_id=old.id,
        created_by=principal.user_id,
    )
    db.add(new)
    db.flush()
    # A revision is an immutable snapshot. Clone every source (including locally
    # superseded ones) so assertion evidence keeps complete provenance, and never
    # repurpose flags on the frozen source revision as cross-revision state.
    old_documents = list(
        db.scalars(
            select(ContractDocument)
            .options(
                selectinload(ContractDocument.pages).selectinload(DocumentPage.evidence_spans),
                selectinload(ContractDocument.extraction_runs).selectinload(
                    ExtractionRun.assertions
                ),
            )
            .where(ContractDocument.case_revision_id == old.id)
            .order_by(ContractDocument.created_at, ContractDocument.id)
        )
    )
    evidence_map: dict[str, EvidenceSpan] = {}
    run_map: dict[str, ExtractionRun] = {}
    for document in old_documents:
        cloned_filename = f"{uuid.uuid4()}.pdf"
        source_path = verify_document_integrity(document)
        target_path = _safe_storage_path(cloned_filename)
        _copy_private_file(source_path, target_path)
        register_rollback_path(db, target_path)
        cloned_document = ContractDocument(
            organization_id=document.organization_id,
            case_revision_id=new.id,
            original_filename=document.original_filename,
            stored_filename=cloned_filename,
            sha256=document.sha256,
            media_type=document.media_type,
            source_type=document.source_type,
            size_bytes=document.size_bytes,
            page_count=document.page_count,
            safety_status=document.safety_status,
            processing_status=document.processing_status,
            processing_error=document.processing_error,
            superseded=document.superseded,
            created_by=principal.user_id,
        )
        db.add(cloned_document)
        db.flush()
        for page in document.pages:
            cloned_page = DocumentPage(
                document_id=cloned_document.id,
                page_number=page.page_number,
                text=page.text,
                text_sha256=page.text_sha256,
                extraction_confidence=page.extraction_confidence,
            )
            db.add(cloned_page)
            db.flush()
            for evidence in page.evidence_spans:
                cloned_span = EvidenceSpan(
                    page_id=cloned_page.id,
                    quote=evidence.quote,
                    char_start=evidence.char_start,
                    char_end=evidence.char_end,
                    quote_sha256=evidence.quote_sha256,
                )
                db.add(cloned_span)
                db.flush()
                evidence_map[evidence.id] = cloned_span
        for run in document.extraction_runs:
            cloned_run = ExtractionRun(
                document_id=cloned_document.id,
                schema_version=run.schema_version,
                extractor=run.extractor,
                model_identifier=run.model_identifier,
                prompt_hash=run.prompt_hash,
                code_version=run.code_version,
                status=run.status,
                structured_output_hash=run.structured_output_hash,
                error_category=run.error_category,
                started_at=run.started_at,
                finished_at=run.finished_at,
            )
            db.add(cloned_run)
            db.flush()
            run_map[run.id] = cloned_run
    all_old_assertions = list(
        db.scalars(
            select(ContractAssertion)
            .options(
                selectinload(ContractAssertion.evidence),
                selectinload(ContractAssertion.decisions),
            )
            .where(ContractAssertion.case_revision_id == old.id)
        )
    )
    all_old_by_id = {item.id: item for item in all_old_assertions}
    assertion_map: dict[str, ContractAssertion] = {}

    def mapped_assertion_evidence(assertion: ContractAssertion) -> list[EvidenceSpan]:
        result: list[EvidenceSpan] = []
        for item in assertion.evidence:
            mapped_evidence = evidence_map.get(item.id)
            if mapped_evidence is None:
                raise RuntimeError(
                    "Assertion evidence is not contained in its source case revision"
                )
            result.append(mapped_evidence)
        return result

    def mapped_run_id(assertion: ContractAssertion) -> str | None:
        if assertion.extraction_run_id is None:
            return None
        mapped = run_map.get(assertion.extraction_run_id)
        return mapped.id if mapped is not None else None

    for assertion in (item for item in all_old_assertions if item.is_current):
        governance = review_governance(assertion, all_by_id=all_old_by_id)
        preserve_assumption_lineage = bool(
            governance is not None and governance.decision.assumption
        )
        cursor: ContractAssertion | None = assertion
        visited: set[str] = set()
        lineage: list[ContractAssertion] = []
        while cursor is not None and cursor.id not in visited:
            lineage.append(cursor)
            visited.add(cursor.id)
            cursor = (
                all_old_by_id.get(cursor.supersedes_assertion_id)
                if cursor.supersedes_assertion_id
                else None
            )
        if preserve_assumption_lineage:
            previous_clone: ContractAssertion | None = None
            cloned_assertion: ContractAssertion | None = None
            for source_assertion in reversed(lineage):
                cloned_assertion = ContractAssertion(
                    case_revision_id=new.id,
                    extraction_run_id=mapped_run_id(source_assertion),
                    semantic_key=source_assertion.semantic_key,
                    raw_value=source_assertion.raw_value,
                    normalized_value=dict(source_assertion.normalized_value),
                    display_value=source_assertion.display_value,
                    source=source_assertion.source,
                    confidence=source_assertion.confidence,
                    review_state=source_assertion.review_state,
                    version=source_assertion.version,
                    is_current=source_assertion.id == assertion.id,
                    supersedes_assertion_id=(
                        previous_clone.id if previous_clone is not None else None
                    ),
                    evidence=mapped_assertion_evidence(source_assertion),
                )
                db.add(cloned_assertion)
                db.flush()
                for decision in source_assertion.decisions:
                    db.add(
                        ReviewDecision(
                            assertion_id=cloned_assertion.id,
                            decision=decision.decision,
                            reviewer_id=decision.reviewer_id,
                            reason=decision.reason,
                            corrected_value=(
                                dict(decision.corrected_value)
                                if decision.corrected_value is not None
                                else None
                            ),
                            assumption=decision.assumption,
                            created_at=decision.created_at,
                        )
                    )
                previous_clone = cloned_assertion
            if cloned_assertion is None:
                raise RuntimeError("Current assertion lineage is missing")
        else:
            cloned_assertion = ContractAssertion(
                case_revision_id=new.id,
                extraction_run_id=mapped_run_id(assertion),
                semantic_key=assertion.semantic_key,
                raw_value=assertion.raw_value,
                normalized_value=dict(assertion.normalized_value),
                display_value=assertion.display_value,
                source=assertion.source,
                confidence=assertion.confidence,
                review_state=AssertionReviewState.PROPOSED,
                version=1,
                is_current=True,
                evidence=mapped_assertion_evidence(assertion),
            )
            db.add(cloned_assertion)
            db.flush()
        for source_assertion in lineage:
            assertion_map[source_assertion.id] = cloned_assertion
    for fee in db.scalars(select(FeeObligation).where(FeeObligation.case_revision_id == old.id)):
        db.add(
            FeeObligation(
                case_revision_id=new.id,
                category=fee.category,
                amount_minor=fee.amount_minor,
                currency=fee.currency,
                service_start=fee.service_start,
                service_end=fee.service_end,
                obligation_date=fee.obligation_date,
                payment_status=fee.payment_status,
                billing_cadence=fee.billing_cadence,
                proration_rule=fee.proration_rule,
                assertion_ids=[
                    assertion_map[item].id for item in fee.assertion_ids if item in assertion_map
                ],
                reviewed=False,
                primary_money_assertion_id=None,
                # Reopening makes every carried obligation unreviewed, but must
                # never resurrect a fee the reviewer explicitly superseded.
                superseded=fee.superseded,
                superseded_at=fee.superseded_at,
                superseded_by=fee.superseded_by,
                supersede_reason=fee.supersede_reason,
            )
        )
    case.current_revision_number = new.number
    case.status = CaseStatus.EVIDENCE_REVIEW
    case.version += 1
    return new


def upload_document(
    db: Session,
    *,
    principal: Principal,
    case_id: str,
    original_filename: str,
    data: bytes,
    source_type: str,
    correlation_id: str,
) -> ContractDocument:
    if source_type not in {"synthetic", "public", "redacted"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Source type must be synthetic, public, or redacted",
        )
    try:
        safety = validate_pdf_bytes(data, get_settings().max_upload_bytes)
    except PdfSafetyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    case = get_case(db, principal, case_id, for_update=True)
    if case.status == CaseStatus.ARCHIVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Archived cases are immutable",
        )
    require_evidence_mutable(case)
    base_revision = current_revision(case)
    duplicate = db.scalar(
        select(ContractDocument).where(
            ContractDocument.case_revision_id == base_revision.id,
            ContractDocument.sha256 == safety.sha256,
        )
    )
    if duplicate:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "This exact document is already in the current revision",
                "id": duplicate.id,
            },
        )
    stored_path: Path | None = None
    try:
        clean_name = Path(original_filename).name[:255] or "contract.pdf"
        stored_filename = f"{uuid.uuid4()}.pdf"
        stored_path = _safe_storage_path(stored_filename)
        _write_private_file(stored_path, data)
        register_rollback_path(db, stored_path)
        document = ContractDocument(
            organization_id=principal.organization_id,
            case_revision_id=base_revision.id,
            original_filename=clean_name,
            stored_filename=stored_filename,
            sha256=safety.sha256,
            media_type="application/pdf",
            source_type=source_type,
            size_bytes=safety.size_bytes,
            page_count=safety.page_count,
            safety_status=SafetyStatus.PASSED,
            processing_status=ProcessingStatus.UPLOADED,
            created_by=principal.user_id,
        )
        db.add(document)
        db.flush()
        append_audit_event(
            db,
            organization_id=principal.organization_id,
            actor_id=principal.user_id,
            action="document.uploaded",
            object_type="contract_document",
            object_id=document.id,
            correlation_id=correlation_id,
            after={
                "case_revision_id": base_revision.id,
                "filename": clean_name,
                "sha256": safety.sha256,
                "size_bytes": safety.size_bytes,
                "page_count": safety.page_count,
                "source_type": source_type,
            },
        )
        retire_revision_exports(
            db,
            principal=principal,
            revision_id=base_revision.id,
            correlation_id=correlation_id,
            reason=f"Document {document.id} was uploaded",
        )
        db.flush()
    except Exception:
        db.rollback()
        if stored_path is not None:
            stored_path.unlink(missing_ok=True)
        raise
    db.refresh(document)
    return document


def _canonical_key(value: str) -> str:
    return CANONICAL_KEYS.get(value, value)


def _assertion_signature(item: ExtractedAssertion) -> str:
    return json.dumps(item.normalized_value, sort_keys=True, separators=(",", ":"))


def _normalized_signature(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _flatten_normalized_candidates(value: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = value.get("candidates")
    if value.get("type") == "conflict" and isinstance(candidates, list):
        return [item for item in candidates if isinstance(item, dict)]
    return [value]


def _merge_evidence(assertion: ContractAssertion, new_evidence: list[EvidenceSpan]) -> None:
    existing_signatures = {
        (item.page_id, item.char_start, item.char_end, item.quote_sha256)
        for item in assertion.evidence
    }
    for item in new_evidence:
        signature = (item.page_id, item.char_start, item.char_end, item.quote_sha256)
        if signature not in existing_signatures:
            assertion.evidence.append(item)
            existing_signatures.add(signature)


def _persist_extraction_result(
    db: Session,
    *,
    document: ContractDocument,
    pages_by_number: dict[int, DocumentPage],
    result: ClauseExtractionResult,
    source: AssertionSource,
) -> ExtractionRun:
    now = datetime.now(UTC)
    output = {
        "assertions": [
            {
                "semantic_key": item.semantic_key,
                "normalized_value": item.normalized_value,
                "page_number": item.page_number,
                "quote": item.quote,
            }
            for item in result.assertions
        ],
        "findings": [
            {"code": item.code, "message": item.message, "severity": item.severity.value}
            for item in result.findings
        ],
    }
    run = ExtractionRun(
        document_id=document.id,
        schema_version="contract-assertions-v1",
        extractor=result.extractor,
        model_identifier=result.model_identifier,
        prompt_hash=result.prompt_hash,
        code_version="rescuedesk-extraction-v2",
        status=ExtractionRunStatus.SUCCEEDED,
        structured_output_hash=hash_payload(output),
        started_at=now,
        finished_at=now,
    )
    db.add(run)
    db.flush()
    grouped: dict[str, list[ExtractedAssertion]] = defaultdict(list)
    for item in result.assertions:
        grouped[_canonical_key(item.semantic_key)].append(item)
    for semantic_key, candidates in grouped.items():
        unique_values = {_assertion_signature(item) for item in candidates}
        conflicting = len(unique_values) > 1 or any(item.conflicting for item in candidates)
        evidence: list[EvidenceSpan] = []
        for item in candidates:
            page = pages_by_number[item.page_number]
            span = EvidenceSpan(
                page_id=page.id,
                quote=item.quote,
                char_start=item.char_start,
                char_end=item.char_end,
                quote_sha256=sha256_text(item.quote),
            )
            db.add(span)
            evidence.append(span)
        first = candidates[0]
        assertion_source = source
        if conflicting:
            normalized: dict[str, Any] = {
                "type": "conflict",
                "candidates": [item.normalized_value for item in candidates],
            }
            raw_value = " | ".join(item.raw_value for item in candidates)
            display_value = "Conflicting values: " + " | ".join(
                item.display_value for item in candidates
            )
            state = AssertionReviewState.CONFLICTING
        else:
            normalized = first.normalized_value
            raw_value = first.raw_value
            display_value = first.display_value
            state = AssertionReviewState.PROPOSED
        existing = db.scalar(
            select(ContractAssertion)
            .options(selectinload(ContractAssertion.evidence))
            .where(
                ContractAssertion.case_revision_id == document.case_revision_id,
                ContractAssertion.semantic_key == semantic_key,
                ContractAssertion.is_current.is_(True),
            )
        )
        version = 1
        supersedes_id = None
        if existing:
            existing_values = _flatten_normalized_candidates(existing.normalized_value)
            candidate_values = _flatten_normalized_candidates(normalized)
            existing_signatures = {_normalized_signature(item) for item in existing_values}
            candidate_signatures = {_normalized_signature(item) for item in candidate_values}
            if candidate_signatures.issubset(existing_signatures):
                _merge_evidence(existing, evidence)
                continue
            combined: list[dict[str, Any]] = []
            seen: set[str] = set()
            for normalized_candidate in [*existing_values, *candidate_values]:
                signature = _normalized_signature(normalized_candidate)
                if signature not in seen:
                    combined.append(normalized_candidate)
                    seen.add(signature)
            normalized = {"type": "conflict", "candidates": combined}
            raw_value = f"{existing.raw_value} | {raw_value}"
            display_value = f"Conflicting values: {existing.display_value} | {display_value}"
            state = AssertionReviewState.CONFLICTING
            assertion_source = AssertionSource.DERIVED
            evidence = [*existing.evidence, *evidence]
            existing.is_current = False
            version = existing.version + 1
            supersedes_id = existing.id
        db.add(
            ContractAssertion(
                case_revision_id=document.case_revision_id,
                extraction_run_id=run.id,
                semantic_key=semantic_key,
                raw_value=raw_value,
                normalized_value=normalized,
                display_value=display_value,
                source=assertion_source,
                confidence=min(item.confidence for item in candidates),
                review_state=state,
                version=version,
                is_current=True,
                supersedes_assertion_id=supersedes_id,
                evidence=evidence,
            )
        )
    for finding in result.findings:
        severity = FindingSeverity(finding.severity.value)
        db.add(
            ReadinessFinding(
                case_revision_id=document.case_revision_id,
                code=f"extraction.{finding.code}",
                title="Contract extraction finding",
                detail=finding.message,
                severity=severity,
            )
        )
    return run


def _raise_processing_failure(
    db: Session,
    *,
    principal: Principal,
    document_id: str,
    correlation_id: str,
    error: Exception,
    idempotency: ProcessingIdempotency | None,
) -> Never:
    """Persist a fail-closed processing state after rolling back partial work."""

    case_id = _document_case_id(db, principal, document_id)
    if case_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    get_case(db, principal, case_id, for_update=True)
    document = db.scalar(
        select(ContractDocument).where(ContractDocument.id == document_id).with_for_update()
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    document.processing_status = ProcessingStatus.FAILED
    document.processing_error = str(error)
    append_audit_event(
        db,
        organization_id=principal.organization_id,
        actor_id=principal.user_id,
        action="document.processing_failed",
        object_type="contract_document",
        object_id=document.id,
        correlation_id=correlation_id,
        after={"processing_status": "failed", "error_type": type(error).__name__},
    )
    retire_revision_exports(
        db,
        principal=principal,
        revision_id=document.case_revision_id,
        correlation_id=correlation_id,
        reason=f"Document {document.id} processing failed",
    )
    detail = "Document processing failed safely; inspect the document status for details"
    if idempotency is None:
        db.commit()
    else:
        store_response(
            db,
            organization_id=principal.organization_id,
            endpoint=idempotency.endpoint,
            key=idempotency.key,
            request_hash=idempotency.request_hash,
            response_status=status.HTTP_422_UNPROCESSABLE_CONTENT,
            response_body={"detail": detail},
        )
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=detail,
    ) from error


def _process_document_unit(
    db: Session,
    *,
    principal: Principal,
    document_id: str,
    correlation_id: str,
) -> ContractDocument:
    case_id = _document_case_id(db, principal, document_id)
    if case_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    case = get_case(db, principal, case_id, for_update=True)
    document = db.scalar(
        select(ContractDocument)
        .where(
            ContractDocument.id == document_id,
            ContractDocument.organization_id == principal.organization_id,
        )
        .with_for_update()
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    revision = current_revision(case)
    if document.case_revision_id != revision.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A prior revision is immutable; process only the current review revision",
        )
    # Run this before even re-verifying a previously processed source. A failed
    # integrity check must never rewrite document status on a frozen revision.
    require_evidence_mutable(case)
    if document.processing_status in {ProcessingStatus.PROCESSED, ProcessingStatus.NEEDS_OCR}:
        try:
            verify_document_integrity(document)
        except Exception as exc:
            raise _ProcessingUnitFailure(exc) from exc
        return document
    if document.processing_status == ProcessingStatus.PROCESSING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document processing is already in progress",
        )
    document.processing_status = ProcessingStatus.PROCESSING
    document.processing_error = None
    db.flush()
    try:
        data = verify_document_integrity(document).read_bytes()
        extracted = extract_pdf_pages(data)
        pages_by_number: dict[int, DocumentPage] = {}
        for item in extracted.pages:
            page = DocumentPage(
                document_id=document.id,
                page_number=item.page_number,
                text=item.text,
                text_sha256=item.text_sha256,
                extraction_confidence=item.extraction_confidence,
            )
            db.add(page)
            db.flush()
            pages_by_number[item.page_number] = page
        deterministic = deterministic_extract(extracted.pages)
        _persist_extraction_result(
            db,
            document=document,
            pages_by_number=pages_by_number,
            result=deterministic,
            source=AssertionSource.DETERMINISTIC,
        )
        settings = get_settings()
        if settings.enable_local_ai:
            try:
                ai_result = extract_with_ollama(
                    extracted.pages,
                    base_url=settings.ollama_url,
                    model=settings.ollama_model,
                )
            except Exception as exc:
                db.add(
                    ReadinessFinding(
                        case_revision_id=document.case_revision_id,
                        code="extraction.local_ai_unavailable",
                        title="Optional local AI extraction unavailable",
                        detail=str(exc),
                        severity=FindingSeverity.WARNING,
                    )
                )
            else:
                # A model transport failure is optional and becomes a warning. Once
                # persistence starts, however, any failure must abort the whole
                # processing unit so no partial AI lineage can be committed.
                _persist_extraction_result(
                    db,
                    document=document,
                    pages_by_number=pages_by_number,
                    result=ai_result,
                    source=AssertionSource.AI,
                )
        if extracted.needs_ocr:
            document.processing_status = ProcessingStatus.NEEDS_OCR
            db.add(
                ReadinessFinding(
                    case_revision_id=document.case_revision_id,
                    code="document.ocr_required",
                    title="OCR required",
                    detail=f"Pages requiring OCR: {', '.join(map(str, extracted.needs_ocr_pages))}",
                    severity=FindingSeverity.BLOCKING,
                )
            )
        else:
            document.processing_status = ProcessingStatus.PROCESSED
        if case.status == CaseStatus.DRAFT:
            case.status = CaseStatus.EVIDENCE_REVIEW
            case.version += 1
        append_audit_event(
            db,
            organization_id=principal.organization_id,
            actor_id=principal.user_id,
            action="document.processed",
            object_type="contract_document",
            object_id=document.id,
            correlation_id=correlation_id,
            after={
                "processing_status": document.processing_status.value,
                "page_count": len(extracted.pages),
                "needs_ocr_pages": list(extracted.needs_ocr_pages),
                "deterministic_assertions": len(deterministic.assertions),
            },
        )
        retire_revision_exports(
            db,
            principal=principal,
            revision_id=document.case_revision_id,
            correlation_id=correlation_id,
            reason=f"Document {document.id} processing result changed",
        )
        db.flush()
    except Exception as exc:
        if isinstance(exc, _ProcessingUnitFailure):
            raise
        raise _ProcessingUnitFailure(exc) from exc
    db.refresh(document)
    return document


def process_document(
    db: Session,
    *,
    principal: Principal,
    document_id: str,
    correlation_id: str,
    idempotency: ProcessingIdempotency | None = None,
) -> ContractDocument:
    """Process atomically while retaining the outer idempotency reservation."""

    try:
        with db.begin_nested():
            return _process_document_unit(
                db,
                principal=principal,
                document_id=document_id,
                correlation_id=correlation_id,
            )
    except _ProcessingUnitFailure as failure:
        _raise_processing_failure(
            db,
            principal=principal,
            document_id=document_id,
            correlation_id=correlation_id,
            error=failure.error,
            idempotency=idempotency,
        )


def list_pages(db: Session, *, principal: Principal, document_id: str) -> list[DocumentPage]:
    document = get_document(db, principal, document_id)
    return list(
        db.scalars(
            select(DocumentPage)
            .where(DocumentPage.document_id == document.id)
            .order_by(DocumentPage.page_number)
        )
    )


def supersede_document(
    db: Session,
    *,
    principal: Principal,
    document_id: str,
    payload: DocumentSupersedeRequest,
    correlation_id: str,
) -> ContractDocument:
    case_id = _document_case_id(db, principal, document_id)
    if case_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    case = get_case(db, principal, case_id, for_update=True)
    document = db.scalar(
        select(ContractDocument)
        .where(
            ContractDocument.id == document_id,
            ContractDocument.organization_id == principal.organization_id,
        )
        .with_for_update()
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    require_evidence_mutable(case)
    revision = current_revision(case)
    if document.case_revision_id != revision.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only a document in the current revision can be superseded",
        )
    if case.version != payload.expected_case_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Case changed; refresh before retrying",
                "current_version": case.version,
            },
        )
    if document.superseded:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document is already superseded",
        )
    replacement_exists = db.scalar(
        select(ContractDocument.id)
        .where(
            ContractDocument.case_revision_id == revision.id,
            ContractDocument.id != document.id,
            ContractDocument.superseded.is_(False),
            ContractDocument.processing_status == ProcessingStatus.PROCESSED,
        )
        .limit(1)
    )
    if replacement_exists is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Process a replacement document before superseding this source",
        )
    document.superseded = True
    case.version += 1
    append_audit_event(
        db,
        organization_id=principal.organization_id,
        actor_id=principal.user_id,
        action="document.superseded",
        object_type="contract_document",
        object_id=document.id,
        correlation_id=correlation_id,
        before={"superseded": False, "case_version": payload.expected_case_version},
        after={
            "case_id": case.id,
            "case_revision_id": revision.id,
            "superseded": True,
            "case_version": case.version,
            "reason": payload.reason,
        },
    )
    retire_revision_exports(
        db,
        principal=principal,
        revision_id=revision.id,
        correlation_id=correlation_id,
        reason=f"Document {document.id} was superseded",
    )
    db.flush()
    db.refresh(document)
    return document


def document_path(db: Session, *, principal: Principal, document_id: str) -> Path:
    document = get_document(db, principal, document_id)
    return verify_document_integrity(document)
