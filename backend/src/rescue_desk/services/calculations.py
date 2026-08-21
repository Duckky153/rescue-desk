from dataclasses import asdict
from datetime import date

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from rescue_desk.auth import Principal
from rescue_desk.domain.calculations import (
    AnalysisResult,
    CalculationInputError,
    FeeInput,
    analyze_fees,
)
from rescue_desk.models import CalculationLineItem, CalculationRun, FeeObligation
from rescue_desk.services.audit import append_audit_event
from rescue_desk.services.cases import current_revision, get_case, require_evidence_mutable
from rescue_desk.services.lifecycle import retire_revision_exports


class CalculationIntegrityError(ValueError):
    """A stored calculation no longer equals its deterministic active-fee result."""

    def __init__(self, message: str, *, expected: AnalysisResult | None = None) -> None:
        super().__init__(message)
        self.expected = expected


def verify_calculation_integrity(
    db: Session,
    *,
    revision_id: str,
    calculation: CalculationRun,
) -> AnalysisResult:
    """Recompute and compare every persisted calculation representation."""

    obligations = list(
        db.scalars(
            select(FeeObligation)
            .where(
                FeeObligation.case_revision_id == revision_id,
                FeeObligation.superseded.is_(False),
            )
            .order_by(FeeObligation.id)
        )
    )
    try:
        expected = analyze_fees(
            as_of=calculation.as_of_date,
            fees=[
                FeeInput(
                    identifier=item.id,
                    category=item.category,
                    amount_minor=item.amount_minor,
                    currency=item.currency,
                    service_start=item.service_start,
                    service_end=item.service_end,
                    payment_status=item.payment_status,
                    proration_rule=item.proration_rule,
                    reviewed=item.reviewed,
                )
                for item in obligations
            ],
        )
    except CalculationInputError as exc:
        raise CalculationIntegrityError(
            "Active fee inputs cannot reproduce the stored calculation"
        ) from exc

    expected_input_snapshot = {
        "as_of_date": expected.as_of_date.isoformat(),
        "fees": [
            {
                "id": item.id,
                "category": item.category.value,
                "amount_minor": item.amount_minor,
                "currency": item.currency,
                "service_start": item.service_start.isoformat() if item.service_start else None,
                "service_end": item.service_end.isoformat() if item.service_end else None,
                "payment_status": item.payment_status,
                "proration_rule": item.proration_rule,
                "reviewed": item.reviewed,
            }
            for item in obligations
        ],
    }
    expected_result_snapshot = {
        "currencies": [asdict(item) for item in expected.currencies],
        "blocking_findings": list(expected.blocking_findings),
        "assumptions": list(expected.assumptions),
    }
    expected_lines = sorted(
        (
            item.identifier,
            item.treatment,
            item.original_amount_minor,
            item.remaining_amount_minor,
            item.currency,
            item.formula_identifier,
            item.explanation,
        )
        for item in expected.line_items
    )
    stored_lines = sorted(
        (
            item.fee_obligation_id or "",
            item.treatment,
            item.original_amount_minor,
            item.remaining_amount_minor,
            item.currency,
            item.formula_identifier,
            item.explanation,
        )
        for item in calculation.line_items
    )
    if (
        calculation.case_revision_id != revision_id
        or calculation.engine_version != expected.engine_version
        or calculation.input_hash != expected.input_hash
        or calculation.result_hash != expected.result_hash
        or calculation.input_snapshot != expected_input_snapshot
        or calculation.result_snapshot != expected_result_snapshot
        or calculation.assumptions != list(expected.assumptions)
        or stored_lines != expected_lines
    ):
        raise CalculationIntegrityError(
            "Stored calculation hashes, snapshots, assumptions, or lines are stale or inconsistent",
            expected=expected,
        )
    return expected


