from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from rescue_desk.domain.audit import AuditPayload, calculate_event_hash, verify_audit_chain
from rescue_desk.models import (
    AuditEvent,
    CalculationRun,
    ContractAssertion,
    ContractDocument,
    ExportArtifact,
    FeeObligation,
    Organization,
    ReadinessFinding,
    RescueCase,
)


class AuditIntegrityError(ValueError):
    """The persisted organization audit chain no longer verifies."""


def verify_organization_audit_integrity(db: Session, organization_id: str) -> None:
    events = list(
        db.scalars(
            select(AuditEvent)
            .where(AuditEvent.organization_id == organization_id)
            .order_by(AuditEvent.created_at, AuditEvent.id)
        )
    )
    chain = [
        (
            AuditPayload(
                organization_id=event.organization_id,
                actor_id=event.actor_id,
                action=event.action,
                object_type=event.object_type,
                object_id=event.object_id,
                correlation_id=event.correlation_id,
                before=event.before,
                after=event.after,
                created_at=event.created_at,
                previous_hash=event.previous_hash,
            ),
            event.event_hash,
        )
        for event in events
    ]
    if not verify_audit_chain(chain):
        raise AuditIntegrityError("Organization audit chain failed its integrity check")


def _serialize_organization_appends(db: Session, organization_id: str) -> None:
    """Hold the organization row's write lock until the caller commits.

    Audit events have one hash chain per organization. A no-op update is portable
    across the supported databases: PostgreSQL takes a row lock, while SQLite
    serializes the write transaction. The subsequent tail lookup therefore cannot
    race another append for the same organization.
    """

    db.execute(
        update(Organization)
        .where(Organization.id == organization_id)
        .values(name=Organization.name)
        .execution_options(synchronize_session=False)
    )


def append_audit_event(
    db: Session,
    *,
    organization_id: str,
    actor_id: str,
    action: str,
    object_type: str,
    object_id: str,
    correlation_id: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> AuditEvent:
    _serialize_organization_appends(db, organization_id)
    latest = db.scalar(
        select(AuditEvent)
        .where(AuditEvent.organization_id == organization_id)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(1)
    )
    created_at = datetime.now(UTC)
    if latest is not None:
        latest_created_at = latest.created_at
        if latest_created_at.tzinfo is None:
            latest_created_at = latest_created_at.replace(tzinfo=UTC)
        else:
            latest_created_at = latest_created_at.astimezone(UTC)
        if created_at <= latest_created_at:
            created_at = latest_created_at + timedelta(microseconds=1)
    previous_hash = latest.event_hash if latest else None
    payload = AuditPayload(
        organization_id=organization_id,
        actor_id=actor_id,
        action=action,
        object_type=object_type,
        object_id=object_id,
        correlation_id=correlation_id,
        before=before,
        after=after,
        created_at=created_at,
        previous_hash=previous_hash,
    )
    event = AuditEvent(
        organization_id=organization_id,
        actor_id=actor_id,
        action=action,
        object_type=object_type,
        object_id=object_id,
        correlation_id=correlation_id,
        before=before,
        after=after,
        previous_hash=previous_hash,
        event_hash=calculate_event_hash(payload),
        created_at=created_at,
    )
    db.add(event)
    db.flush([event])
    return event


def list_case_audit_events(
    db: Session,
    *,
    case: RescueCase,
    organization_id: str,
    limit: int = 200,
) -> list[AuditEvent]:
    """Return the canonical audit slice for one tenant-scoped case."""

    try:
        verify_organization_audit_integrity(db, organization_id)
    except AuditIntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Case audit chain failed its integrity check",
        ) from exc

    revision_ids = [revision.id for revision in case.revisions]
    document_ids = list(
        db.scalars(
            select(ContractDocument.id).where(ContractDocument.case_revision_id.in_(revision_ids))
        )
    )
    assertion_ids = list(
        db.scalars(
            select(ContractAssertion.id).where(ContractAssertion.case_revision_id.in_(revision_ids))
        )
    )
    fee_ids = list(
        db.scalars(select(FeeObligation.id).where(FeeObligation.case_revision_id.in_(revision_ids)))
    )
    calculation_ids = list(
        db.scalars(
            select(CalculationRun.id).where(CalculationRun.case_revision_id.in_(revision_ids))
        )
    )
    export_ids = list(
        db.scalars(
            select(ExportArtifact.id).where(ExportArtifact.case_revision_id.in_(revision_ids))
        )
    )
    finding_ids = list(
        db.scalars(
            select(ReadinessFinding.id).where(ReadinessFinding.case_revision_id.in_(revision_ids))
        )
    )
    object_ids = [
        case.id,
        *revision_ids,
        *document_ids,
        *assertion_ids,
        *fee_ids,
        *calculation_ids,
        *export_ids,
        *finding_ids,
    ]
    return list(
        db.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.organization_id == organization_id,
                AuditEvent.object_id.in_(object_ids),
            )
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
            .limit(limit)
        )
    )
