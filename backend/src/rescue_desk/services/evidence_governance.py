"""Shared review-governance checks for readiness and immutable exports."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from rescue_desk.domain.assertions import (
    SemanticValueError,
    canonical_assertion_value,
    correction_requires_assumption,
)
from rescue_desk.models import (
    AssertionReviewState,
    AssertionSource,
    ContractAssertion,
    ReviewDecision,
    ReviewDecisionType,
    User,
)


@dataclass(frozen=True, slots=True)
class ReviewGovernance:
    decision: ReviewDecision
    source_assertion: ContractAssertion
    assumption_required: bool


@dataclass(frozen=True, slots=True)
class AssertionResponseProvenance:
    assumption: bool
    review_reason: str | None
    reviewed_by: str | None
    evidence_basis: str


def _latest_matching_decision(
    assertion: ContractAssertion,
    decision_type: ReviewDecisionType,
) -> ReviewDecision | None:
    matching = [item for item in assertion.decisions if item.decision == decision_type]
    if not matching:
        return None
    return max(matching, key=lambda item: (item.created_at, item.id))


def correction_lineage_requirement(
    assertion: ContractAssertion,
    *,
    all_by_id: dict[str, ContractAssertion],
    _visited: frozenset[str] = frozenset(),
) -> bool | None:
    """Return whether correction ancestry requires an assumption, or None if invalid."""

    if assertion.supersedes_assertion_id is None:
        return False
    if assertion.id in _visited:
        return None
    source = all_by_id.get(assertion.supersedes_assertion_id)
    if (
        source is None
        or source.case_revision_id != assertion.case_revision_id
        or source.semantic_key != assertion.semantic_key
    ):
        return None
    decision = _latest_matching_decision(source, ReviewDecisionType.CORRECT)
    # Extraction conflict resolution and human correction deliberately share the
    # same immutable supersession pointer.  A derived conflict is an extractor
    # merge edge, however, so its predecessor correctly has no CORRECT decision.
    # Traverse through that edge while still validating every earlier human
    # correction.  Keep this predicate narrow so changing a HUMAN row's source or
    # omitting its audited decision cannot launder an assumed correction.
    if (
        assertion.source == AssertionSource.DERIVED
        and assertion.extraction_run_id is not None
        and assertion.normalized_value.get("type") == "conflict"
    ):
        if decision is not None:
            return None
        return correction_lineage_requirement(
            source,
            all_by_id=all_by_id,
            _visited=_visited | {assertion.id},
        )
    if assertion.source != AssertionSource.HUMAN:
        return None
    if decision is None or decision.corrected_value is None:
        return None
    try:
        corrected = canonical_assertion_value(assertion.semantic_key, assertion.normalized_value)
        decided = canonical_assertion_value(assertion.semantic_key, decision.corrected_value)
        changed_here = correction_requires_assumption(
            assertion.semantic_key,
            source.normalized_value,
            corrected,
        )
    except SemanticValueError:
        return None
    inherited = correction_lineage_requirement(
        source,
        all_by_id=all_by_id,
        _visited=_visited | {assertion.id},
    )
    if decided != corrected or inherited is None:
        return None
    required = changed_here or inherited
    if required and not decision.assumption:
        return None
    return required


def review_governance(
    assertion: ContractAssertion,
    *,
    all_by_id: dict[str, ContractAssertion],
) -> ReviewGovernance | None:
    """Return the audited decision governing a reviewed assertion, or fail closed.

    Corrected assertions store the decision on the immediate predecessor.  A
    changed canonical value is governed only when that decision explicitly says
    it is an assumption; this re-check prevents a tampered database row from
    bypassing the API rule at readiness or export time.
    """

    if assertion.review_state == AssertionReviewState.ACCEPTED:
        if assertion.supersedes_assertion_id is not None:
            requirement = correction_lineage_requirement(
                assertion,
                all_by_id=all_by_id,
            )
            source = all_by_id.get(assertion.supersedes_assertion_id)
            if requirement is None or source is None:
                return None
            correction = _latest_matching_decision(source, ReviewDecisionType.CORRECT)
            if correction is None:
                return None
            return ReviewGovernance(
                decision=correction,
                source_assertion=source,
                assumption_required=requirement,
            )
        decision = _latest_matching_decision(assertion, ReviewDecisionType.ACCEPT)
        if decision is None or decision.assumption or decision.corrected_value is not None:
            return None
        try:
            canonical_assertion_value(assertion.semantic_key, assertion.normalized_value)
        except SemanticValueError:
            return None
        return ReviewGovernance(
            decision=decision,
            source_assertion=assertion,
            assumption_required=False,
        )

    if assertion.review_state == AssertionReviewState.REJECTED:
        decision = _latest_matching_decision(assertion, ReviewDecisionType.REJECT)
        if decision is None or decision.assumption or decision.corrected_value is not None:
            return None
        return ReviewGovernance(
            decision=decision,
            source_assertion=assertion,
            assumption_required=False,
        )

    if assertion.review_state != AssertionReviewState.CORRECTED:
        return None
    if assertion.supersedes_assertion_id is None:
        return None
    source = all_by_id.get(assertion.supersedes_assertion_id)
    if source is None or source.semantic_key != assertion.semantic_key:
        return None
    decision = _latest_matching_decision(source, ReviewDecisionType.CORRECT)
    if decision is None or decision.corrected_value is None:
        return None
    assumption_required = correction_lineage_requirement(
        assertion,
        all_by_id=all_by_id,
    )
    if assumption_required is None:
        return None
    return ReviewGovernance(
        decision=decision,
        source_assertion=source,
        assumption_required=assumption_required,
    )


def assertion_response_provenance(
    db: Session,
    assertion: ContractAssertion,
) -> AssertionResponseProvenance:
    assertions = list(
        db.scalars(
            select(ContractAssertion)
            .options(selectinload(ContractAssertion.decisions))
            .where(ContractAssertion.case_revision_id == assertion.case_revision_id)
            .execution_options(populate_existing=True)
        )
    )
    all_by_id = {item.id: item for item in assertions}
    current = all_by_id.get(assertion.id, assertion)
    governance = review_governance(current, all_by_id=all_by_id)
    if governance is None:
        basis = (
            "invalid_review"
            if current.review_state
            in {
                AssertionReviewState.ACCEPTED,
                AssertionReviewState.CORRECTED,
                AssertionReviewState.REJECTED,
            }
            else "pending_review"
        )
        return AssertionResponseProvenance(
            assumption=False,
            review_reason=None,
            reviewed_by=None,
            evidence_basis=basis,
        )
    reviewer = db.get(User, governance.decision.reviewer_id)
    if current.review_state == AssertionReviewState.REJECTED:
        basis = "rejected_source"
    elif governance.decision.assumption:
        basis = "explicit_assumption"
    else:
        basis = "source_evidence"
    return AssertionResponseProvenance(
        assumption=governance.decision.assumption,
        review_reason=governance.decision.reason,
        reviewed_by=reviewer.display_name if reviewer else "Unknown reviewer",
        evidence_basis=basis,
    )
