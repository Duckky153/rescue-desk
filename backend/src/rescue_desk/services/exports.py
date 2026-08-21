from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from rescue_desk.auth import Principal
from rescue_desk.config import get_settings
from rescue_desk.database import register_rollback_path
from rescue_desk.domain.assertions import (
    SemanticValueError,
    canonical_assertion_display,
)
from rescue_desk.domain.hashing import hash_payload
from rescue_desk.domain.money import currency_exponent
from rescue_desk.exports import (
    DISCLAIMER,
    ApprovalStatus,
    BlockerStatus,
    CalculationLine,
    CurrencyScenario,
    ExportContractError,
    ExportPacket,
    FactProvenance,
    ReviewBlocker,
    ReviewedFact,
    ReviewState,
    SourceCitation,
    build_customer_explanation_pdf,
    build_evidence_csv,
    build_internal_review_pdf,
    build_machine_json,
    content_type_for,
    extension_for,
)
from rescue_desk.exports import ExportKind as PacketExportKind
from rescue_desk.models import (
    AssertionReviewState,
    AuditEvent,
    CalculationRun,
    CaseRevision,
    CaseStatus,
    ContractAssertion,
    DocumentPage,
    EvidenceSpan,
    ExportArtifact,
    ExportKind,
    FeeObligation,
    FindingSeverity,
    RescueCase,
    User,
)
from rescue_desk.services.audit import (
    AuditIntegrityError,
    append_audit_event,
    verify_organization_audit_integrity,
)
from rescue_desk.services.calculations import (
    CalculationIntegrityError,
    verify_calculation_integrity,
)
from rescue_desk.services.cases import current_revision, get_case, revision_snapshot
from rescue_desk.services.evidence_governance import review_governance
from rescue_desk.services.lifecycle import retire_revision_exports

GENERATOR_VERSION = "rescuedesk-export-v2"

_FORMULAS: dict[str, str] = {
    "paid-no-remaining-obligation-v1": "result_minor = 0 when payment_status = 'paid'",
    "payment-status-unknown": (
        "result_minor = null because payment_status is unknown, not proven unpaid"
    ),
    "missing-service-period": (
        "result_minor = null because service_start or service_end is missing"
    ),
    "expired-service-period-v1": "result_minor = 0 when as_of_date >= service_end",
    "future-full-obligation-v1": "result_minor = original_minor when as_of_date <= service_start",
    "proration-unsupported": (
        "result_minor = null because the reviewed proration_rule is unsupported"
    ),
    "contract-daily-half-open-v1": (
        "round_half_up(original_minor * remaining_days / total_service_days)"
    ),
    "unreviewed-v1": "result_minor = null; unreviewed amount remains unclassified",
    "unclassified-v1": "result_minor = null; unclassified amount remains unclassified",
    "non-subscription-exclusion-v1": ("result_minor = 0; non-subscription amount is excluded"),
}


@dataclass(frozen=True, slots=True)
class ExportDownload:
    artifact: ExportArtifact
    path: Path
    content_type: str
    download_name: str
    packet_snapshot_sha256: str


@dataclass(frozen=True, slots=True)
class ApprovalContext:
    status: ApprovalStatus
    approved_by_name: str | None
    approved_by_user_id: str | None
    prepared_at: datetime
    prepared_by_name: str
    prepared_by_user_id: str
    approval_case_version: int | None


@dataclass(frozen=True, slots=True)
class PreparedExport:
    packet: ExportPacket
    calculation: CalculationRun
    revision_snapshot_sha256: str
    context: ApprovalContext


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _packet_kind(kind: ExportKind) -> PacketExportKind:
    return PacketExportKind(kind.value)


def _render(kind: PacketExportKind, packet: ExportPacket) -> bytes:
    builders = {
        PacketExportKind.INTERNAL_REVIEW_PDF: build_internal_review_pdf,
        PacketExportKind.CUSTOMER_EXPLANATION_PDF: build_customer_explanation_pdf,
        PacketExportKind.EVIDENCE_CSV: build_evidence_csv,
        PacketExportKind.MACHINE_READABLE_JSON: build_machine_json,
    }
    return builders[kind](packet)


