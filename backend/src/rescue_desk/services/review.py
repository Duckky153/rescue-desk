from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from rescue_desk.auth import Principal
from rescue_desk.domain.assertions import (
    SemanticValueError,
    assertion_date_value,
    canonical_assertion_display,
    canonical_assertion_value,
    correction_requires_assumption,
    fee_money_evidence_matches,
)
from rescue_desk.domain.evidence import EvidenceValidationError, verify_persisted_evidence_span
from rescue_desk.domain.money import MAX_MINOR_UNITS
from rescue_desk.models import (
    AssertionReviewState,
    AssertionSource,
    CaseRevision,
    ContractAssertion,
    DocumentPage,
    EvidenceSpan,
    FeeObligation,
    RescueCase,
    ReviewDecision,
    ReviewDecisionType,
)
from rescue_desk.schemas import AssertionReviewRequest, FeeCreate, FeeSupersedeRequest
from rescue_desk.services.audit import append_audit_event
from rescue_desk.services.cases import current_revision, get_case, require_evidence_mutable
from rescue_desk.services.evidence_governance import (
    correction_lineage_requirement,
    review_governance,
)
from rescue_desk.services.lifecycle import retire_revision_exports


def list_assertions(db: Session, *, principal: Principal, case_id: str) -> list[ContractAssertion]:
    case = get_case(db, principal, case_id)
    revision = current_revision(case)
    return list(
        db.scalars(
            select(ContractAssertion)
            .options(selectinload(ContractAssertion.evidence).selectinload(EvidenceSpan.page))
            .where(
                ContractAssertion.case_revision_id == revision.id,
                ContractAssertion.is_current.is_(True),
            )
            .order_by(ContractAssertion.semantic_key)
        )
    )


