from typing import Annotated

from fastapi import APIRouter, Depends, Header, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from rescue_desk.api_dependencies import correlation_id, idempotency_key
from rescue_desk.auth import Principal, get_current_principal, require_roles
from rescue_desk.database import get_db
from rescue_desk.idempotency import payload_hash, replay_if_present, store_response
from rescue_desk.models import (
    CalculationRun,
    Role,
)
from rescue_desk.schemas import (
    AssertionResponse,
    AssertionReviewRequest,
    AuditEventResponse,
    CalculationRequest,
    CalculationResponse,
    CaseCreate,
    CaseDetail,
    CaseSummary,
    FeeCreate,
    FeeResponse,
    FeeSupersedeRequest,
    FindingResolutionRequest,
    ReadinessFindingResponse,
    ReadinessResponse,
    TransitionRequest,
    WorkbenchSnapshotResponse,
)
from rescue_desk.services.audit import list_case_audit_events
from rescue_desk.services.calculations import calculation_response, run_calculation
from rescue_desk.services.cases import create_case, get_case, list_cases, transition_case
from rescue_desk.services.evidence_governance import assertion_response_provenance
from rescue_desk.services.findings import resolve_finding
from rescue_desk.services.readiness import compute_readiness
from rescue_desk.services.review import (
    add_fee,
    list_assertions,
    list_fees,
    review_assertion,
    supersede_fee,
)
from rescue_desk.services.workbench import build_workbench_snapshot

router = APIRouter(prefix="/v1/cases", tags=["cases"])

Viewer = Annotated[Principal, Depends(get_current_principal)]
Analyst = Annotated[
    Principal,
    Depends(require_roles(Role.ANALYST, Role.APPROVER, Role.ADMIN)),
]
Approver = Annotated[Principal, Depends(require_roles(Role.APPROVER, Role.ADMIN))]
Database = Annotated[Session, Depends(get_db)]
CorrelationId = Annotated[str, Depends(correlation_id)]
IdempotencyKey = Annotated[str, Depends(idempotency_key)]


@router.post("", response_model=CaseDetail, status_code=status.HTTP_201_CREATED)
def post_case(
    payload: CaseCreate,
    principal: Analyst,
    db: Database,
    correlation: CorrelationId,
    idem_key: IdempotencyKey,
) -> CaseDetail:
    endpoint = "POST /v1/cases"
    request_hash = payload_hash(payload.model_dump(mode="json"))
    replay = replay_if_present(
        db,
        organization_id=principal.organization_id,
        endpoint=endpoint,
        key=idem_key,
        request_hash=request_hash,
    )
    if replay:
        return CaseDetail.model_validate(replay.response_body)
    case = create_case(db, principal=principal, payload=payload, correlation_id=correlation)
    response = CaseDetail.model_validate(case)
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


@router.get("", response_model=list[CaseSummary])
def get_cases(principal: Viewer, db: Database) -> list[CaseSummary]:
    return [CaseSummary.model_validate(case) for case in list_cases(db, principal)]


@router.get("/{case_id}", response_model=CaseDetail)
def get_case_detail(case_id: str, principal: Viewer, db: Database) -> CaseDetail:
    return CaseDetail.model_validate(get_case(db, principal, case_id))


@router.get("/{case_id}/workbench-snapshot", response_model=WorkbenchSnapshotResponse)
def get_workbench_snapshot(
    case_id: str,
    principal: Viewer,
    db: Database,
) -> WorkbenchSnapshotResponse:
    return build_workbench_snapshot(db, principal=principal, case_id=case_id)


@router.post("/{case_id}/transitions", response_model=CaseDetail)
def post_transition(
    case_id: str,
    payload: TransitionRequest,
    principal: Analyst,
    db: Database,
    correlation: CorrelationId,
    idem_key: IdempotencyKey,
) -> CaseDetail:
    endpoint = f"POST /v1/cases/{case_id}/transitions"
    request_hash = payload_hash(payload.model_dump(mode="json"))
    replay = replay_if_present(
        db,
        organization_id=principal.organization_id,
        endpoint=endpoint,
        key=idem_key,
        request_hash=request_hash,
    )
    if replay:
        return CaseDetail.model_validate(replay.response_body)
    case = transition_case(
        db,
        principal=principal,
        case_id=case_id,
        payload=payload,
        correlation_id=correlation,
    )
    response = CaseDetail.model_validate(case)
    store_response(
        db,
        organization_id=principal.organization_id,
        endpoint=endpoint,
        key=idem_key,
        request_hash=request_hash,
        response_status=200,
        response_body=response.model_dump(mode="json"),
    )
    return response


