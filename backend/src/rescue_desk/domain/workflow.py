from dataclasses import dataclass

from rescue_desk.models import CaseStatus, Role


class InvalidTransitionError(ValueError):
    pass


@dataclass(frozen=True)
class TransitionContext:
    actor_role: Role
    has_document: bool = False
    extraction_finished: bool = False
    mandatory_assertions_reviewed: bool = False
    has_reproducible_calculation: bool = False
    open_blocking_findings: int = 0
    approved_export_exists: bool = False


ALLOWED_TRANSITIONS: dict[CaseStatus, set[CaseStatus]] = {
    CaseStatus.DRAFT: {CaseStatus.EVIDENCE_REVIEW, CaseStatus.ARCHIVED},
    CaseStatus.EVIDENCE_REVIEW: {
        CaseStatus.READY_FOR_INTERNAL_REVIEW,
        CaseStatus.ARCHIVED,
    },
    CaseStatus.READY_FOR_INTERNAL_REVIEW: {
        CaseStatus.EVIDENCE_REVIEW,
        CaseStatus.INTERNAL_PACKET_APPROVED,
        CaseStatus.ARCHIVED,
    },
    CaseStatus.INTERNAL_PACKET_APPROVED: {
        CaseStatus.EVIDENCE_REVIEW,
        CaseStatus.EXPORTED,
        CaseStatus.ARCHIVED,
    },
    CaseStatus.EXPORTED: {CaseStatus.EVIDENCE_REVIEW, CaseStatus.ARCHIVED},
    CaseStatus.ARCHIVED: set(),
}


def validate_transition(
    current: CaseStatus, target: CaseStatus, context: TransitionContext
) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransitionError(f"Illegal case transition: {current.value} -> {target.value}")
    if (
        target == CaseStatus.EVIDENCE_REVIEW
        and current == CaseStatus.DRAFT
        and (not context.has_document or not context.extraction_finished)
    ):
        raise InvalidTransitionError("Evidence review requires a processed document")
    if target == CaseStatus.INTERNAL_PACKET_APPROVED and context.actor_role not in {
        Role.APPROVER,
        Role.ADMIN,
    }:
        raise InvalidTransitionError("Only an approver can approve the internal packet")
    if target in {
        CaseStatus.READY_FOR_INTERNAL_REVIEW,
        CaseStatus.INTERNAL_PACKET_APPROVED,
    }:
        if not context.mandatory_assertions_reviewed:
            raise InvalidTransitionError("Mandatory assertions must be reviewed")
        if not context.has_reproducible_calculation:
            raise InvalidTransitionError("A reproducible calculation is required")
        if context.open_blocking_findings:
            raise InvalidTransitionError("Blocking findings must be resolved")
    if target == CaseStatus.EXPORTED and not context.approved_export_exists:
        raise InvalidTransitionError("An approved export artifact is required")
