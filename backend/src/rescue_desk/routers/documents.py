import hashlib
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from rescue_desk.api_dependencies import correlation_id, idempotency_key
from rescue_desk.auth import Principal, get_current_principal, require_roles
from rescue_desk.config import get_settings
from rescue_desk.database import get_db
from rescue_desk.idempotency import payload_hash, replay_if_present, store_response
from rescue_desk.models import Role
from rescue_desk.schemas import DocumentPageResponse, DocumentSummary, DocumentSupersedeRequest
from rescue_desk.services.documents import (
    ProcessingIdempotency,
    document_path,
    get_document,
    list_documents,
    list_pages,
    process_document,
    supersede_document,
    upload_document,
)

router = APIRouter(prefix="/v1", tags=["documents"])

Viewer = Annotated[Principal, Depends(get_current_principal)]
Analyst = Annotated[
    Principal,
    Depends(require_roles(Role.UPLOADER, Role.ANALYST, Role.APPROVER, Role.ADMIN)),
]
Database = Annotated[Session, Depends(get_db)]
CorrelationId = Annotated[str, Depends(correlation_id)]
IdempotencyKey = Annotated[str, Depends(idempotency_key)]


@router.post("/cases/{case_id}/documents", response_model=DocumentSummary, status_code=201)
async def post_document(
    case_id: str,
    principal: Analyst,
    db: Database,
    correlation: CorrelationId,
    idem_key: IdempotencyKey,
    file: Annotated[UploadFile, File()],
    source_type: Annotated[Literal["synthetic", "public", "redacted"], Form()],
) -> DocumentSummary:
    data = await file.read(get_settings().max_upload_bytes + 1)
    await file.close()
    endpoint = f"POST /v1/cases/{case_id}/documents"
    request_hash = payload_hash(
        {
            "filename": file.filename,
            "source_type": source_type,
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    )
    replay = replay_if_present(
        db,
        organization_id=principal.organization_id,
        endpoint=endpoint,
        key=idem_key,
        request_hash=request_hash,
    )
    if replay:
        return DocumentSummary.model_validate(replay.response_body)
    document = upload_document(
        db,
        principal=principal,
        case_id=case_id,
        original_filename=file.filename or "contract.pdf",
        data=data,
        source_type=source_type,
        correlation_id=correlation,
    )
    response = DocumentSummary.model_validate(document)
    store_response(
        db,
        organization_id=principal.organization_id,
        endpoint=endpoint,
        key=idem_key,
        request_hash=request_hash,
        response_status=201,
        response_body=response.model_dump(mode="json"),
    )
    return response


@router.get("/cases/{case_id}/documents", response_model=list[DocumentSummary])
def get_case_documents(case_id: str, principal: Viewer, db: Database) -> list[DocumentSummary]:
    return [
        DocumentSummary.model_validate(item)
        for item in list_documents(db, principal=principal, case_id=case_id)
    ]


@router.get("/documents/{document_id}", response_model=DocumentSummary)
def get_document_detail(document_id: str, principal: Viewer, db: Database) -> DocumentSummary:
    return DocumentSummary.model_validate(get_document(db, principal, document_id))


@router.post("/documents/{document_id}/process", response_model=DocumentSummary)
def post_process_document(
    document_id: str,
    principal: Analyst,
    db: Database,
    correlation: CorrelationId,
    idem_key: IdempotencyKey,
) -> DocumentSummary:
    endpoint = f"POST /v1/documents/{document_id}/process"
    request_hash = payload_hash({"document_id": document_id, "operation": "process"})
    replay = replay_if_present(
        db,
        organization_id=principal.organization_id,
        endpoint=endpoint,
        key=idem_key,
        request_hash=request_hash,
    )
    if replay:
        if replay.response_status >= 400:
            raise HTTPException(
                status_code=replay.response_status,
                detail=replay.response_body.get("detail", "Document processing failed"),
            )
        return DocumentSummary.model_validate(replay.response_body)
    document = process_document(
        db,
        principal=principal,
        document_id=document_id,
        correlation_id=correlation,
        idempotency=ProcessingIdempotency(
            endpoint=endpoint,
            key=idem_key,
            request_hash=request_hash,
        ),
    )
    response = DocumentSummary.model_validate(document)
    store_response(
        db,
        organization_id=principal.organization_id,
        endpoint=endpoint,
        key=idem_key,
        request_hash=request_hash,
        response_status=status.HTTP_200_OK,
        response_body=response.model_dump(mode="json"),
    )
    return response


@router.get("/documents/{document_id}/pages", response_model=list[DocumentPageResponse])
def get_document_pages(
    document_id: str, principal: Viewer, db: Database
) -> list[DocumentPageResponse]:
    return [
        DocumentPageResponse.model_validate(item)
        for item in list_pages(db, principal=principal, document_id=document_id)
    ]


@router.post("/documents/{document_id}/supersede", response_model=DocumentSummary)
def post_supersede_document(
    document_id: str,
    payload: DocumentSupersedeRequest,
    principal: Analyst,
    db: Database,
    correlation: CorrelationId,
    idem_key: IdempotencyKey,
) -> DocumentSummary:
    endpoint = f"POST /v1/documents/{document_id}/supersede"
    request_hash = payload_hash(payload.model_dump(mode="json"))
    replay = replay_if_present(
        db,
        organization_id=principal.organization_id,
        endpoint=endpoint,
        key=idem_key,
        request_hash=request_hash,
    )
    if replay:
        return DocumentSummary.model_validate(replay.response_body)
    document = supersede_document(
        db,
        principal=principal,
        document_id=document_id,
        payload=payload,
        correlation_id=correlation,
    )
    response = DocumentSummary.model_validate(document)
    store_response(
        db,
        organization_id=principal.organization_id,
        endpoint=endpoint,
        key=idem_key,
        request_hash=request_hash,
        response_status=status.HTTP_200_OK,
        response_body=response.model_dump(mode="json"),
    )
    return response


@router.get("/documents/{document_id}/file")
def get_document_file(document_id: str, principal: Viewer, db: Database) -> FileResponse:
    document = get_document(db, principal, document_id)
    path = document_path(db, principal=principal, document_id=document_id)
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=document.original_filename,
        content_disposition_type="inline",
    )