def _export_root() -> Path:
    # Settings deliberately has one runtime data root. Keeping exports beside uploads
    # avoids inventing a second, unconfigured storage location.
    root = (get_settings().upload_dir.parent / "exports").resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def _artifact_path(stored_filename: str) -> Path:
    if not stored_filename or Path(stored_filename).name != stored_filename:
        raise _conflict("Export artifact has an unsafe storage name")
    root = _export_root()
    candidate = root / stored_filename
    if candidate.is_symlink():
        raise _conflict("Export artifact cannot be a symbolic link")
    target = candidate.resolve(strict=False)
    if target.parent != root:
        raise _conflict("Export artifact resolved outside the configured storage directory")
    return target


def _write_atomic(target: Path, value: bytes) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=".rescuedesk-export-",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.chmod(temporary_path, 0o600)
            temporary.write(value)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _load_assertions(db: Session, revision_id: str) -> list[ContractAssertion]:
    return list(
        db.scalars(
            select(ContractAssertion)
            .options(
                selectinload(ContractAssertion.evidence)
                .selectinload(EvidenceSpan.page)
                .selectinload(DocumentPage.document),
                selectinload(ContractAssertion.decisions),
                selectinload(ContractAssertion.extraction_run),
            )
            .where(ContractAssertion.case_revision_id == revision_id)
            .order_by(ContractAssertion.semantic_key, ContractAssertion.version)
            .execution_options(populate_existing=True)
        )
    )


def _current_descendant(
    assertion_id: str,
    *,
    all_by_id: dict[str, ContractAssertion],
    current_assertions: list[ContractAssertion],
) -> ContractAssertion | None:
    source = all_by_id.get(assertion_id)
    if source is None:
        return None
    if source.is_current:
        return source
    for candidate in current_assertions:
        cursor: ContractAssertion | None = candidate
        visited: set[str] = set()
        while cursor is not None and cursor.id not in visited:
            if cursor.id == source.id:
                return candidate
            visited.add(cursor.id)
            cursor = (
                all_by_id.get(cursor.supersedes_assertion_id)
                if cursor.supersedes_assertion_id
                else None
            )
    return None


def _citation(evidence: EvidenceSpan) -> SourceCitation:
    document = evidence.page.document
    return SourceCitation(
        citation_id=evidence.id,
        document_id=document.id,
        document_name=document.original_filename,
        document_sha256=document.sha256,
        page_number=evidence.page.page_number,
        quote=evidence.quote,
        quote_sha256=evidence.quote_sha256,
        char_start=evidence.char_start,
        char_end=evidence.char_end,
        superseded=document.superseded,
    )


def _assertion_citation_ids(assertion: ContractAssertion) -> tuple[str, ...]:
    return tuple(
        sorted({item.id for item in assertion.evidence if not item.page.document.superseded})
    )


def _approval(
    db: Session,
    *,
    case: RescueCase,
    revision: CaseRevision,
    calculation: CalculationRun,
    current_snapshot: dict[str, Any],
    current_snapshot_hash: str,
) -> ApprovalContext:
    approved_state = case.status in {
        CaseStatus.INTERNAL_PACKET_APPROVED,
        CaseStatus.EXPORTED,
    }
    if not approved_state:
        preparer = db.get(User, calculation.created_by)
        if preparer is None:
            raise _conflict("The calculation preparer no longer exists")
        return ApprovalContext(
            status=ApprovalStatus.NOT_APPROVED,
            approved_by_name=None,
            approved_by_user_id=None,
            prepared_at=_as_utc(calculation.created_at),
            prepared_by_name=preparer.display_name,
            prepared_by_user_id=preparer.id,
            approval_case_version=None,
        )

    if not revision.snapshot_hash or revision.snapshot_hash != current_snapshot_hash:
        raise _conflict(
            "The approved snapshot no longer matches the current revision; return the case "
            "to evidence review before exporting"
        )
    if current_snapshot.get("calculation_result_hash") != calculation.result_hash:
        raise _conflict(
            "The selected calculation is not the calculation in the approver-recorded snapshot"
        )
    events = list(
        db.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.organization_id == case.organization_id,
                AuditEvent.object_type == "rescue_case",
                AuditEvent.object_id == case.id,
                AuditEvent.action == "case.transitioned",
            )
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        )
    )

    def matches_current_approval(item: AuditEvent) -> bool:
        before = item.before or {}
        after = item.after or {}
        approval_version = after.get("version")
        if not isinstance(approval_version, int):
            return False
        if (
            after.get("status") != CaseStatus.INTERNAL_PACKET_APPROVED.value
            or after.get("revision_id") != revision.id
            or after.get("revision_snapshot_sha256") != current_snapshot_hash
            or before.get("status") != CaseStatus.READY_FOR_INTERNAL_REVIEW.value
            or before.get("version") != approval_version - 1
        ):
            return False
        if case.status == CaseStatus.INTERNAL_PACKET_APPROVED:
            return case.version == approval_version
        return bool(
            case.status == CaseStatus.EXPORTED
            and case.version == approval_version + 1
            and any(
                export_event.before
                and export_event.after
                and export_event.before.get("status") == CaseStatus.INTERNAL_PACKET_APPROVED.value
                and export_event.before.get("version") == approval_version
                and export_event.after.get("status") == CaseStatus.EXPORTED.value
                and export_event.after.get("version") == case.version
                and export_event.after.get("revision_id") == revision.id
                and export_event.after.get("revision_snapshot_sha256") == current_snapshot_hash
                for export_event in events
            )
        )

    approval_event = next((item for item in events if matches_current_approval(item)), None)
    if approval_event is None:
        raise _conflict(
            "The case is marked approved but has no matching approver-role event for the "
            "current revision, snapshot, and lifecycle version"
        )
    approver = db.get(User, approval_event.actor_id)
    if approver is None:
        raise _conflict("The approver recorded in the audit trail no longer exists")
    approval_version = approval_event.after.get("version") if approval_event.after else None
    if not isinstance(approval_version, int):
        raise _conflict("The approval audit event has no valid case version")
    return ApprovalContext(
        status=ApprovalStatus.APPROVED,
        approved_by_name=approver.display_name,
        approved_by_user_id=approver.id,
        prepared_at=_as_utc(approval_event.created_at),
        prepared_by_name=approver.display_name,
        prepared_by_user_id=approver.id,
        approval_case_version=approval_version,
    )


