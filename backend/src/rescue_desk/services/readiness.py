from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from rescue_desk.auth import Principal
from rescue_desk.domain.assertions import (
    assertion_date_value,
    canonical_assertion_display,
    fee_money_evidence_matches,
    semantic_value_is_valid,
)
from rescue_desk.domain.calculations import AnalysisResult
from rescue_desk.domain.evidence import EvidenceValidationError, verify_persisted_evidence_span
from rescue_desk.models import (
    AssertionReviewState,
    CalculationRun,
    ContractAssertion,
    ContractDocument,
    DocumentPage,
    EvidenceSpan,
    FeeCategory,
    FeeObligation,
    FindingSeverity,
    FindingStatus,
    ProcessingStatus,
    ReadinessFinding,
)
from rescue_desk.services.calculations import (
    CalculationIntegrityError,
    verify_calculation_integrity,
)
from rescue_desk.services.cases import current_revision, get_case

DISCLAIMER = (
    "Demonstration only. This is an internal evidence-review aid, not legal, accounting, "
    "or financial advice; it does not determine Rescue Fund eligibility or approval and is "
    "not affiliated with Entry Inc."
)


def _finding(code: str, title: str, detail: str, severity: FindingSeverity) -> dict[str, Any]:
    return {
        "id": f"computed:{code}",
        "code": code,
        "title": title,
        "detail": detail,
        "severity": severity,
        "status": FindingStatus.OPEN,
        "version": 1,
        "resolution_reason": None,
        "created_at": "1970-01-01T00:00:00Z",
        "resolved_at": None,
    }


