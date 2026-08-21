from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

import rescue_desk.services.documents as documents_service
from rescue_desk.auth import Principal
from rescue_desk.models import (
    AssertionReviewState,
    AuditEvent,
    CalculationRun,
    CaseStatus,
    ContractAssertion,
    ContractDocument,
    ExportArtifact,
    FeeObligation,
    Membership,
    Organization,
    ProcessingStatus,
    RescueCase,
    ReviewDecision,
    ReviewDecisionType,
    Role,
    User,
)
from rescue_desk.schemas import AssertionReviewRequest
from rescue_desk.seed_demo import (
    DEMO_COMPLETED_CASE_NAME,
    DEMO_GUIDED_CASE_NAME,
    DEMO_PENDING_SEMANTIC_KEY,
    seed_demo,
)
from rescue_desk.services.readiness import compute_readiness
from rescue_desk.services.review import review_assertion

FIXTURE = Path(__file__).parents[3] / "fixtures" / "contracts" / "clean_standard.pdf"


def test_demo_seed_is_complete_and_idempotent(db: Session) -> None:
    first = seed_demo(db, fixture_path=FIXTURE)
    second = seed_demo(db, fixture_path=FIXTURE)
    assert first.created is True
    assert second.created is False
    assert second.case_id == first.case_id
    assert second.completed_case_id == first.completed_case_id
    assert db.query(Organization).count() == 1
    assert db.query(User).count() == 5
    assert db.query(Membership).count() == 5
    assert db.query(RescueCase).count() == 2
    assert db.query(ContractDocument).count() == 2
    assert db.query(ContractAssertion).count() >= 12
    assert db.query(FeeObligation).count() == 2
    assert db.query(CalculationRun).count() == 2

    guided = db.query(RescueCase).filter_by(display_name=DEMO_GUIDED_CASE_NAME).one()
    completed = db.query(RescueCase).filter_by(display_name=DEMO_COMPLETED_CASE_NAME).one()
    assert guided.id == first.case_id
    assert guided.status == CaseStatus.EVIDENCE_REVIEW
    assert completed.id == first.completed_case_id
    assert completed.status == CaseStatus.EXPORTED

    guided_revision = next(item for item in guided.revisions if item.number == 1)
    guided_assertions = db.query(ContractAssertion).filter_by(
        case_revision_id=guided_revision.id,
        is_current=True,
    )
    pending = guided_assertions.filter_by(review_state=AssertionReviewState.PROPOSED).one()
    assert pending.semantic_key == DEMO_PENDING_SEMANTIC_KEY
    assert guided_assertions.filter_by(review_state=AssertionReviewState.ACCEPTED).count() >= 5

    synthetic_analyst = db.query(User).filter_by(display_name="Synthetic Demo Analyst").one()
    seeded_guided_decisions = (
        db.query(ReviewDecision)
        .join(ContractAssertion, ReviewDecision.assertion_id == ContractAssertion.id)
        .filter(ContractAssertion.case_revision_id == guided_revision.id)
        .all()
    )
    assert seeded_guided_decisions
    assert {item.reviewer_id for item in seeded_guided_decisions} == {synthetic_analyst.id}

    analyst = db.query(User).filter_by(email=first.analyst_email).one()
    principal = Principal(
        user_id=analyst.id,
        organization_id=first.organization_id,
        role=Role.ANALYST,
        email=analyst.email,
        display_name=analyst.display_name,
    )
    guided_readiness = compute_readiness(db, principal=principal, case_id=guided.id)
    assert guided_readiness["open_blocking_findings"] == 1
    assert guided_readiness["ready_for_internal_review"] is False
    assert guided_readiness["reproducible_calculation_exists"] is True

    artifacts = db.query(ExportArtifact).all()
    assert len(artifacts) == 4
    assert {item.kind.value for item in artifacts} == {
        "internal_review_pdf",
        "customer_explanation_pdf",
        "evidence_csv",
        "machine_readable_json",
    }
    assert {item.approval_status for item in artifacts} == {"approved"}
    assert {item.superseded for item in artifacts} == {False}
    assert len({item.packet_snapshot_sha256 for item in artifacts}) == 1

    synthetic_approver = db.query(User).filter_by(display_name="Synthetic Demo Approver").one()
    approval_event = db.query(AuditEvent).filter_by(correlation_id="demo-completed-approved").one()
    assert approval_event.actor_id == synthetic_approver.id
    assert approval_event.after is not None
    assert approval_event.after["reason"] == (
        "Seeded synthetic approver-role checkpoint recorded for the demonstration"
    )

    review_assertion(
        db,
        principal=principal,
        assertion_id=pending.id,
        payload=AssertionReviewRequest(
            decision=ReviewDecisionType.ACCEPT,
            expected_assertion_version=pending.version,
            reason="Final guided decision verified against the synthetic page citation",
        ),
        correlation_id="demo-test-final-guided-decision",
    )
    live_decision = db.query(ReviewDecision).filter_by(assertion_id=pending.id).one()
    assert live_decision.reviewer_id == analyst.id
    completed_readiness = compute_readiness(db, principal=principal, case_id=guided.id)
    assert completed_readiness["open_blocking_findings"] == 0
    assert completed_readiness["ready_for_internal_review"] is True


def test_demo_seed_recovers_same_named_case_after_transient_processing_failure(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_extract = documents_service.deterministic_extract
    attempts = 0

    def fail_once(*args: object, **kwargs: object) -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient deterministic extraction failure")
        return real_extract(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(documents_service, "deterministic_extract", fail_once)
    with pytest.raises(HTTPException) as failure:
        seed_demo(db, fixture_path=FIXTURE)
    assert failure.value.status_code == 422
    failed_case = db.query(RescueCase).one()
    failed_document = db.query(ContractDocument).one()
    assert failed_document.processing_status == ProcessingStatus.FAILED

    recovered = seed_demo(db, fixture_path=FIXTURE)
    repeated = seed_demo(db, fixture_path=FIXTURE)

    # The failed completed case is resumed, then the second guided case is created.
    assert recovered.created is True
    assert recovered.completed_case_id == failed_case.id
    assert repeated.created is False
    assert repeated.case_id == recovered.case_id
    assert repeated.completed_case_id == failed_case.id
    assert attempts == 3
    assert db.query(RescueCase).count() == 2
    assert db.query(ContractDocument).count() == 2
    assert {item.processing_status for item in db.query(ContractDocument).all()} == {
        ProcessingStatus.PROCESSED
    }
    assert db.query(FeeObligation).count() == 2
    assert db.query(CalculationRun).count() == 2
    assert db.query(ExportArtifact).count() == 4