def _calculation(
    db: Session,
    *,
    revision_id: str,
    calculation_id: str | None,
) -> CalculationRun:
    statement = (
        select(CalculationRun)
        .options(selectinload(CalculationRun.line_items))
        .where(CalculationRun.case_revision_id == revision_id)
        .order_by(CalculationRun.created_at.desc(), CalculationRun.id.desc())
    )
    calculation = db.scalar(statement.limit(1))
    if calculation is None:
        if calculation_id is not None:
            selected_revision_id = db.scalar(
                select(CalculationRun.case_revision_id).where(CalculationRun.id == calculation_id)
            )
            if selected_revision_id != revision_id:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="The selected calculation does not belong to the current case revision",
                )
        raise _conflict("Run a current-revision calculation before generating an export")
    if calculation_id is not None and calculation.id != calculation_id:
        selected_revision_id = db.scalar(
            select(CalculationRun.case_revision_id).where(CalculationRun.id == calculation_id)
        )
        if selected_revision_id != revision_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="The selected calculation does not belong to the current case revision",
            )
        raise _conflict(
            "The selected calculation is not the latest current-revision calculation; "
            "refresh and export the latest calculation"
        )
    return calculation


def _ensure_calculation_current(
    db: Session,
    *,
    revision_id: str,
    calculation: CalculationRun,
) -> None:
    try:
        verify_calculation_integrity(
            db,
            revision_id=revision_id,
            calculation=calculation,
        )
    except CalculationIntegrityError as exc:
        raise _conflict(
            "The selected calculation is stale or inconsistent; rerun it before export"
        ) from exc


def _scenarios(calculation: CalculationRun) -> tuple[CurrencyScenario, ...]:
    raw_scenarios = calculation.result_snapshot.get("currencies")
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        raise _conflict("The selected calculation has no currency scenario snapshot")
    scenarios: list[CurrencyScenario] = []
    try:
        for raw in raw_scenarios:
            if not isinstance(raw, dict):
                raise TypeError
            currency = str(raw["currency"]).upper()
            scenarios.append(
                CurrencyScenario(
                    currency=currency,
                    documented_remaining_subscription_minor=int(
                        raw["documented_remaining_subscription_minor"]
                    ),
                    excluded_non_subscription_minor=int(raw["excluded_non_subscription_minor"]),
                    unclassified_minor=int(raw["unclassified_minor"]),
                    potential_coverage_min_minor=int(raw["potential_coverage_min_minor"]),
                    potential_coverage_max_minor=int(raw["potential_coverage_max_minor"]),
                    currency_exponent=currency_exponent(currency),
                )
            )
    except (KeyError, TypeError, ValueError, ExportContractError) as exc:
        raise _conflict("The selected calculation contains an invalid scenario snapshot") from exc
    return tuple(scenarios)


