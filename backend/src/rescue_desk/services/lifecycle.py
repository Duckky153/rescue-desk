"""Shared lifecycle controls for retiring materialized export snapshots."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from rescue_desk.auth import Principal
from rescue_desk.models import ExportArtifact, ExportKind
from rescue_desk.services.audit import append_audit_event


def retire_revision_exports(
    db: Session,
    *,
    principal: Principal,
    revision_id: str,
    correlation_id: str,
    reason: str,
    kinds: set[ExportKind] | None = None,
) -> list[str]:
    """Soft-retire current artifacts invalidated by a material state change."""

    statement = (
        select(ExportArtifact)
        .where(
            ExportArtifact.case_revision_id == revision_id,
            ExportArtifact.superseded.is_(False),
        )
        .with_for_update()
    )
    if kinds is not None:
        statement = statement.where(ExportArtifact.kind.in_(kinds))
    artifacts = list(db.scalars(statement))
    for artifact in artifacts:
        artifact.superseded = True
        append_audit_event(
            db,
            organization_id=principal.organization_id,
            actor_id=principal.user_id,
            action="export.superseded",
            object_type="export_artifact",
            object_id=artifact.id,
            correlation_id=correlation_id,
            before={
                "superseded": False,
                "case_revision_id": artifact.case_revision_id,
                "kind": artifact.kind.value,
            },
            after={
                "superseded": True,
                "case_revision_id": artifact.case_revision_id,
                "kind": artifact.kind.value,
                "reason": reason,
            },
        )
    return [artifact.id for artifact in artifacts]