def compute_readiness(db: Session, *, principal: Principal, case_id: str) -> dict[str, Any]:
    case = get_case(db, principal, case_id)
    revision = current_revision(case)
    documents = list(
        db.scalars(
            select(ContractDocument).where(
                ContractDocument.case_revision_id == revision.id,
                ContractDocument.superseded.is_(False),
            )
        )
    )
    all_assertions = list(
        db.scalars(
            select(ContractAssertion)
            .options(
                selectinload(ContractAssertion.evidence)
                .selectinload(EvidenceSpan.page)
                .selectinload(DocumentPage.document),
                selectinload(ContractAssertion.decisions),
            )
            .where(ContractAssertion.case_revision_id == revision.id)
            .execution_options(populate_existing=True)
        )
    )
    assertions = [item for item in all_assertions if item.is_current]
    fees = list(
        db.scalars(
            select(FeeObligation).where(
                FeeObligation.case_revision_id == revision.id,
                FeeObligation.superseded.is_(False),
            )
        )
    )
    calculation = db.scalar(
        select(CalculationRun)
        .where(CalculationRun.case_revision_id == revision.id)
        .order_by(CalculationRun.created_at.desc(), CalculationRun.id.desc())
        .limit(1)
    )
    persisted = list(
        db.scalars(select(ReadinessFinding).where(ReadinessFinding.case_revision_id == revision.id))
    )
    findings: list[Any] = list(persisted)
    if not documents:
        findings.append(
            _finding(
                "document.missing",
                "Contract missing",
                "Upload a contract.",
                FindingSeverity.BLOCKING,
            )
        )
    for document in documents:
        from rescue_desk.services.documents import verify_document_integrity

        try:
            verify_document_integrity(document)
        except RuntimeError:
            findings.append(
                _finding(
                    f"document.{document.id}.integrity_failed",
                    "Document integrity check failed",
                    f"{document.original_filename} no longer matches its intake SHA-256.",
                    FindingSeverity.BLOCKING,
                )
            )
        if document.processing_status != ProcessingStatus.PROCESSED:
            findings.append(
                _finding(
                    f"document.{document.id}.not_processed",
                    "Document processing incomplete",
                    f"{document.original_filename} is {document.processing_status.value}.",
                    FindingSeverity.BLOCKING,
                )
            )
    current_by_key = {item.semantic_key: item for item in assertions}
    accepted = {AssertionReviewState.ACCEPTED, AssertionReviewState.CORRECTED}
    all_by_id = {item.id: item for item in all_assertions}

    def has_active_evidence(assertion: ContractAssertion) -> bool:
        for item in assertion.evidence:
            if item.page.document.superseded:
                continue
            try:
                verify_persisted_evidence_span(
                    page_text=item.page.text,
                    page_text_sha256=item.page.text_sha256,
                    quote=item.quote,
                    quote_sha256=item.quote_sha256,
                    char_start=item.char_start,
                    char_end=item.char_end,
                )
            except EvidenceValidationError:
                continue
            return True
        return False

    def reviewed_semantic_assertion(assertion: ContractAssertion | None) -> bool:
        from rescue_desk.services.evidence_governance import review_governance

        if assertion is None:
            return False
        try:
            display_is_canonical = assertion.display_value == canonical_assertion_display(
                assertion.semantic_key, assertion.normalized_value
            )
        except ValueError:
            display_is_canonical = False
        return bool(
            assertion.review_state in accepted
            and has_active_evidence(assertion)
            and semantic_value_is_valid(assertion.semantic_key, assertion.normalized_value)
            and display_is_canonical
            and review_governance(assertion, all_by_id=all_by_id) is not None
        )

    for key, title in (("contract_end_date", "Contract end date"),):
        item = current_by_key.get(key)
        if not reviewed_semantic_assertion(item):
            findings.append(
                _finding(
                    f"assertion.{key}.unreviewed",
                    f"{title} needs review",
                    f"A reviewer must confirm or correct the {title.lower()}.",
                    FindingSeverity.BLOCKING,
                )
            )
    for assertion in assertions:
        if not has_active_evidence(assertion):
            findings.append(
                _finding(
                    f"assertion.{assertion.id}.no_evidence",
                    "Assertion has no active evidence",
                    f"{assertion.semantic_key} cannot be accepted without an exact source span.",
                    FindingSeverity.BLOCKING,
                )
            )
        if assertion.review_state in accepted and not semantic_value_is_valid(
            assertion.semantic_key, assertion.normalized_value
        ):
            findings.append(
                _finding(
                    f"assertion.{assertion.id}.invalid_value",
                    "Assertion value is invalid",
                    (
                        f"{assertion.semantic_key} does not match its governed typed value "
                        "contract and must be corrected."
                    ),
                    FindingSeverity.BLOCKING,
                )
            )
        if assertion.review_state in accepted and not reviewed_semantic_assertion(assertion):
            findings.append(
                _finding(
                    f"assertion.{assertion.id}.governance_invalid",
                    "Assertion review record is invalid",
                    (
                        f"{assertion.semantic_key} lacks a matching audited review decision, "
                        "or a changed correction was not labeled as an explicit assumption."
                    ),
                    FindingSeverity.BLOCKING,
                )
            )
        if assertion.review_state in {
            AssertionReviewState.PROPOSED,
            AssertionReviewState.CONFLICTING,
        }:
            findings.append(
                _finding(
                    f"assertion.{assertion.id}.pending",
                    "Assertion requires review",
                    f"{assertion.semantic_key} remains {assertion.review_state.value}.",
                    FindingSeverity.BLOCKING,
                )
            )

    def current_descendant(assertion_id: str) -> ContractAssertion | None:
        source = all_by_id.get(assertion_id)
        if source is None:
            return None
        if source.is_current:
            return source
        for candidate in assertions:
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

    def fee_has_reviewed_citation(fee: FeeObligation) -> bool:
        descendants = [current_descendant(item) for item in fee.assertion_ids]
        if not descendants:
            return False
        supported_dates = {
            value
            for value in (fee.service_start, fee.service_end, fee.obligation_date)
            if value is not None
        }
        matching_money_assertion_id: str | None = None
        for item in descendants:
            if item is None or not reviewed_semantic_assertion(item):
                return False
            if fee_money_evidence_matches(
                semantic_key=item.semantic_key,
                normalized_value=item.normalized_value,
                category=fee.category,
                amount_minor=fee.amount_minor,
                currency=fee.currency,
                billing_cadence=fee.billing_cadence,
            ):
                if matching_money_assertion_id is not None:
                    return False
                matching_money_assertion_id = item.id
                continue
            linked_date = assertion_date_value(item.semantic_key, item.normalized_value)
            if linked_date is None or linked_date not in supported_dates:
                return False
        primary = (
            current_descendant(fee.primary_money_assertion_id)
            if fee.primary_money_assertion_id is not None
            else None
        )
        return bool(
            fee.reviewed and primary is not None and matching_money_assertion_id == primary.id
        )

    for fee in fees:
        if not fee_has_reviewed_citation(fee):
            findings.append(
                _finding(
                    f"fee.{fee.id}.citation_missing",
                    "Obligation needs reviewed evidence",
                    (
                        f"{fee.category.value.replace('_', ' ').title()} must link to a current, "
                        "accepted or corrected assertion with an exact page citation."
                    ),
                    FindingSeverity.BLOCKING,
                )
            )
    reviewed_subscription = any(
        fee.category == FeeCategory.SUBSCRIPTION and fee.reviewed and fee_has_reviewed_citation(fee)
        for fee in fees
    )
    if not reviewed_subscription:
        findings.append(
            _finding(
                "fee.reviewed_subscription_missing",
                "Reviewed subscription obligation missing",
                "At least one subscription fee must be reviewed before analysis.",
                FindingSeverity.BLOCKING,
            )
        )
    calculation_is_reproducible = False
    current_calculation: AnalysisResult | None = None
    if calculation is None:
        findings.append(
            _finding(
                "calculation.missing",
                "Calculation missing",
                "Run a reproducible remaining-subscription analysis.",
                FindingSeverity.BLOCKING,
            )
        )
    else:
        try:
            current_calculation = verify_calculation_integrity(
                db,
                revision_id=revision.id,
                calculation=calculation,
            )
        except CalculationIntegrityError as exc:
            current_calculation = exc.expected
            findings.append(
                _finding(
                    "calculation.stale",
                    "Calculation is stale",
                    (
                        "Run the deterministic calculation again; its active inputs, hashes, "
                        "snapshots, assumptions, or line ledger no longer agree."
                    ),
                    FindingSeverity.BLOCKING,
                )
            )
        else:
            calculation_is_reproducible = True
        for index, detail in enumerate(
            current_calculation.blocking_findings if current_calculation is not None else ()
        ):
            findings.append(
                _finding(
                    f"calculation.blocker.{index}",
                    "Calculation requires review",
                    str(detail),
                    FindingSeverity.BLOCKING,
                )
            )
    open_blockers = sum(
        1
        for item in findings
        if (
            item["severity"] == FindingSeverity.BLOCKING and item["status"] == FindingStatus.OPEN
            if isinstance(item, dict)
            else item.severity == FindingSeverity.BLOCKING and item.status == FindingStatus.OPEN
        )
    )
    mandatory_reviewed = (
        all(reviewed_semantic_assertion(current_by_key.get(key)) for key in ("contract_end_date",))
        and reviewed_subscription
    )
    return {
        "ready_for_internal_review": open_blockers == 0,
        "mandatory_assertions_reviewed": mandatory_reviewed,
        "reproducible_calculation_exists": calculation_is_reproducible,
        "open_blocking_findings": open_blockers,
        "findings": findings,
        "disclaimer": DISCLAIMER,
    }