def _calculation_lines(
    db: Session,
    *,
    calculation: CalculationRun,
    all_by_id: dict[str, ContractAssertion],
    current_assertions: list[ContractAssertion],
) -> tuple[CalculationLine, ...]:
    fee_ids = {
        item.fee_obligation_id
        for item in calculation.line_items
        if item.fee_obligation_id is not None
    }
    fees = {
        item.id: item
        for item in db.scalars(select(FeeObligation).where(FeeObligation.id.in_(fee_ids)))
    }
    lines: list[CalculationLine] = []
    for item in sorted(calculation.line_items, key=lambda value: value.id):
        if item.fee_obligation_id is None or item.fee_obligation_id not in fees:
            raise _conflict(f"Calculation line {item.id} has no source fee obligation")
        fee = fees[item.fee_obligation_id]
        source_assertions: list[ContractAssertion] = []
        for assertion_id in fee.assertion_ids:
            current = _current_descendant(
                assertion_id,
                all_by_id=all_by_id,
                current_assertions=current_assertions,
            )
            if current is None:
                raise _conflict(
                    f"Calculation line {item.id} references an assertion that is not current"
                )
            if current.review_state not in {
                AssertionReviewState.ACCEPTED,
                AssertionReviewState.CORRECTED,
            }:
                raise _conflict(
                    f"Calculation line {item.id} relies on an assertion that is not reviewed"
                )
            source_assertions.append(current)
        citation_ids = tuple(
            sorted(
                {
                    evidence.id
                    for assertion in source_assertions
                    for evidence in assertion.evidence
                    if not evidence.page.document.superseded
                }
            )
        )
        if not citation_ids:
            raise _conflict(
                f"Calculation line {item.id} needs an exact page citation before export"
            )
        formula = _FORMULAS.get(item.formula_identifier)
        if formula is None:
            raise _conflict(f"Calculation line {item.id} uses an unsupported formula identifier")
        currency = item.currency.upper()
        lines.append(
            CalculationLine(
                line_id=item.id,
                label=f"{fee.category.value.replace('_', ' ').title()} obligation",
                currency=currency,
                original_amount_minor=item.original_amount_minor,
                result_amount_minor=item.remaining_amount_minor,
                treatment=item.treatment,
                formula_id=item.formula_identifier,
                formula=formula,
                explanation=item.explanation,
                citation_ids=citation_ids,
                currency_exponent=currency_exponent(currency),
            )
        )
    if not lines:
        raise _conflict("The selected calculation has no line-item detail")
    return tuple(lines)


def _review_blockers(readiness: dict[str, Any]) -> tuple[ReviewBlocker, ...]:
    blockers: list[ReviewBlocker] = []
    for item in readiness["findings"]:
        if isinstance(item, dict):
            severity = item["severity"]
            if str(severity) not in {FindingSeverity.BLOCKING, FindingSeverity.BLOCKING.value}:
                continue
            resolution_reason = item.get("resolution_reason")
            detail = str(item["detail"])
            if resolution_reason is not None:
                detail = f"{detail} Resolution: {resolution_reason}"
            blockers.append(
                ReviewBlocker(
                    blocker_id=str(item["id"]),
                    title=str(item["title"]),
                    detail=detail,
                    status=BlockerStatus(str(item["status"])),
                )
            )
            continue
        if item.severity != FindingSeverity.BLOCKING:
            continue
        blockers.append(
            ReviewBlocker(
                blocker_id=item.id,
                title=item.title,
                detail=(
                    item.detail
                    if item.resolution_reason is None
                    else f"{item.detail} Resolution: {item.resolution_reason}"
                ),
                status=BlockerStatus(item.status.value),
            )
        )
    return tuple(blockers)