def review_assertion(
    db: Session,
    *,
    principal: Principal,
    assertion_id: str,
    payload: AssertionReviewRequest,
    correlation_id: str,
) -> ContractAssertion:
    case_id = db.scalar(
        select(RescueCase.id)
        .join(CaseRevision, CaseRevision.case_id == RescueCase.id)
        .join(ContractAssertion, ContractAssertion.case_revision_id == CaseRevision.id)
        .where(
            ContractAssertion.id == assertion_id,
            RescueCase.organization_id == principal.organization_id,
        )
    )
    if case_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assertion not found")
    # Every same-case mutation takes the case lifecycle mutex before child rows.
    # This matches fee creation, document processing, transitions, and exports.
    case = get_case(db, principal, case_id, for_update=True)
    revision = current_revision(case)
    require_evidence_mutable(case)
    assertion = db.scalar(
        select(ContractAssertion)
        .options(
            selectinload(ContractAssertion.evidence)
            .selectinload(EvidenceSpan.page)
            .selectinload(DocumentPage.document)
        )
        .where(ContractAssertion.id == assertion_id, ContractAssertion.is_current.is_(True))
        .with_for_update()
    )
    if assertion is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assertion not found")
    if assertion.case_revision_id != revision.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A prior revision is immutable; refresh to review the current revision",
        )
    if assertion.version != payload.expected_assertion_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Assertion changed; refresh before retrying",
                "current_version": assertion.version,
            },
        )
    if payload.decision in {
        ReviewDecisionType.ACCEPT,
        ReviewDecisionType.REJECT,
    } and assertion.review_state in {
        AssertionReviewState.ACCEPTED,
        AssertionReviewState.CORRECTED,
        AssertionReviewState.REJECTED,
    }:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This assertion already has a final review decision; record a correction instead"
            ),
        )
    before = {
        "display_value": assertion.display_value,
        "normalized_value": assertion.normalized_value,
        "review_state": assertion.review_state.value,
        "version": assertion.version,
    }
    corrected_value: dict[str, object] | None = None
    corrected_display_value: str | None = None
    if payload.decision != ReviewDecisionType.CORRECT and payload.assumption:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Only a correction can be recorded as an explicit assumption",
        )
    if payload.decision == ReviewDecisionType.CORRECT:
        if payload.corrected_value is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="A correction requires a normalized value",
            )
        if not assertion.evidence and not payload.assumption:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Correction requires evidence or an explicitly labeled assumption",
            )
        try:
            corrected_value = canonical_assertion_value(
                assertion.semantic_key, payload.corrected_value
            )
            corrected_display_value = canonical_assertion_display(
                assertion.semantic_key, corrected_value
            )
            requires_assumption = correction_requires_assumption(
                assertion.semantic_key,
                assertion.normalized_value,
                corrected_value,
            )
            lineage_assertions = list(
                db.scalars(
                    select(ContractAssertion)
                    .options(selectinload(ContractAssertion.decisions))
                    .where(ContractAssertion.case_revision_id == revision.id)
                )
            )
            lineage_by_id = {item.id: item for item in lineage_assertions}
            inherited_requirement = correction_lineage_requirement(
                lineage_by_id.get(assertion.id, assertion),
                all_by_id=lineage_by_id,
            )
            if inherited_requirement is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="The assertion correction lineage is invalid and cannot be extended",
                )
            requires_assumption = requires_assumption or inherited_requirement
            if requires_assumption and not payload.assumption:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=(
                        "A correction that changes or descends from an assumed canonical value "
                        "must be recorded as an explicit assumption"
                    ),
                )
        except SemanticValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Corrected value does not match {assertion.semantic_key}: {exc}",
            ) from exc
    elif payload.decision == ReviewDecisionType.ACCEPT:
        try:
            canonical_assertion_value(assertion.semantic_key, assertion.normalized_value)
        except SemanticValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"The proposed value does not match {assertion.semantic_key}; "
                    "record a typed correction instead"
                ),
            ) from exc
        has_exact_active_evidence = False
        for evidence in assertion.evidence:
            if evidence.page.document.superseded:
                continue
            try:
                verify_persisted_evidence_span(
                    page_text=evidence.page.text,
                    page_text_sha256=evidence.page.text_sha256,
                    quote=evidence.quote,
                    quote_sha256=evidence.quote_sha256,
                    char_start=evidence.char_start,
                    char_end=evidence.char_end,
                )
            except EvidenceValidationError:
                continue
            has_exact_active_evidence = True
            break
        if not has_exact_active_evidence:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="An accepted assertion requires exact evidence from an active document",
            )
    decision = ReviewDecision(
        assertion_id=assertion.id,
        decision=payload.decision,
        reviewer_id=principal.user_id,
        reason=payload.reason,
        corrected_value=corrected_value,
        assumption=payload.assumption,
    )
    db.add(decision)
    if payload.decision == ReviewDecisionType.CORRECT:
        assert corrected_value is not None
        assert corrected_display_value is not None
        assertion.is_current = False
        corrected = ContractAssertion(
            case_revision_id=assertion.case_revision_id,
            # The value is human-governed, but the original extraction run remains
            # relevant provenance for the quoted source proposal.
            extraction_run_id=assertion.extraction_run_id,
            semantic_key=assertion.semantic_key,
            raw_value=corrected_display_value,
            normalized_value=corrected_value,
            display_value=corrected_display_value,
            source=AssertionSource.HUMAN,
            confidence=1,
            review_state=AssertionReviewState.CORRECTED,
            version=assertion.version + 1,
            is_current=True,
            supersedes_assertion_id=assertion.id,
            evidence=list(assertion.evidence),
        )
        db.add(corrected)
        db.flush()
        result = corrected
    else:
        assertion.review_state = (
            AssertionReviewState.ACCEPTED
            if payload.decision == ReviewDecisionType.ACCEPT
            else AssertionReviewState.REJECTED
        )
        assertion.version += 1
        result = assertion
    append_audit_event(
        db,
        organization_id=principal.organization_id,
        actor_id=principal.user_id,
        action="assertion.reviewed",
        object_type="contract_assertion",
        object_id=result.id,
        correlation_id=correlation_id,
        before=before,
        after={
            "display_value": result.display_value,
            "normalized_value": result.normalized_value,
            "review_state": result.review_state.value,
            "version": result.version,
            "decision": payload.decision.value,
            "assumption": payload.assumption,
        },
    )
    retire_revision_exports(
        db,
        principal=principal,
        revision_id=revision.id,
        correlation_id=correlation_id,
        reason=f"Assertion {result.id} review state changed",
    )
    db.flush()
    return (
        db.scalar(
            select(ContractAssertion)
            .options(selectinload(ContractAssertion.evidence).selectinload(EvidenceSpan.page))
            .where(ContractAssertion.id == result.id)
        )
        or result
    )


