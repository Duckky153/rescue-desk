from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from rescue_desk.auth import Principal
from rescue_desk.models import CalculationRun
from rescue_desk.schemas import (
    AssertionResponse,
    AuditEventResponse,
    CalculationResponse,
    CaseDetail,
    DocumentSummary,
    ExportMetadata,
    FeeResponse,
    ReadinessResponse,
    WorkbenchSnapshotResponse,
)
from rescue_desk.services.audit import list_case_audit_events
from rescue_desk.services.calculations import calculation_response
from rescue_desk.services.cases import current_revision, get_case
from rescue_desk.services.documents import list_documents
from rescue_desk.services.evidence_governance import assertion_response_provenance
from rescue_desk.services.exports import export_metadata, list_exports
from rescue_desk.services.readiness import compute_readiness
from rescue_desk.services.review import list_assertions, list_fees


def build_workbench_snapshot(
    db: Session,
    *,
    principal: Principal,
    case_id: str,
) -> WorkbenchSnapshotResponse:
    """Assemble one coherent workbench read while holding the case lifecycle lock."""

    # Every same-case writer takes this lock before mutating child state. Holding it
    # through all reads prevents a revision/status/export split-brain response.
    case = get_case(db, principal, case_id, for_update=True)
    revision = current_revision(case)

    documents = [
        DocumentSummary.model_validate(item)
        for item in list_documents(db, principal=principal, case_id=case.id)
    ]
    assertions: list[AssertionResponse] = []
    for item in list_assertions(db, principal=principal, case_id=case.id):
        provenance = assertion_response_provenance(db, item)
        assertions.append(
            AssertionResponse.model_validate(item).model_copy(
                update={
                    "assumption": provenance.assumption,
                    "review_reason": provenance.review_reason,
                    "reviewed_by": provenance.reviewed_by,
                    "evidence_basis": provenance.evidence_basis,
                }
            )
        )
    fees = [
        FeeResponse.model_validate(item)
        for item in list_fees(db, principal=principal, case_id=case.id)
    ]
    calculation = db.scalar(
        select(CalculationRun)
        .options(selectinload(CalculationRun.line_items))
        .where(CalculationRun.case_revision_id == revision.id)
        .order_by(CalculationRun.created_at.desc(), CalculationRun.id.desc())
        .limit(1)
    )
    calculation_payload = (
        CalculationResponse.model_validate(calculation_response(calculation))
        if calculation is not None
        else None
    )
    readiness = ReadinessResponse.model_validate(
        compute_readiness(db, principal=principal, case_id=case.id)
    )
    audit_events = [
        AuditEventResponse.model_validate(event)
        for event in list_case_audit_events(
            db,
            case=case,
            organization_id=principal.organization_id,
        )
    ]
    exports = [
        ExportMetadata.model_validate(export_metadata(db, artifact))
        for artifact in list_exports(db, principal=principal, case_id=case.id)
    ]
    return WorkbenchSnapshotResponse(
        case_detail=CaseDetail.model_validate(case),
        documents=documents,
        assertions=assertions,
        fees=fees,
        calculation=calculation_payload,
        readiness=readiness,
        audit_events=audit_events,
        exports=exports,
    )