def _fact_provenance(
    db: Session,
    *,
    assertion: ContractAssertion,
    all_by_id: dict[str, ContractAssertion],
) -> FactProvenance:
    governance = review_governance(assertion, all_by_id=all_by_id)
    reviewed_states = {
        AssertionReviewState.ACCEPTED,
        AssertionReviewState.CORRECTED,
        AssertionReviewState.REJECTED,
    }
    if assertion.review_state in reviewed_states and governance is None:
        raise _conflict(
            f"Assertion {assertion.id} lacks a valid audited review decision or assumption label"
        )
    source = governance.source_assertion if governance is not None else assertion
    reviewer: User | None = None
    if governance is not None:
        reviewer = db.get(User, governance.decision.reviewer_id)
        if reviewer is None:
            raise _conflict(f"Assertion {assertion.id} reviewer no longer exists")
    extraction_run = source.extraction_run
    if governance is None:
        evidence_basis = "pending_review"
    elif assertion.review_state == AssertionReviewState.REJECTED:
        evidence_basis = "rejected_source"
    elif governance.decision.assumption:
        evidence_basis = "explicit_assumption"
    else:
        evidence_basis = "source_evidence"
    return FactProvenance(
        assertion_source=assertion.source.value,
        source_assertion_id=source.id,
        reviewer_id=reviewer.id if reviewer else None,
        reviewer_name=reviewer.display_name if reviewer else None,
        review_reason=governance.decision.reason if governance else None,
        assumption=bool(governance and governance.decision.assumption),
        evidence_basis=evidence_basis,
        extraction_run_id=extraction_run.id if extraction_run else None,
        extractor=extraction_run.extractor if extraction_run else None,
        model_identifier=extraction_run.model_identifier if extraction_run else None,
        prompt_hash=extraction_run.prompt_hash if extraction_run else None,
        code_version=extraction_run.code_version if extraction_run else None,
        structured_output_hash=(extraction_run.structured_output_hash if extraction_run else None),
    )


def _fact_value(assertion: ContractAssertion) -> str:
    try:
        canonical = canonical_assertion_display(
            assertion.semantic_key,
            assertion.normalized_value,
        )
    except SemanticValueError:
        if assertion.review_state in {
            AssertionReviewState.ACCEPTED,
            AssertionReviewState.CORRECTED,
            AssertionReviewState.REJECTED,
        }:
            raise _conflict(
                f"Assertion {assertion.id} has a reviewed value outside its semantic contract"
            ) from None
        return assertion.display_value
    if assertion.display_value != canonical:
        raise _conflict(
            f"Assertion {assertion.id} display text does not match its canonical typed value"
        )
    return canonical


def build_export_packet(
    db: Session,
    *,
    principal: Principal,
    case_id: str,
    calculation_id: str | None,
    locked_case: RescueCase | None = None,
) -> PreparedExport:
    case = locked_case or get_case(db, principal, case_id)
    if case.id != case_id or case.organization_id != principal.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
    revision = current_revision(case)
    calculation = _calculation(
        db,
        revision_id=revision.id,
        calculation_id=calculation_id,
    )
    _ensure_calculation_current(
        db,
        revision_id=revision.id,
        calculation=calculation,
    )
    assertions = _load_assertions(db, revision.id)
    current_assertions = [item for item in assertions if item.is_current]
    if not current_assertions:
        raise _conflict("Review at least one evidence-backed assertion before exporting")
    unsupported = [
        item.semantic_key for item in current_assertions if not _assertion_citation_ids(item)
    ]
    if unsupported:
        raise _conflict(
            "Every exported assertion needs an exact page citation; missing evidence for: "
            + ", ".join(sorted(unsupported))
        )

    citations_by_id = {
        evidence.id: _citation(evidence)
        for assertion in current_assertions
        for evidence in assertion.evidence
        if not evidence.page.document.superseded
    }
    all_by_id = {item.id: item for item in assertions}
    facts = tuple(
        ReviewedFact(
            fact_id=item.id,
            label=item.semantic_key.replace("_", " ").title(),
            value=_fact_value(item),
            review_state=ReviewState(item.review_state.value),
            citation_ids=_assertion_citation_ids(item),
            provenance=_fact_provenance(db, assertion=item, all_by_id=all_by_id),
            customer_visible=item.review_state.value != ReviewState.REJECTED.value,
        )
        for item in sorted(current_assertions, key=lambda value: (value.semantic_key, value.id))
    )
    calculation_lines = _calculation_lines(
        db,
        calculation=calculation,
        all_by_id=all_by_id,
        current_assertions=current_assertions,
    )
    try:
        current_snapshot = revision_snapshot(db, revision.id)
    except RuntimeError as exc:
        raise _conflict(f"Revision evidence failed integrity validation: {exc}") from exc
    current_snapshot_hash = hash_payload(current_snapshot)
    approval = _approval(
        db,
        case=case,
        revision=revision,
        calculation=calculation,
        current_snapshot=current_snapshot,
        current_snapshot_hash=current_snapshot_hash,
    )
    from rescue_desk.services.readiness import compute_readiness

    readiness = compute_readiness(db, principal=principal, case_id=case_id)
    if not readiness["reproducible_calculation_exists"]:
        raise _conflict(
            "The selected calculation is not reproducible from current evidence; rerun it"
        )
    blockers = _review_blockers(readiness)
    try:
        packet = ExportPacket(
            case_id=case.id,
            case_name=case.display_name,
            applicant_company=case.applicant_company,
            erp_provider=case.erp_provider,
            case_status=(
                CaseStatus.INTERNAL_PACKET_APPROVED.value
                if approval.status == ApprovalStatus.APPROVED
                else case.status.value
            ),
            revision_id=revision.id,
            revision_number=revision.number,
            prepared_at=approval.prepared_at,
            prepared_by=approval.prepared_by_name,
            as_of_date=calculation.as_of_date,
            approval_status=approval.status,
            approved_by=approval.approved_by_name,
            citations=tuple(sorted(citations_by_id.values(), key=lambda value: value.citation_id)),
            facts=facts,
            calculation_engine_version=calculation.engine_version,
            calculation_input_hash=calculation.input_hash,
            calculation_result_hash=calculation.result_hash,
            calculation_lines=calculation_lines,
            currency_scenarios=_scenarios(calculation),
            blockers=blockers,
            assumptions=tuple(str(value) for value in calculation.assumptions),
        )
    except ExportContractError as exc:
        raise _conflict(f"Export snapshot is inconsistent: {exc}") from exc
    return PreparedExport(
        packet=packet,
        calculation=calculation,
        revision_snapshot_sha256=current_snapshot_hash,
        context=approval,
    )