def add_fee(
    db: Session,
    *,
    principal: Principal,
    case_id: str,
    payload: FeeCreate,
    correlation_id: str,
) -> FeeObligation:
    case = get_case(db, principal, case_id, for_update=True)
    require_evidence_mutable(case)
    revision = current_revision(case)
    source_assertions: list[ContractAssertion] = []
    if payload.assertion_ids:
        if len(payload.assertion_ids) != len(set(payload.assertion_ids)):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Source assertion IDs must be unique",
            )
        source_assertions = list(
            db.scalars(
                select(ContractAssertion)
                .options(
                    selectinload(ContractAssertion.evidence)
                    .selectinload(EvidenceSpan.page)
                    .selectinload(DocumentPage.document),
                    selectinload(ContractAssertion.decisions),
                )
                .where(
                    ContractAssertion.case_revision_id == revision.id,
                    ContractAssertion.id.in_(payload.assertion_ids),
                    ContractAssertion.is_current.is_(True),
                )
                .with_for_update()
            )
        )
        if len(source_assertions) != len(set(payload.assertion_ids)):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Every source assertion must be current and belong to this case revision",
            )
    primary_money_assertion_id: str | None = None
    if payload.reviewed:
        accepted = {AssertionReviewState.ACCEPTED, AssertionReviewState.CORRECTED}
        lineage_assertions = list(
            db.scalars(
                select(ContractAssertion)
                .options(selectinload(ContractAssertion.decisions))
                .where(ContractAssertion.case_revision_id == revision.id)
            )
        )
        all_by_id = {item.id: item for item in lineage_assertions}
        if not source_assertions or any(
            item.review_state not in accepted
            or not any(not evidence.page.document.superseded for evidence in item.evidence)
            or review_governance(item, all_by_id=all_by_id) is None
            for item in source_assertions
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "A reviewed obligation requires current, accepted or corrected, "
                    "governed assertions with active exact-page evidence"
                ),
            )
        supported_dates = {
            value
            for value in (payload.service_start, payload.service_end, payload.obligation_date)
            if value is not None
        }
        matching_money_assertion_ids: list[str] = []
        for item in source_assertions:
            if fee_money_evidence_matches(
                semantic_key=item.semantic_key,
                normalized_value=item.normalized_value,
                category=payload.category,
                amount_minor=payload.amount_minor,
                currency=payload.currency,
                billing_cadence=payload.billing_cadence,
            ):
                matching_money_assertion_ids.append(item.id)
                continue
            linked_date = assertion_date_value(item.semantic_key, item.normalized_value)
            if linked_date is not None and linked_date in supported_dates:
                continue
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"Assertion {item.semantic_key} is not relevant to the obligation's "
                    "typed amount, currency, cadence, or supplied dates"
                ),
            )
        if len(matching_money_assertion_ids) != 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "A reviewed obligation requires exactly one compatible fee assertion whose "
                    "typed amount, currency, category, and cadence match exactly"
                ),
            )
        primary_money_assertion_id = matching_money_assertion_ids[0]
        assert primary_money_assertion_id is not None
        source_by_id = {item.id: item for item in source_assertions}
        lineage_ids = {primary_money_assertion_id}
        cursor = source_by_id.get(primary_money_assertion_id)
        while cursor is not None and cursor.supersedes_assertion_id is not None:
            ancestor_id = cursor.supersedes_assertion_id
            if ancestor_id in lineage_ids:
                break
            lineage_ids.add(ancestor_id)
            cursor = db.get(ContractAssertion, ancestor_id)
        existing = db.scalar(
            select(FeeObligation)
            .where(
                FeeObligation.case_revision_id == revision.id,
                FeeObligation.primary_money_assertion_id.in_(lineage_ids),
                FeeObligation.superseded.is_(False),
            )
            .with_for_update()
        )
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "This money assertion already substantiates an active obligation; "
                    "supersede the accidental obligation before reusing it"
                ),
            )
    if (
        payload.service_start
        and payload.service_end
        and payload.service_end <= payload.service_start
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Service end must be after service start",
        )
    active_total = db.scalar(
        select(func.coalesce(func.sum(FeeObligation.amount_minor), 0)).where(
            FeeObligation.case_revision_id == revision.id,
            FeeObligation.currency == payload.currency,
            FeeObligation.superseded.is_(False),
        )
    )
    if not isinstance(active_total, int):
        active_total = int(active_total or 0)
    if active_total + payload.amount_minor > MAX_MINOR_UNITS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Active {payload.currency} obligations would exceed the exact supported "
                "aggregate minor-unit range"
            ),
        )
    fee = FeeObligation(
        case_revision_id=revision.id,
        primary_money_assertion_id=primary_money_assertion_id,
        **payload.model_dump(),
    )
    db.add(fee)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This money assertion was concurrently assigned to another active obligation; "
                "refresh before retrying"
            ),
        ) from exc
    append_audit_event(
        db,
        organization_id=principal.organization_id,
        actor_id=principal.user_id,
        action="fee.created",
        object_type="fee_obligation",
        object_id=fee.id,
        correlation_id=correlation_id,
        after={
            "case_revision_id": revision.id,
            "category": fee.category.value,
            "amount_minor": fee.amount_minor,
            "currency": fee.currency,
            "reviewed": fee.reviewed,
            "assertion_ids": fee.assertion_ids,
            "primary_money_assertion_id": fee.primary_money_assertion_id,
            "evidence_basis": "typed_matching_assertions" if fee.reviewed else "unreviewed",
        },
    )
    retire_revision_exports(
        db,
        principal=principal,
        revision_id=revision.id,
        correlation_id=correlation_id,
        reason=f"Fee obligation {fee.id} was added",
    )
    db.flush()
    db.refresh(fee)
    return fee


