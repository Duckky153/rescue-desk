from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from rescue_desk.auth import Principal
from rescue_desk.domain.evidence import EvidenceValidationError, verify_persisted_evidence_span
from rescue_desk.domain.hashing import hash_payload, sha256_text
from rescue_desk.domain.workflow import TransitionContext, validate_transition
from rescue_desk.models import (
    CalculationRun,
    CaseRevision,
    CaseStatus,
    ContractAssertion,
    ContractDocument,
    DocumentPage,
    ExportArtifact,
    FeeObligation,
    Membership,
    ProcessingStatus,
    ReadinessFinding,
    RescueCase,
    Role,
)
from rescue_desk.schemas import CaseCreate, TransitionRequest
from rescue_desk.services.audit import append_audit_event
from rescue_desk.services.lifecycle import retire_revision_exports


def get_case(
    db: Session,
    principal: Principal,
    case_id: str,
    *,
    for_update: bool = False,
) -> RescueCase:
    statement = (
        select(RescueCase)
        .options(selectinload(RescueCase.revisions))
        .where(
            RescueCase.id == case_id,
            RescueCase.organization_id == principal.organization_id,
        )
    )
    if for_update:
        statement = statement.with_for_update()
    case = db.scalar(statement)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
    return case


def current_revision(case: RescueCase) -> CaseRevision:
    for revision in case.revisions:
        if revision.number == case.current_revision_number:
            return revision
    raise RuntimeError("Case current revision is missing")


def require_evidence_mutable(case: RescueCase) -> None:
    if case.status not in {CaseStatus.DRAFT, CaseStatus.EVIDENCE_REVIEW}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This review revision is locked. Return it to evidence review first; "
                "approved or exported cases will receive a new revision."
            ),
        )


def _validate_assignment(
    db: Session,
    *,
    organization_id: str,
    user_id: str | None,
    allowed_roles: set[Role],
    label: str,
) -> None:
    if user_id is None:
        return
    membership = db.scalar(
        select(Membership).where(
            Membership.organization_id == organization_id,
            Membership.user_id == user_id,
        )
    )
    if membership is None or membership.role not in allowed_roles:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{label} must be an active member of this organization with an eligible role",
        )


def create_case(
    db: Session,
    *,
    principal: Principal,
    payload: CaseCreate,
    correlation_id: str,
) -> RescueCase:
    assigned_analyst_id = payload.assigned_analyst_id or principal.user_id
    _validate_assignment(
        db,
        organization_id=principal.organization_id,
        user_id=assigned_analyst_id,
        allowed_roles={Role.ANALYST, Role.APPROVER, Role.ADMIN},
        label="Assigned analyst",
    )
    _validate_assignment(
        db,
        organization_id=principal.organization_id,
        user_id=payload.assigned_approver_id,
        allowed_roles={Role.APPROVER, Role.ADMIN},
        label="Assigned approver",
    )
    case = RescueCase(
        organization_id=principal.organization_id,
        display_name=payload.display_name,
        applicant_company=payload.applicant_company,
        erp_provider=payload.erp_provider,
        assigned_analyst_id=assigned_analyst_id,
        assigned_approver_id=payload.assigned_approver_id,
    )
    db.add(case)
    db.flush()
    revision = CaseRevision(
        case_id=case.id,
        number=1,
        reason="Initial case revision",
        created_by=principal.user_id,
    )
    db.add(revision)
    append_audit_event(
        db,
        organization_id=principal.organization_id,
        actor_id=principal.user_id,
        action="case.created",
        object_type="rescue_case",
        object_id=case.id,
        correlation_id=correlation_id,
        after={
            "display_name": case.display_name,
            "applicant_company": case.applicant_company,
            "erp_provider": case.erp_provider,
            "status": case.status.value,
            "revision": 1,
        },
    )
    db.flush()
    return get_case(db, principal, case.id)


def list_cases(db: Session, principal: Principal) -> list[RescueCase]:
    return list(
        db.scalars(
            select(RescueCase)
            .where(RescueCase.organization_id == principal.organization_id)
            .order_by(RescueCase.updated_at.desc(), RescueCase.id)
        )
    )