def create_export(
    db: Session,
    *,
    principal: Principal,
    case_id: str,
    kind: ExportKind,
    calculation_id: str | None,
    correlation_id: str,
) -> ExportArtifact:
    # The case row is the lifecycle mutex shared by transitions/reopen, fee and
    # evidence mutations, and export creation.  Keep it through rendering and
    # artifact insertion so an old-revision packet cannot become current after a
    # concurrent reopen, and same-kind races have one deterministic winner.
    case = get_case(db, principal, case_id, for_update=True)
    prepared = build_export_packet(
        db,
        principal=principal,
        case_id=case_id,
        calculation_id=calculation_id,
        locked_case=case,
    )
    packet = prepared.packet
    calculation = prepared.calculation
    packet_kind = _packet_kind(kind)
    try:
        rendered = _render(packet_kind, packet)
    except ExportContractError as exc:
        raise _conflict(f"Export rendering failed validation: {exc}") from exc
    artifact_sha256 = hashlib.sha256(rendered).hexdigest()
    artifact_id = str(uuid.uuid4())
    stored_filename = f"{artifact_id}-{kind.value}{extension_for(packet_kind)}"
    target = _artifact_path(stored_filename)
    _write_atomic(target, rendered)
    register_rollback_path(db, target)
    artifact = ExportArtifact(
        id=artifact_id,
        case_revision_id=packet.revision_id,
        calculation_id=calculation.id,
        kind=kind,
        stored_filename=stored_filename,
        sha256=artifact_sha256,
        packet_snapshot_sha256=packet.snapshot_hash,
        revision_snapshot_sha256=prepared.revision_snapshot_sha256,
        approval_status=packet.approval_status.value,
        approval_case_version=prepared.context.approval_case_version,
        approved_by=prepared.context.approved_by_user_id,
        prepared_at=prepared.context.prepared_at,
        prepared_by=prepared.context.prepared_by_user_id,
        generator_version=GENERATOR_VERSION,
        created_by=principal.user_id,
    )
    try:
        retire_revision_exports(
            db,
            principal=principal,
            revision_id=packet.revision_id,
            correlation_id=correlation_id,
            reason=f"Replaced by export {artifact.id}",
            kinds={kind},
        )
        db.add(artifact)
        db.flush()
        append_audit_event(
            db,
            organization_id=principal.organization_id,
            actor_id=principal.user_id,
            action="export.created",
            object_type="export_artifact",
            object_id=artifact.id,
            correlation_id=correlation_id,
            after={
                "case_id": case_id,
                "case_revision_id": packet.revision_id,
                "calculation_id": calculation.id,
                "kind": kind.value,
                "artifact_sha256": artifact_sha256,
                "packet_snapshot_sha256": packet.snapshot_hash,
                "revision_snapshot_sha256": prepared.revision_snapshot_sha256,
                "approval_status": packet.approval_status.value,
                "approval_case_version": prepared.context.approval_case_version,
                "approved_by": prepared.context.approved_by_user_id,
                "prepared_at": prepared.context.prepared_at.isoformat(),
                "prepared_by": prepared.context.prepared_by_user_id,
                "generator_version": GENERATOR_VERSION,
                "superseded": False,
                "created_by": principal.user_id,
                "size_bytes": len(rendered),
                "external_delivery": False,
                "disclaimer": DISCLAIMER,
            },
        )
        db.flush()
        db.refresh(artifact)
    except IntegrityError as exc:
        db.rollback()
        target.unlink(missing_ok=True)
        raise _conflict(
            "A concurrent export changed the current artifact; refresh before retrying"
        ) from exc
    except Exception:
        db.rollback()
        target.unlink(missing_ok=True)
        raise
    return artifact


