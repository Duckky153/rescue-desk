import pytest

from rescue_desk.domain.workflow import (
    InvalidTransitionError,
    TransitionContext,
    validate_transition,
)
from rescue_desk.models import CaseStatus, Role


def test_processed_document_can_enter_evidence_review() -> None:
    validate_transition(
        CaseStatus.DRAFT,
        CaseStatus.EVIDENCE_REVIEW,
        TransitionContext(actor_role=Role.ANALYST, has_document=True, extraction_finished=True),
    )


def test_ai_or_analyst_cannot_approve_internal_packet() -> None:
    with pytest.raises(InvalidTransitionError, match="Only an approver"):
        validate_transition(
            CaseStatus.READY_FOR_INTERNAL_REVIEW,
            CaseStatus.INTERNAL_PACKET_APPROVED,
            TransitionContext(actor_role=Role.ANALYST),
        )


def test_readiness_requires_review_calculation_and_no_blockers() -> None:
    with pytest.raises(InvalidTransitionError, match="Blocking findings"):
        validate_transition(
            CaseStatus.EVIDENCE_REVIEW,
            CaseStatus.READY_FOR_INTERNAL_REVIEW,
            TransitionContext(
                actor_role=Role.ANALYST,
                mandatory_assertions_reviewed=True,
                has_reproducible_calculation=True,
                open_blocking_findings=1,
            ),
        )


def test_approved_case_can_return_to_review_after_new_evidence() -> None:
    validate_transition(
        CaseStatus.INTERNAL_PACKET_APPROVED,
        CaseStatus.EVIDENCE_REVIEW,
        TransitionContext(actor_role=Role.ANALYST),
    )