def revision_snapshot(db: Session, revision_id: str) -> dict[str, Any]:
    revision = db.get(CaseRevision, revision_id)
    if revision is None:
        raise RuntimeError("Case revision is missing")
    case = db.get(RescueCase, revision.case_id)
    if case is None:
        raise RuntimeError("Case for revision is missing")
    documents = list(
        db.scalars(
            select(ContractDocument)
            .options(
                selectinload(ContractDocument.pages).selectinload(DocumentPage.evidence_spans),
                selectinload(ContractDocument.extraction_runs),
            )
            .where(ContractDocument.case_revision_id == revision_id)
            .order_by(ContractDocument.id)
        )
    )
    # Approval and export snapshots fail closed if runtime bytes diverge from intake.
    from rescue_desk.services.documents import verify_document_integrity

    for document in documents:
        verify_document_integrity(document)
        for page in document.pages:
            if sha256_text(page.text) != page.text_sha256:
                raise RuntimeError(
                    f"Document page {page.id} failed its text SHA-256 integrity check"
                )
            for evidence in page.evidence_spans:
                try:
                    verify_persisted_evidence_span(
                        page_text=page.text,
                        page_text_sha256=page.text_sha256,
                        quote=evidence.quote,
                        quote_sha256=evidence.quote_sha256,
                        char_start=evidence.char_start,
                        char_end=evidence.char_end,
                    )
                except EvidenceValidationError as exc:
                    raise RuntimeError(
                        f"Evidence span {evidence.id} failed persisted integrity validation"
                    ) from exc
    assertions = list(
        db.scalars(
            select(ContractAssertion)
            .options(
                selectinload(ContractAssertion.evidence),
                selectinload(ContractAssertion.decisions),
            )
            .where(
                ContractAssertion.case_revision_id == revision_id,
            )
            .order_by(
                ContractAssertion.semantic_key,
                ContractAssertion.version,
                ContractAssertion.id,
            )
        )
    )
    fees = list(
        db.scalars(
            select(FeeObligation)
            .where(FeeObligation.case_revision_id == revision_id)
            .order_by(FeeObligation.id)
        )
    )
    calculation = db.scalar(
        select(CalculationRun)
        .options(selectinload(CalculationRun.line_items))
        .where(CalculationRun.case_revision_id == revision_id)
        .order_by(CalculationRun.created_at.desc(), CalculationRun.id.desc())
        .limit(1)
    )
    findings = list(
        db.scalars(
            select(ReadinessFinding)
            .where(ReadinessFinding.case_revision_id == revision_id)
            .order_by(ReadinessFinding.id)
        )
    )
    return {
        "case_identity": {
            "id": case.id,
            "organization_id": case.organization_id,
            "display_name": case.display_name,
            "applicant_company": case.applicant_company,
            "erp_provider": case.erp_provider,
        },
        "revision": {
            "id": revision.id,
            "number": revision.number,
            "previous_revision_id": revision.previous_revision_id,
            "reason": revision.reason,
        },
        "documents": [
            {
                "id": document.id,
                "original_filename": document.original_filename,
                "sha256": document.sha256,
                "size_bytes": document.size_bytes,
                "page_count": document.page_count,
                "media_type": document.media_type,
                "source_type": document.source_type,
                "safety_status": document.safety_status.value,
                "processing_status": document.processing_status.value,
                "superseded": document.superseded,
                "extraction_runs": [
                    {
                        "id": run.id,
                        "schema_version": run.schema_version,
                        "extractor": run.extractor,
                        "model_identifier": run.model_identifier,
                        "prompt_hash": run.prompt_hash,
                        "code_version": run.code_version,
                        "status": run.status.value,
                        "structured_output_hash": run.structured_output_hash,
                        "error_category": run.error_category,
                        "started_at": run.started_at.isoformat(),
                        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                    }
                    for run in sorted(document.extraction_runs, key=lambda item: item.id)
                ],
                "pages": [
                    {
                        "id": page.id,
                        "page_number": page.page_number,
                        "text_sha256": page.text_sha256,
                        "extraction_confidence": str(page.extraction_confidence),
                        "evidence": [
                            {
                                "id": evidence.id,
                                "quote": evidence.quote,
                                "quote_sha256": evidence.quote_sha256,
                                "char_start": evidence.char_start,
                                "char_end": evidence.char_end,
                            }
                            for evidence in sorted(page.evidence_spans, key=lambda item: item.id)
                        ],
                    }
                    for page in sorted(document.pages, key=lambda item: item.page_number)
                ],
            }
            for document in documents
        ],
        "assertions": [
            {
                "id": item.id,
                "extraction_run_id": item.extraction_run_id,
                "semantic_key": item.semantic_key,
                "raw_value": item.raw_value,
                "normalized_value": item.normalized_value,
                "display_value": item.display_value,
                "source": item.source.value,
                "confidence": str(item.confidence),
                "review_state": item.review_state.value,
                "version": item.version,
                "is_current": item.is_current,
                "supersedes_assertion_id": item.supersedes_assertion_id,
                "evidence_ids": sorted(evidence.id for evidence in item.evidence),
                "decisions": [
                    {
                        "id": decision.id,
                        "decision": decision.decision.value,
                        "reviewer_id": decision.reviewer_id,
                        "reason": decision.reason,
                        "corrected_value": decision.corrected_value,
                        "assumption": decision.assumption,
                    }
                    for decision in sorted(item.decisions, key=lambda value: value.id)
                ],
            }
            for item in assertions
        ],
        "fees": [
            {
                "id": item.id,
                "category": item.category.value,
                "amount_minor": item.amount_minor,
                "currency": item.currency,
                "service_start": item.service_start.isoformat() if item.service_start else None,
                "service_end": item.service_end.isoformat() if item.service_end else None,
                "obligation_date": (
                    item.obligation_date.isoformat() if item.obligation_date else None
                ),
                "payment_status": item.payment_status,
                "billing_cadence": item.billing_cadence,
                "proration_rule": item.proration_rule,
                "assertion_ids": sorted(item.assertion_ids),
                "reviewed": item.reviewed,
                "primary_money_assertion_id": item.primary_money_assertion_id,
                "superseded": item.superseded,
                "superseded_at": (item.superseded_at.isoformat() if item.superseded_at else None),
                "superseded_by": item.superseded_by,
                "supersede_reason": item.supersede_reason,
            }
            for item in fees
        ],
        "calculation": (
            {
                "id": calculation.id,
                "as_of_date": calculation.as_of_date.isoformat(),
                "engine_version": calculation.engine_version,
                "input_snapshot": calculation.input_snapshot,
                "input_hash": calculation.input_hash,
                "assumptions": calculation.assumptions,
                "result_snapshot": calculation.result_snapshot,
                "result_hash": calculation.result_hash,
                "created_by": calculation.created_by,
                "line_items": [
                    {
                        "id": line.id,
                        "fee_obligation_id": line.fee_obligation_id,
                        "treatment": line.treatment,
                        "original_amount_minor": line.original_amount_minor,
                        "remaining_amount_minor": line.remaining_amount_minor,
                        "currency": line.currency,
                        "formula_identifier": line.formula_identifier,
                        "explanation": line.explanation,
                    }
                    for line in sorted(calculation.line_items, key=lambda item: item.id)
                ],
            }
            if calculation
            else None
        ),
        # Kept as a top-level compatibility key for export approval checks.
        "calculation_result_hash": calculation.result_hash if calculation else None,
        "findings": [
            {
                "id": item.id,
                "code": item.code,
                "title": item.title,
                "detail": item.detail,
                "severity": item.severity.value,
                "status": item.status.value,
                "resolution_reason": item.resolution_reason,
                "version": item.version,
            }
            for item in findings
        ],
    }