def _case_artifact(
    db: Session,
    *,
    principal: Principal,
    case_id: str,
    artifact_id: str,
) -> ExportArtifact:
    artifact = db.scalar(
        select(ExportArtifact)
        .join(CaseRevision, ExportArtifact.case_revision_id == CaseRevision.id)
        .join(RescueCase, CaseRevision.case_id == RescueCase.id)
        .where(
            ExportArtifact.id == artifact_id,
            RescueCase.id == case_id,
            RescueCase.organization_id == principal.organization_id,
        )
    )
    if artifact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Export not found")
    return artifact


def list_exports(
    db: Session,
    *,
    principal: Principal,
    case_id: str,
) -> list[ExportArtifact]:
    get_case(db, principal, case_id)
    return list(
        db.scalars(
            select(ExportArtifact)
            .join(CaseRevision, ExportArtifact.case_revision_id == CaseRevision.id)
            .where(CaseRevision.case_id == case_id)
            .order_by(ExportArtifact.created_at.desc(), ExportArtifact.id.desc())
        )
    )


def get_export(
    db: Session,
    *,
    principal: Principal,
    case_id: str,
    artifact_id: str,
) -> ExportArtifact:
    return _case_artifact(
        db,
        principal=principal,
        case_id=case_id,
        artifact_id=artifact_id,
    )


def _creation_event(db: Session, artifact_id: str) -> AuditEvent:
    event = db.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.object_type == "export_artifact",
            AuditEvent.object_id == artifact_id,
            AuditEvent.action == "export.created",
        )
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(1)
    )
    if event is None or event.after is None:
        raise _conflict("Export artifact is missing its creation audit snapshot")
    return event


def verify_export_artifact_integrity(db: Session, artifact: ExportArtifact) -> Path:
    """Verify confined bytes and their immutable creation-audit identity."""

    try:
        path = _artifact_path(artifact.stored_filename)
        if not path.is_file():
            raise FileNotFoundError(path)
        artifact_bytes = path.read_bytes()
        size_bytes = path.stat().st_size
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Export artifact is no longer available in runtime storage",
        ) from exc
    actual_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    if actual_sha256 != artifact.sha256:
        raise _conflict("Export artifact failed its SHA-256 integrity check")
    event = _creation_event(db, artifact.id)
    try:
        verify_organization_audit_integrity(db, event.organization_id)
    except AuditIntegrityError as exc:
        raise _conflict("Export artifact audit chain failed its integrity check") from exc
    after = event.after or {}
    legacy_expected = {
        "case_revision_id": artifact.case_revision_id,
        "calculation_id": artifact.calculation_id,
        "kind": artifact.kind.value,
        "artifact_sha256": artifact.sha256,
        "packet_snapshot_sha256": artifact.packet_snapshot_sha256,
        "approval_status": artifact.approval_status,
        "generator_version": artifact.generator_version,
        "size_bytes": size_bytes,
    }
    if any(after.get(key) != value for key, value in legacy_expected.items()):
        raise _conflict("Export artifact metadata differs from its creation audit snapshot")
    if artifact.generator_version != GENERATOR_VERSION:
        # Migration quarantines prior generator versions as historical. Their
        # append-only creation events predate the v2 metadata fields, so validate
        # the complete legacy contract without pretending those fields existed.
        if not artifact.superseded:
            raise _conflict("A legacy export artifact cannot remain current")
        return path
    expected = {
        "revision_snapshot_sha256": artifact.revision_snapshot_sha256,
        "approval_case_version": artifact.approval_case_version,
        "approved_by": artifact.approved_by,
        "prepared_at": _as_utc(artifact.prepared_at).isoformat(),
        "prepared_by": artifact.prepared_by,
        "superseded": False,
        "created_by": artifact.created_by,
    }
    if any(after.get(key) != value for key, value in expected.items()):
        raise _conflict("Export artifact metadata differs from its creation audit snapshot")
    supersede_event = db.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.object_type == "export_artifact",
            AuditEvent.object_id == artifact.id,
            AuditEvent.action == "export.superseded",
        )
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(1)
    )
    if artifact.superseded:
        if (
            supersede_event is None
            or supersede_event.after is None
            or supersede_event.after.get("superseded") is not True
            or supersede_event.after.get("case_revision_id") != artifact.case_revision_id
            or supersede_event.after.get("kind") != artifact.kind.value
        ):
            raise _conflict("Export supersession differs from its lifecycle audit snapshot")
    elif supersede_event is not None:
        raise _conflict("Export lifecycle audit says the artifact was superseded")
    return path


