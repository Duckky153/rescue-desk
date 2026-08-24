import hashlib
from datetime import date
from typing import cast

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from rescue_desk.config import get_settings
from rescue_desk.models import (
    AssertionReviewState,
    AssertionSource,
    CaseRevision,
    ContractAssertion,
    ContractDocument,
    DocumentPage,
    EvidenceSpan,
    ProcessingStatus,
    RescueCase,
    ReviewDecision,
    ReviewDecisionType,
    Role,
    SafetyStatus,
)
from tests.integration.conftest import SeededIdentity, seed_identity


def create_case(client: TestClient, identity: SeededIdentity, *, key: str) -> dict[str, object]:
    response = client.post(
        "/v1/cases",
        headers={**identity.headers, "Idempotency-Key": key},
        json={
            "display_name": "Legacy ERP exit review",
            "applicant_company": "Northstar Manufacturing",
            "erp_provider": "LegacySuite",
        },
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, object], response.json())


def test_login_and_current_user(client: TestClient, db: Session) -> None:
    identity = seed_identity(db, email="analyst@demo.local", role=Role.ANALYST)
    login = client.post(
        "/v1/auth/token",
        json={
            "email": "analyst@demo.local",
            "password": "DemoPassword!2026",
            "organization_id": identity.organization_id,
        },
    )
    assert login.status_code == 200
    me = client.get(
        "/v1/auth/me",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )
    assert me.status_code == 200
    assert me.json()["role"] == "analyst"


def test_create_case_is_idempotent_and_rejects_key_reuse(client: TestClient, db: Session) -> None:
    identity = seed_identity(db, email="analyst@demo.local", role=Role.ANALYST)
    first = create_case(client, identity, key="create-case-001")
    second = create_case(client, identity, key="create-case-001")
    assert second["id"] == first["id"]
    conflict = client.post(
        "/v1/cases",
        headers={**identity.headers, "Idempotency-Key": "create-case-001"},
        json={
            "display_name": "Different case",
            "applicant_company": "Different Company",
            "erp_provider": "Other ERP",
        },
    )
    assert conflict.status_code == 409
    assert db.query(RescueCase).count() == 1


def test_tenant_isolation_hides_other_organization_case(client: TestClient, db: Session) -> None:
    first = seed_identity(db, email="first@demo.local", role=Role.ANALYST)
    second = seed_identity(
        db,
        email="second@demo.local",
        role=Role.ANALYST,
        organization_name="Second Demo",
    )
    case = create_case(client, first, key="first-org-case")
    response = client.get(f"/v1/cases/{case['id']}", headers=second.headers)
    assert response.status_code == 404


