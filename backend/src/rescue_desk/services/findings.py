from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from rescue_desk.auth import Principal
from rescue_desk.models import CaseRevision, FindingStatus, ReadinessFinding, RescueCase
from rescue_desk.schemas import FindingResolutionRequest
from rescue_desk.services.audit import append_audit_event
from rescue_desk.services.cases import current_revision, get_case, require_evidence_mutable
from rescue_desk.services.lifecycle import retire_revision_exports


def resolve_finding(
    db: Session,
    *,
    principal: Principal,
    case_id: str,
    finding_id: str,
    payload: FindingResolutionRequest,
    correlation_id: str,
) -> ReadinessFinding:
    case = get_case(db, principal, case_id, for_update=True)
    require_evidence_mutable(case)
    revision = current_revision(case)
    finding = db.scalar(
        select(ReadinessFinding)
        .join(CaseRevision, ReadinessFinding.case_revision_id == CaseRevision.id)
        .join(RescueCase, CaseRevision.case_id == RescueCase.id)
        .where(
            ReadinessFinding.id == finding_id,
            ReadinessFinding.case_revision_id == revision.id,
            RescueCase.organization_id == principal.organization_id,
        )
        .with_for_update()
    )
    if finding is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Finding not found")
    if finding.version != payload.expected_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Finding changed; refresh before retrying",
                "current_version": finding.version,
            },
        )
    if finding.status != FindingStatus.OPEN:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only an open persisted finding can be resolved or accepted as risk",
        )
    before = {
        "status": finding.status.value,
        "version": finding.version,
        "resolution_reason": finding.resolution_reason,
    }
    finding.status = payload.status
    finding.resolution_reason = payload.reason
    finding.resolved_at = datetime.now(UTC)
    finding.version += 1
    append_audit_event(
        db,
        organization_id=principal.organization_id,
        actor_id=principal.user_id,
        action="finding.disposition_recorded",
        object_type="readiness_finding",
        object_id=finding.id,
        correlation_id=correlation_id,
        before=before,
        after={
            "case_id": case.id,
            "case_revision_id": revision.id,
            "status": finding.status.value,
            "version": finding.version,
            "resolution_reason": finding.resolution_reason,
        },
    )
    retire_revision_exports(
        db,
        principal=principal,
        revision_id=revision.id,
        correlation_id=correlation_id,
        reason=f"Finding {finding.id} disposition changed",
    )
    db.flush()
    db.refresh(finding)
    return finding