def export_metadata(db: Session, artifact: ExportArtifact) -> dict[str, Any]:
    path = verify_export_artifact_integrity(db, artifact)
    event = _creation_event(db, artifact.id)
    after = event.after or {}
    packet_kind = _packet_kind(artifact.kind)
    return {
        "id": artifact.id,
        "case_revision_id": artifact.case_revision_id,
        "calculation_id": artifact.calculation_id,
        "kind": artifact.kind,
        "sha256": artifact.sha256,
        "packet_snapshot_sha256": artifact.packet_snapshot_sha256,
        "approval_status": artifact.approval_status,
        "generator_version": artifact.generator_version,
        "superseded": artifact.superseded,
        "content_type": content_type_for(packet_kind),
        "filename": (
            f"rescuedesk-{artifact.kind.value}-{artifact.id[:8]}{extension_for(packet_kind)}"
        ),
        "size_bytes": path.stat().st_size,
        "download_url": f"/v1/cases/{after.get('case_id')}/exports/{artifact.id}/download",
        "disclaimer": DISCLAIMER,
        "created_at": artifact.created_at,
    }


def prepare_download(
    db: Session,
    *,
    principal: Principal,
    case_id: str,
    artifact_id: str,
    correlation_id: str,
) -> ExportDownload:
    # Downloads share the same case-row lifecycle mutex as export creation and
    # every packet-material mutation.  This prevents a current artifact from
    # being retired after the freshness check but before its bytes are returned.
    case = get_case(db, principal, case_id, for_update=True)
    artifact = _case_artifact(
        db,
        principal=principal,
        case_id=case_id,
        artifact_id=artifact_id,
    )
    if artifact.superseded:
        raise _conflict("Retired export artifacts cannot be downloaded")
    prepared = build_export_packet(
        db,
        principal=principal,
        case_id=case_id,
        calculation_id=artifact.calculation_id,
        locked_case=case,
    )
    if (
        prepared.packet.snapshot_hash != artifact.packet_snapshot_sha256
        or prepared.revision_snapshot_sha256 != artifact.revision_snapshot_sha256
    ):
        raise _conflict(
            "The current export no longer matches reproducible case state; regenerate it"
        )
    metadata = export_metadata(db, artifact)
    path = verify_export_artifact_integrity(db, artifact)
    actual_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual_sha256 != artifact.sha256:
        raise _conflict("Export artifact failed its SHA-256 integrity check")
    append_audit_event(
        db,
        organization_id=principal.organization_id,
        actor_id=principal.user_id,
        action="export.downloaded",
        object_type="export_artifact",
        object_id=artifact.id,
        correlation_id=correlation_id,
        after={
            "case_id": case_id,
            "artifact_sha256": artifact.sha256,
            "packet_snapshot_sha256": metadata["packet_snapshot_sha256"],
            "external_delivery": False,
        },
    )
    db.commit()
    return ExportDownload(
        artifact=artifact,
        path=path,
        content_type=str(metadata["content_type"]),
        download_name=str(metadata["filename"]),
        packet_snapshot_sha256=str(metadata["packet_snapshot_sha256"]),
    )