def transition_case(
    db: Session,
    *,
    principal: Principal,
    case_id: str,
    payload: TransitionRequest,
    correlation_id: str,
) -> RescueCase:
    case = get_case(db, principal, case_id, for_update=True)
    if case.version != payload.expected_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Case changed; refresh before retrying",
                "current_version": case.version,
            },
        )
    revision = current_revision(case)
    documents = list(
        db.scalars(
            select(ContractDocument).where(
                ContractDocument.case_revision_id == revision.id,
                ContractDocument.superseded.is_(False),
            )
        )
    )
    has_document = bool(documents)
    extraction_finished = has_document and all(
        item.processing_status == ProcessingStatus.PROCESSED for item in documents
    )
    # Import locally to keep the readiness evaluator reusable without a module cycle.
    from rescue_desk.services.readiness import compute_readiness

    readiness = compute_readiness(db, principal=principal, case_id=case_id)
    mandatory_review_complete = bool(readiness["mandatory_assertions_reviewed"])
    calculation_exists = bool(readiness["reproducible_calculation_exists"])
    open_blockers = int(readiness["open_blocking_findings"])
    approved_export_candidates: list[ExportArtifact] = []
    if payload.target == CaseStatus.EXPORTED:
        try:
            current_revision_snapshot_hash = hash_payload(revision_snapshot(db, revision.id))
        except RuntimeError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"The approved revision failed integrity validation: {exc}",
            ) from exc
        if revision.snapshot_hash == current_revision_snapshot_hash:
            approved_export_candidates = list(
                db.scalars(
                    select(ExportArtifact).where(
                        ExportArtifact.case_revision_id == revision.id,
                        ExportArtifact.superseded.is_(False),
                        ExportArtifact.approval_status == "approved",
                        ExportArtifact.revision_snapshot_sha256 == current_revision_snapshot_hash,
                        ExportArtifact.approval_case_version == case.version,
                    )
                )
            )
    approved_export_exists = False
    if payload.target == CaseStatus.EXPORTED:
        # Import locally because export packet construction depends on case helpers.
        from rescue_desk.services.exports import verify_export_artifact_integrity

        for artifact in approved_export_candidates:
            try:
                verify_export_artifact_integrity(db, artifact)
            except HTTPException:
                continue
            approved_export_exists = True
            break
    if (
        payload.target == CaseStatus.INTERNAL_PACKET_APPROVED
        and case.assigned_approver_id is not None
        and principal.user_id != case.assigned_approver_id
        and principal.role != Role.ADMIN
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only the assigned approver (or an administrator) can approve this case",
        )
    validate_transition(
        case.status,
        payload.target,
        TransitionContext(
            actor_role=principal.role,
            has_document=has_document,
            extraction_finished=extraction_finished,
            mandatory_assertions_reviewed=mandatory_review_complete,
            has_reproducible_calculation=calculation_exists,
            open_blocking_findings=open_blockers,
            approved_export_exists=approved_export_exists,
        ),
    )
    before_status = case.status
    if payload.target == CaseStatus.EVIDENCE_REVIEW and before_status in {
        CaseStatus.INTERNAL_PACKET_APPROVED,
        CaseStatus.EXPORTED,
    }:
        from rescue_desk.services.documents import clone_current_revision

        retire_revision_exports(
            db,
            principal=principal,
            revision_id=revision.id,
            correlation_id=correlation_id,
            reason=(
                f"Case transition from {before_status.value} to evidence_review created a "
                "new revision"
            ),
        )
        new_revision = clone_current_revision(
            db,
            case=case,
            principal=principal,
            reason=payload.reason,
        )
        append_audit_event(
            db,
            organization_id=principal.organization_id,
            actor_id=principal.user_id,
            action="case.revision_created",
            object_type="case_revision",
            object_id=new_revision.id,
            correlation_id=correlation_id,
            after={
                "case_id": case.id,
                "revision_number": new_revision.number,
                "previous_revision_id": revision.id,
                "reason": payload.reason,
            },
        )
        append_audit_event(
            db,
            organization_id=principal.organization_id,
            actor_id=principal.user_id,
            action="case.transitioned",
            object_type="rescue_case",
            object_id=case.id,
            correlation_id=correlation_id,
            before={"status": before_status.value, "version": payload.expected_version},
            after={
                "status": case.status.value,
                "version": case.version,
                "reason": payload.reason,
                "revision_id": new_revision.id,
            },
        )
        db.flush()
        return get_case(db, principal, case.id)
    case.status = payload.target
    case.version += 1
    if payload.target == CaseStatus.INTERNAL_PACKET_APPROVED:
        snapshot = revision_snapshot(db, revision.id)
        revision.snapshot_hash = hash_payload(snapshot)
    # Export packets deliberately canonicalize EXPORTED as the already approved
    # snapshot, so delivery does not change packet content. Keep that exact
    # integrity-checked artifact current/downloadable until a real evidence or
    # revision mutation. Other status changes invalidate materialized packets.
    if payload.target != CaseStatus.EXPORTED:
        retire_revision_exports(
            db,
            principal=principal,
            revision_id=revision.id,
            correlation_id=correlation_id,
            reason=f"Case status changed from {before_status.value} to {payload.target.value}",
        )
    append_audit_event(
        db,
        organization_id=principal.organization_id,
        actor_id=principal.user_id,
        action="case.transitioned",
        object_type="rescue_case",
        object_id=case.id,
        correlation_id=correlation_id,
        before={"status": before_status.value, "version": payload.expected_version},
        after={
            "status": payload.target.value,
            "version": case.version,
            "reason": payload.reason,
            "revision_id": revision.id,
            "revision_snapshot_sha256": revision.snapshot_hash,
        },
    )
    db.flush()
    return get_case(db, principal, case.id)
