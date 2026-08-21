from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from rescue_desk.api_dependencies import correlation_id, idempotency_key
from rescue_desk.auth import Principal, get_current_principal, require_roles
from rescue_desk.database import get_db
from rescue_desk.idempotency import payload_hash, replay_if_present, store_response
from rescue_desk.models import Role
from rescue_desk.schemas import ExportMetadata, ExportRequest
from rescue_desk.services.exports import (
    create_export,
    export_metadata,
    get_export,
    list_exports,
    prepare_download,
)

router = APIRouter(prefix="/v1/cases", tags=["exports"])

Viewer = Annotated[Principal, Depends(get_current_principal)]
Analyst = Annotated[
    Principal,
    Depends(require_roles(Role.ANALYST, Role.APPROVER, Role.ADMIN)),
]
Database = Annotated[Session, Depends(get_db)]
CorrelationId = Annotated[str, Depends(correlation_id)]
IdempotencyKey = Annotated[str, Depends(idempotency_key)]


@router.post(
    "/{case_id}/exports",
    response_model=ExportMetadata,
    status_code=status.HTTP_201_CREATED,
)
def post_export(
    case_id: str,
    payload: ExportRequest,
    principal: Analyst,
    db: Database,
    correlation: CorrelationId,
    idem_key: IdempotencyKey,
) -> ExportMetadata:
    endpoint = f"POST /v1/cases/{case_id}/exports"
    request_hash = payload_hash(payload.model_dump(mode="json"))
    replay = replay_if_present(
        db,
        organization_id=principal.organization_id,
        endpoint=endpoint,
        key=idem_key,
        request_hash=request_hash,
    )
    if replay is not None:
        return ExportMetadata.model_validate(replay.response_body)
    artifact = create_export(
        db,
        principal=principal,
        case_id=case_id,
        kind=payload.kind,
        calculation_id=payload.calculation_id,
        correlation_id=correlation,
    )
    response = ExportMetadata.model_validate(export_metadata(db, artifact))
    store_response(
        db,
        organization_id=principal.organization_id,
        endpoint=endpoint,
        key=idem_key,
        request_hash=request_hash,
        response_status=status.HTTP_201_CREATED,
        response_body=response.model_dump(mode="json"),
    )
    return response


@router.get("/{case_id}/exports", response_model=list[ExportMetadata])
def get_exports(case_id: str, principal: Viewer, db: Database) -> list[ExportMetadata]:
    return [
        ExportMetadata.model_validate(export_metadata(db, artifact))
        for artifact in list_exports(db, principal=principal, case_id=case_id)
    ]


@router.get("/{case_id}/exports/{artifact_id}", response_model=ExportMetadata)
def get_export_metadata(
    case_id: str,
    artifact_id: str,
    principal: Viewer,
    db: Database,
) -> ExportMetadata:
    artifact = get_export(
        db,
        principal=principal,
        case_id=case_id,
        artifact_id=artifact_id,
    )
    return ExportMetadata.model_validate(export_metadata(db, artifact))


@router.get("/{case_id}/exports/{artifact_id}/download")
def download_export(
    case_id: str,
    artifact_id: str,
    principal: Viewer,
    db: Database,
    correlation: CorrelationId,
) -> FileResponse:
    download = prepare_download(
        db,
        principal=principal,
        case_id=case_id,
        artifact_id=artifact_id,
        correlation_id=correlation,
    )
    return FileResponse(
        download.path,
        media_type=download.content_type,
        filename=download.download_name,
        headers={
            "X-Artifact-SHA256": download.artifact.sha256,
            "X-Packet-Snapshot-SHA256": download.packet_snapshot_sha256,
        },
    )