def list_fees(db: Session, *, principal: Principal, case_id: str) -> list[FeeObligation]:
    case = get_case(db, principal, case_id)
    revision = current_revision(case)
    return list(
        db.scalars(
            select(FeeObligation)
            .where(FeeObligation.case_revision_id == revision.id)
            .order_by(FeeObligation.created_at, FeeObligation.id)
        )
    )


def supersede_fee(
    db: Session,
    *,
    principal: Principal,
    case_id: str,
    fee_id: str,
    payload: FeeSupersedeRequest,
    correlation_id: str,
) -> FeeObligation:
    case = get_case(db, principal, case_id, for_update=True)
    require_evidence_mutable(case)
    if case.version != payload.expected_case_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Case changed; refresh before retrying",
                "current_version": case.version,
            },
        )
    revision = current_revision(case)
    fee = db.scalar(
        select(FeeObligation)
        .join(CaseRevision, FeeObligation.case_revision_id == CaseRevision.id)
        .where(FeeObligation.id == fee_id, CaseRevision.case_id == case.id)
        .with_for_update()
    )
    if fee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fee not found")
    if fee.case_revision_id != revision.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A fee from a prior revision is immutable",
        )
    if fee.superseded:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Fee is already superseded",
        )
    now = datetime.now(UTC)
    before = {
        "superseded": False,
        "case_version": case.version,
        "primary_money_assertion_id": fee.primary_money_assertion_id,
    }
    fee.superseded = True
    fee.superseded_at = now
    fee.superseded_by = principal.user_id
    fee.supersede_reason = payload.reason
    case.version += 1
    append_audit_event(
        db,
        organization_id=principal.organization_id,
        actor_id=principal.user_id,
        action="fee.superseded",
        object_type="fee_obligation",
        object_id=fee.id,
        correlation_id=correlation_id,
        before=before,
        after={
            "superseded": True,
            "superseded_at": now.isoformat(),
            "superseded_by": principal.user_id,
            "supersede_reason": payload.reason,
            "case_version": case.version,
            "primary_money_assertion_id": fee.primary_money_assertion_id,
        },
    )
    retire_revision_exports(
        db,
        principal=principal,
        revision_id=revision.id,
        correlation_id=correlation_id,
        reason=f"Fee obligation {fee.id} was superseded",
    )
    db.flush()
    db.refresh(fee)
    return fee