def test_end_to_end_review_calculation_and_human_approval(client: TestClient, db: Session) -> None:
    analyst = seed_identity(db, email="analyst@demo.local", role=Role.ANALYST)
    approver = seed_identity(db, email="approver@demo.local", role=Role.APPROVER)
    case_payload = create_case(client, analyst, key="end-to-end-case")
    case = db.get(RescueCase, str(case_payload["id"]))
    assert case is not None
    revision = db.query(CaseRevision).filter_by(case_id=case.id, number=1).one()
    page_text = "Contract ends December 31, 2027. Annual subscription fee is USD 12,000."
    document_bytes = page_text.encode()
    stored_path = get_settings().upload_dir / "synthetic-contract.pdf"
    stored_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stored_path.write_bytes(document_bytes)
    stored_path.chmod(0o600)
    document = ContractDocument(
        organization_id=analyst.organization_id,
        case_revision_id=revision.id,
        original_filename="synthetic-legacy-contract.pdf",
        stored_filename=stored_path.name,
        sha256=hashlib.sha256(document_bytes).hexdigest(),
        media_type="application/pdf",
        source_type="synthetic",
        size_bytes=len(document_bytes),
        page_count=1,
        safety_status=SafetyStatus.PASSED,
        processing_status=ProcessingStatus.PROCESSED,
        created_by=analyst.user_id,
    )
    db.add(document)
    db.flush()
    page = DocumentPage(
        document_id=document.id,
        page_number=1,
        text=page_text,
        text_sha256=hashlib.sha256(document_bytes).hexdigest(),
        extraction_confidence=1,
    )
    db.add(page)
    db.flush()
    assertions: list[ContractAssertion] = []
    for key, display, value in (
        (
            "contract_end_date",
            "December 31, 2027",
            {"type": "date", "value": "2027-12-31"},
        ),
        (
            "fee.subscription",
            "USD 12,000",
            {
                "type": "money",
                "amount_minor": 1_200_000,
                "currency": "USD",
                "cadence": "annual",
            },
        ),
    ):
        start = page_text.index(display)
        evidence = EvidenceSpan(
            page_id=page.id,
            quote=display,
            char_start=start,
            char_end=start + len(display),
            quote_sha256=hashlib.sha256(display.encode()).hexdigest(),
        )
        assertion = ContractAssertion(
            case_revision_id=revision.id,
            semantic_key=key,
            raw_value=display,
            normalized_value=value,
            display_value=("2027-12-31" if key == "contract_end_date" else "USD 12,000.00"),
            source=AssertionSource.HUMAN,
            confidence=1,
            review_state=AssertionReviewState.ACCEPTED,
            evidence=[evidence],
        )
        db.add(assertion)
        db.flush()
        db.add(
            ReviewDecision(
                assertion_id=assertion.id,
                decision=ReviewDecisionType.ACCEPT,
                reviewer_id=analyst.user_id,
                reason="Confirmed against the exact synthetic source quote.",
                assumption=False,
            )
        )
        assertions.append(assertion)
    db.commit()
    fee = client.post(
        f"/v1/cases/{case.id}/fees",
        headers={**analyst.headers, "Idempotency-Key": "fee-annual-001"},
        json={
            "category": "subscription",
            "amount_minor": 12_000_00,
            "currency": "USD",
            "service_start": "2026-01-01",
            "service_end": "2027-01-01",
            "payment_status": "unpaid",
            "billing_cadence": "annual",
            "proration_rule": "contract_daily",
            "assertion_ids": [
                next(item.id for item in assertions if item.semantic_key == "fee.subscription")
            ],
            "reviewed": True,
        },
    )
    assert fee.status_code == 201, fee.text
    calculation = client.post(
        f"/v1/cases/{case.id}/calculations",
        headers={**analyst.headers, "Idempotency-Key": "calculation-001"},
        json={"as_of_date": date(2026, 7, 2).isoformat()},
    )
    assert calculation.status_code == 201, calculation.text
    assert calculation.json()["currencies"][0]["documented_remaining_subscription_minor"] == 601_644
    readiness = client.get(f"/v1/cases/{case.id}/readiness", headers=analyst.headers)
    assert readiness.status_code == 200, readiness.text
    assert readiness.json()["ready_for_internal_review"] is True
    evidence_review = client.post(
        f"/v1/cases/{case.id}/transitions",
        headers={**analyst.headers, "Idempotency-Key": "transition-evidence"},
        json={"target": "evidence_review", "expected_version": 1, "reason": "Evidence ready"},
    )
    assert evidence_review.status_code == 200, evidence_review.text
    ready = client.post(
        f"/v1/cases/{case.id}/transitions",
        headers={**analyst.headers, "Idempotency-Key": "transition-ready"},
        json={
            "target": "ready_for_internal_review",
            "expected_version": 2,
            "reason": "All evidence reviewed",
        },
    )
    assert ready.status_code == 200, ready.text
    forbidden = client.post(
        f"/v1/cases/{case.id}/transitions",
        headers={**analyst.headers, "Idempotency-Key": "transition-approve-analyst"},
        json={
            "target": "internal_packet_approved",
            "expected_version": 3,
            "reason": "Attempted analyst approval",
        },
    )
    assert forbidden.status_code == 409
    approved = client.post(
        f"/v1/cases/{case.id}/transitions",
        headers={**approver.headers, "Idempotency-Key": "transition-approve-human"},
        json={
            "target": "internal_packet_approved",
            "expected_version": 3,
            "reason": "Human internal review complete",
        },
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "internal_packet_approved"
    assert approved.json()["revisions"][0]["snapshot_hash"]