@router.get("/{case_id}/assertions", response_model=list[AssertionResponse])
def get_assertions(case_id: str, principal: Viewer, db: Database) -> list[AssertionResponse]:
    responses: list[AssertionResponse] = []
    for item in list_assertions(db, principal=principal, case_id=case_id):
        provenance = assertion_response_provenance(db, item)
        responses.append(
            AssertionResponse.model_validate(item).model_copy(
                update={
                    "assumption": provenance.assumption,
                    "review_reason": provenance.review_reason,
                    "reviewed_by": provenance.reviewed_by,
                    "evidence_basis": provenance.evidence_basis,
                }
            )
        )
    return responses


@router.post("/assertions/{assertion_id}/reviews", response_model=AssertionResponse)
def post_assertion_review(
    assertion_id: str,
    payload: AssertionReviewRequest,
    principal: Analyst,
    db: Database,
    correlation: CorrelationId,
    idem_key: IdempotencyKey,
) -> AssertionResponse:
    endpoint = f"POST /v1/cases/assertions/{assertion_id}/reviews"
    request_hash = payload_hash(payload.model_dump(mode="json"))
    replay = replay_if_present(
        db,
        organization_id=principal.organization_id,
        endpoint=endpoint,
        key=idem_key,
        request_hash=request_hash,
    )
    if replay:
        return AssertionResponse.model_validate(replay.response_body)
    assertion = review_assertion(
        db,
        principal=principal,
        assertion_id=assertion_id,
        payload=payload,
        correlation_id=correlation,
    )
    provenance = assertion_response_provenance(db, assertion)
    response = AssertionResponse.model_validate(assertion).model_copy(
        update={
            "assumption": provenance.assumption,
            "review_reason": provenance.review_reason,
            "reviewed_by": provenance.reviewed_by,
            "evidence_basis": provenance.evidence_basis,
        }
    )
    store_response(
        db,
        organization_id=principal.organization_id,
        endpoint=endpoint,
        key=idem_key,
        request_hash=request_hash,
        response_status=200,
        response_body=response.model_dump(mode="json"),
    )
    return response


@router.post("/{case_id}/fees", response_model=FeeResponse, status_code=201)
def post_fee(
    case_id: str,
    payload: FeeCreate,
    principal: Analyst,
    db: Database,
    correlation: CorrelationId,
    idem_key: IdempotencyKey,
) -> FeeResponse:
    endpoint = f"POST /v1/cases/{case_id}/fees"
    request_hash = payload_hash(payload.model_dump(mode="json"))
    replay = replay_if_present(
        db,
        organization_id=principal.organization_id,
        endpoint=endpoint,
        key=idem_key,
        request_hash=request_hash,
    )
    if replay:
        return FeeResponse.model_validate(replay.response_body)
    fee = add_fee(
        db,
        principal=principal,
        case_id=case_id,
        payload=payload,
        correlation_id=correlation,
    )
    response = FeeResponse.model_validate(fee)
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


@router.get("/{case_id}/fees", response_model=list[FeeResponse])
def get_fees(case_id: str, principal: Viewer, db: Database) -> list[FeeResponse]:
    return [
        FeeResponse.model_validate(item)
        for item in list_fees(db, principal=principal, case_id=case_id)
    ]


@router.post("/{case_id}/fees/{fee_id}/supersede", response_model=FeeResponse)
def post_fee_supersede(
    case_id: str,
    fee_id: str,
    payload: FeeSupersedeRequest,
    principal: Analyst,
    db: Database,
    correlation: CorrelationId,
    idem_key: IdempotencyKey,
) -> FeeResponse:
    endpoint = f"POST /v1/cases/{case_id}/fees/{fee_id}/supersede"
    request_hash = payload_hash(payload.model_dump(mode="json"))
    replay = replay_if_present(
        db,
        organization_id=principal.organization_id,
        endpoint=endpoint,
        key=idem_key,
        request_hash=request_hash,
    )
    if replay:
        return FeeResponse.model_validate(replay.response_body)
    fee = supersede_fee(
        db,
        principal=principal,
        case_id=case_id,
        fee_id=fee_id,
        payload=payload,
        correlation_id=correlation,
    )
    response = FeeResponse.model_validate(fee)
    store_response(
        db,
        organization_id=principal.organization_id,
        endpoint=endpoint,
        key=idem_key,
        request_hash=request_hash,
        response_status=200,
        response_body=response.model_dump(mode="json"),
    )
    return response