def run_calculation(
    db: Session,
    *,
    principal: Principal,
    case_id: str,
    as_of_date: date,
    correlation_id: str,
) -> CalculationRun:
    case = get_case(db, principal, case_id, for_update=True)
    require_evidence_mutable(case)
    revision = current_revision(case)
    obligations = list(
        db.scalars(
            select(FeeObligation)
            .where(
                FeeObligation.case_revision_id == revision.id,
                FeeObligation.superseded.is_(False),
            )
            .order_by(FeeObligation.id)
        )
    )
    if not obligations:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Add at least one fee obligation before calculating",
        )
    try:
        result = analyze_fees(
            as_of=as_of_date,
            fees=[
                FeeInput(
                    identifier=item.id,
                    category=item.category,
                    amount_minor=item.amount_minor,
                    currency=item.currency,
                    service_start=item.service_start,
                    service_end=item.service_end,
                    payment_status=item.payment_status,
                    proration_rule=item.proration_rule,
                    reviewed=item.reviewed,
                )
                for item in obligations
            ],
        )
    except CalculationInputError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    input_snapshot = {
        "as_of_date": result.as_of_date.isoformat(),
        "fees": [
            {
                "id": item.id,
                "category": item.category.value,
                "amount_minor": item.amount_minor,
                "currency": item.currency,
                "service_start": item.service_start.isoformat() if item.service_start else None,
                "service_end": item.service_end.isoformat() if item.service_end else None,
                "payment_status": item.payment_status,
                "proration_rule": item.proration_rule,
                "reviewed": item.reviewed,
            }
            for item in obligations
        ],
    }
    result_snapshot = {
        "currencies": [asdict(item) for item in result.currencies],
        "blocking_findings": list(result.blocking_findings),
        "assumptions": list(result.assumptions),
    }
    calculation = CalculationRun(
        case_revision_id=revision.id,
        as_of_date=as_of_date,
        engine_version=result.engine_version,
        input_snapshot=input_snapshot,
        input_hash=result.input_hash,
        assumptions=list(result.assumptions),
        result_snapshot=result_snapshot,
        result_hash=result.result_hash,
        created_by=principal.user_id,
    )
    db.add(calculation)
    db.flush()
    for line in result.line_items:
        db.add(
            CalculationLineItem(
                calculation_id=calculation.id,
                fee_obligation_id=line.identifier,
                treatment=line.treatment,
                original_amount_minor=line.original_amount_minor,
                remaining_amount_minor=line.remaining_amount_minor,
                currency=line.currency,
                formula_identifier=line.formula_identifier,
                explanation=line.explanation,
            )
        )
    append_audit_event(
        db,
        organization_id=principal.organization_id,
        actor_id=principal.user_id,
        action="calculation.created",
        object_type="calculation_run",
        object_id=calculation.id,
        correlation_id=correlation_id,
        after={
            "case_revision_id": revision.id,
            "input_hash": result.input_hash,
            "result_hash": result.result_hash,
            "blocking_findings": list(result.blocking_findings),
        },
    )
    retire_revision_exports(
        db,
        principal=principal,
        revision_id=revision.id,
        correlation_id=correlation_id,
        reason=f"Calculation {calculation.id} became the current calculation",
    )
    db.flush()
    db.refresh(calculation)
    return calculation


def calculation_response(calculation: CalculationRun) -> dict[str, object]:
    return {
        "id": calculation.id,
        "case_revision_id": calculation.case_revision_id,
        "as_of_date": calculation.as_of_date,
        "engine_version": calculation.engine_version,
        "currencies": calculation.result_snapshot["currencies"],
        "line_items": [
            {
                "identifier": line.fee_obligation_id or line.id,
                "treatment": line.treatment,
                "original_amount_minor": line.original_amount_minor,
                "remaining_amount_minor": line.remaining_amount_minor,
                "currency": line.currency,
                "formula_identifier": line.formula_identifier,
                "explanation": line.explanation,
            }
            for line in calculation.line_items
        ],
        "blocking_findings": calculation.result_snapshot["blocking_findings"],
        "assumptions": calculation.assumptions,
        "input_hash": calculation.input_hash,
        "result_hash": calculation.result_hash,
        "created_at": calculation.created_at,
    }