@router.post("/{case_id}/calculations", response_model=CalculationResponse, status_code=201)
def post_calculation(
    case_id: str,
    payload: CalculationRequest,
    principal: Analyst,
    db: Database,
    correlation: CorrelationId,
    idem_key: IdempotencyKey,
) -> CalculationResponse:
    endpoint = f"POST /v1/cases/{case_id}/calculations"
    request_hash = payload_hash(payload.model_dump(mode="json"))
    replay = replay_if_present(
        db,
        organization_id=principal.organization_id,
        endpoint=endpoint,
        key=idem_key,
        request_hash=request_hash,
    )
    if replay:
        return CalculationResponse.model_validate(replay.response_body)
    calculation = run_calculation(
        db,
        principal=principal,
        case_id=case_id,
        as_of_date=payload.as_of_date,
        correlation_id=correlation,
    )
    response = CalculationResponse.model_validate(calculation_response(calculation))
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


@router.get("/{case_id}/calculations/latest", response_model=CalculationResponse)
def get_latest_calculation(case_id: str, principal: Viewer, db: Database) -> CalculationResponse:
    case = get_case(db, principal, case_id)
    revision_id = next(
        revision.id
        for revision in case.revisions
        if revision.number == case.current_revision_number
    )
    calculation = db.scalar(
        select(CalculationRun)
        .options(selectinload(CalculationRun.line_items))
        .where(CalculationRun.case_revision_id == revision_id)
        .order_by(CalculationRun.created_at.desc(), CalculationRun.id.desc())
        .limit(1)
    )
    if calculation is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Calculation not found")
    return CalculationResponse.model_validate(calculation_response(calculation))


@router.get("/{case_id}/readiness", response_model=ReadinessResponse)
def get_readiness(case_id: str, principal: Viewer, db: Database) -> ReadinessResponse:
    return ReadinessResponse.model_validate(
        compute_readiness(db, principal=principal, case_id=case_id)
    )


@router.post(
    "/{case_id}/findings/{finding_id}/disposition",
    response_model=ReadinessFindingResponse,
)
def post_finding_disposition(
    case_id: str,
    finding_id: str,
    payload: FindingResolutionRequest,
    principal: Approver,
    db: Database,
    correlation: CorrelationId,
    idem_key: IdempotencyKey,
) -> ReadinessFindingResponse:
    endpoint = f"POST /v1/cases/{case_id}/findings/{finding_id}/disposition"
    request_hash = payload_hash(payload.model_dump(mode="json"))
    replay = replay_if_present(
        db,
        organization_id=principal.organization_id,
        endpoint=endpoint,
        key=idem_key,
        request_hash=request_hash,
    )
    if replay:
        return ReadinessFindingResponse.model_validate(replay.response_body)
    finding = resolve_finding(
        db,
        principal=principal,
        case_id=case_id,
        finding_id=finding_id,
        payload=payload,
        correlation_id=correlation,
    )
    response = ReadinessFindingResponse.model_validate(finding)
    store_response(
        db,
        organization_id=principal.organization_id,
        endpoint=endpoint,
        key=idem_key,
        request_hash=request_hash,
        response_status=200,
        response_body=response.model_dump(mode="json"),
    )
    return response


@router.get("/{case_id}/audit-events", response_model=list[AuditEventResponse])
def get_audit_events(
    case_id: str,
    principal: Viewer,
    db: Database,
    limit: Annotated[int, Header(alias="X-Result-Limit", ge=1, le=500)] = 200,
) -> list[AuditEventResponse]:
    case = get_case(db, principal, case_id)
    events = list_case_audit_events(
        db,
        case=case,
        organization_id=principal.organization_id,
        limit=limit,
    )
    return [AuditEventResponse.model_validate(event) for event in events]
