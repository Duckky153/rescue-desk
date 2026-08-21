from __future__ import annotations

import copy
from datetime import date
from pathlib import Path
from typing import Any, cast

import fitz
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from rescue_desk.models import (
    AuditEvent,
    CalculationRun,
    ContractAssertion,
    ContractDocument,
    FeeObligation,
    IdempotencyRecord,
    ReviewDecision,
    ReviewDecisionType,
    Role,
)
from tests.integration.conftest import SeededIdentity, seed_identity

FIXTURES = Path(__file__).parents[3] / "fixtures" / "contracts"


def _create_and_process(
    client: TestClient, identity: SeededIdentity, *, suffix: str
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    created = client.post(
        "/v1/cases",
        headers={**identity.headers, "Idempotency-Key": f"case-{suffix}"},
        json={
            "display_name": f"Evidence contract {suffix}",
            "applicant_company": "Northstar Systems",
            "erp_provider": "LegacySuite",
        },
    )
    assert created.status_code == 201, created.text
    case = cast(dict[str, Any], created.json())
    uploaded = client.post(
        f"/v1/cases/{case['id']}/documents",
        headers={**identity.headers, "Idempotency-Key": f"upload-{suffix}"},
        data={"source_type": "synthetic"},
        files={
            "file": (
                "clean_standard.pdf",
                (FIXTURES / "clean_standard.pdf").read_bytes(),
                "application/pdf",
            )
        },
    )
    assert uploaded.status_code == 201, uploaded.text
    processed = client.post(
        f"/v1/documents/{uploaded.json()['id']}/process",
        headers={**identity.headers, "Idempotency-Key": f"process-{suffix}"},
    )
    assert processed.status_code == 200, processed.text
    assertions_response = client.get(f"/v1/cases/{case['id']}/assertions", headers=identity.headers)
    assert assertions_response.status_code == 200, assertions_response.text
    assertions = {item["semantic_key"]: item for item in assertions_response.json()}
    return case, assertions


def _review(
    client: TestClient,
    identity: SeededIdentity,
    assertion: dict[str, Any],
    *,
    suffix: str,
    decision: str = "accept",
    corrected_value: dict[str, Any] | None = None,
    corrected_display_value: str | None = None,
    assumption: bool = False,
) -> Any:
    return client.post(
        f"/v1/cases/assertions/{assertion['id']}/reviews",
        headers={**identity.headers, "Idempotency-Key": f"review-{suffix}"},
        json={
            "decision": decision,
            "expected_assertion_version": assertion["version"],
            "reason": "Adversarial evidence-contract verification",
            "corrected_value": corrected_value,
            "corrected_display_value": corrected_display_value,
            "assumption": assumption,
        },
    )


@pytest.mark.parametrize(
    "malformed",
    [
        {"type": "money", "amount_minor": 1, "currency": "USD", "cadence": "annual"},
        {"type": "date", "value": "2030-02-30"},
        {"type": "date", "value": "2030-02-28", "untrusted": "anything"},
        {"type": "text", "value": "whenever the user wants"},
    ],
)
def test_contract_end_correction_rejects_malformed_or_mismatched_json(
    client: TestClient,
    db: Session,
    malformed: dict[str, Any],
) -> None:
    analyst = seed_identity(
        db,
        email=f"bad-correction-{len(str(malformed))}@demo.local",
        role=Role.ANALYST,
        organization_name=f"Bad correction {len(str(malformed))}",
    )
    _, assertions = _create_and_process(client, analyst, suffix=f"bad-{len(str(malformed))}")
    response = _review(
        client,
        analyst,
        assertions["contract_end_date"],
        suffix=f"bad-{len(str(malformed))}",
        decision="correct",
        corrected_value=malformed,
        corrected_display_value="Arbitrary display text",
    )
    assert response.status_code == 422, response.text
    assert db.query(ReviewDecision).count() == 0


def test_accept_rejects_canonical_assertion_without_exact_source_evidence(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="accept-no-evidence@demo.local", role=Role.ANALYST)
    _, assertions = _create_and_process(client, analyst, suffix="accept-no-evidence")
    target = db.get(ContractAssertion, assertions["contract_end_date"]["id"])
    assert target is not None
    target.evidence.clear()
    db.commit()

    response = _review(
        client,
        analyst,
        assertions["contract_end_date"],
        suffix="accept-no-evidence",
    )

    assert response.status_code == 422, response.text
    assert "exact evidence" in response.text
    assert db.query(ReviewDecision).filter_by(assertion_id=target.id).count() == 0
    assert db.query(IdempotencyRecord).filter_by(key="review-accept-no-evidence").count() == 0


def test_accept_rejects_assertion_whose_only_exact_evidence_is_superseded(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="accept-retired-evidence@demo.local", role=Role.ANALYST)
    case, _ = _create_and_process(client, analyst, suffix="accept-retired-evidence")
    case_id = str(case["id"])
    first_document = db.query(ContractDocument).one()
    second = client.post(
        f"/v1/cases/{case_id}/documents",
        headers={**analyst.headers, "Idempotency-Key": "accept-retired-second-upload"},
        data={"source_type": "synthetic"},
        files={
            "file": (
                "fee_and_date_variants.pdf",
                (FIXTURES / "fee_and_date_variants.pdf").read_bytes(),
                "application/pdf",
            )
        },
    )
    assert second.status_code == 201, second.text
    processed = client.post(
        f"/v1/documents/{second.json()['id']}/process",
        headers={**analyst.headers, "Idempotency-Key": "accept-retired-second-process"},
    )
    assert processed.status_code == 200, processed.text
    current = client.get(f"/v1/cases/{case_id}", headers=analyst.headers)
    assert current.status_code == 200, current.text
    superseded = client.post(
        f"/v1/documents/{first_document.id}/supersede",
        headers={**analyst.headers, "Idempotency-Key": "accept-retired-supersede"},
        json={
            "expected_case_version": current.json()["version"],
            "reason": "The second synthetic source replaces this first source.",
        },
    )
    assert superseded.status_code == 200, superseded.text
    assertions = client.get(f"/v1/cases/{case_id}/assertions", headers=analyst.headers).json()
    notice = next(item for item in assertions if item["semantic_key"] == "renewal.notice_days")
    assert {item["document_id"] for item in notice["evidence"]} == {first_document.id}

    rejected = _review(
        client,
        analyst,
        notice,
        suffix="accept-retired-only",
    )

    assert rejected.status_code == 422, rejected.text
    assert "active document" in rejected.text
    assert db.query(ReviewDecision).filter_by(assertion_id=notice["id"]).count() == 0


def test_valid_correction_is_canonicalized_and_invalid_state_cannot_pass_approval(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="canonical-correction@demo.local", role=Role.ANALYST)
    case, assertions = _create_and_process(client, analyst, suffix="canonical")
    response = _review(
        client,
        analyst,
        assertions["contract_end_date"],
        suffix="canonical",
        decision="correct",
        corrected_value={"type": "date", "value": "2030-02-28"},
        corrected_display_value="This free text must not become the approved fact",
    )
    assert response.status_code == 422, response.text
    response = _review(
        client,
        analyst,
        assertions["contract_end_date"],
        suffix="canonical-assumption",
        decision="correct",
        corrected_value={"type": "date", "value": "2030-02-28"},
        corrected_display_value="This free text must not become the approved fact",
        assumption=True,
    )
    assert response.status_code == 200, response.text
    corrected = response.json()
    assert corrected["normalized_value"] == {"type": "date", "value": "2030-02-28"}
    assert corrected["display_value"] == "2030-02-28"
    persisted = db.get(ContractAssertion, corrected["id"])
    assert persisted is not None
    persisted.normalized_value = {"type": "text", "value": "tampered"}
    persisted.display_value = "Any date at all"
    db.commit()

    readiness = client.get(f"/v1/cases/{case['id']}/readiness", headers=analyst.headers)
    assert readiness.status_code == 200, readiness.text
    payload = readiness.json()
    assert payload["mandatory_assertions_reviewed"] is False
    assert any(item["code"].endswith(".invalid_value") for item in payload["findings"])
    current = client.get(f"/v1/cases/{case['id']}", headers=analyst.headers).json()
    transition = client.post(
        f"/v1/cases/{case['id']}/transitions",
        headers={**analyst.headers, "Idempotency-Key": "invalid-contract-ready"},
        json={
            "target": "ready_for_internal_review",
            "expected_version": current["version"],
            "reason": "Invalid typed state must not pass",
        },
    )
    assert transition.status_code == 409, transition.text


def test_valid_correction_derives_display_without_caller_supplied_text(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="derived-correction@demo.local", role=Role.ANALYST)
    _, assertions = _create_and_process(client, analyst, suffix="derived")

    response = _review(
        client,
        analyst,
        assertions["contract_end_date"],
        suffix="derived",
        decision="correct",
        corrected_value={"type": "date", "value": "2031-04-30"},
        assumption=True,
    )

    assert response.status_code == 200, response.text
    assert response.json()["display_value"] == "2031-04-30"


def test_accept_cannot_promote_a_malformed_proposal(client: TestClient, db: Session) -> None:
    analyst = seed_identity(db, email="malformed-proposal@demo.local", role=Role.ANALYST)
    _, assertions = _create_and_process(client, analyst, suffix="malformed-proposal")
    fee_assertion = db.get(ContractAssertion, assertions["fee.subscription"]["id"])
    assert fee_assertion is not None
    fee_assertion.normalized_value = {"type": "text", "value": "USD 120,000 maybe"}
    db.commit()
    response = _review(
        client,
        analyst,
        assertions["fee.subscription"],
        suffix="malformed-proposal",
    )
    assert response.status_code == 422, response.text
    assert "record a typed correction" in response.text
    assert db.query(ReviewDecision).count() == 0


def test_reviewed_fee_rejects_unrelated_or_mismatched_evidence_and_accepts_exact_fee_fact(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="fee-evidence@demo.local", role=Role.ANALYST)
    case, assertions = _create_and_process(client, analyst, suffix="fee-evidence")
    for key in ("contract_end_date", "fee.subscription"):
        reviewed = _review(client, analyst, assertions[key], suffix=f"fee-{key}")
        assert reviewed.status_code == 200, reviewed.text
        assertions[key] = reviewed.json()

    base_fee = {
        "category": "subscription",
        "amount_minor": 12_000_000,
        "currency": "USD",
        "service_start": "2026-01-15",
        "service_end": "2027-01-15",
        "payment_status": "unpaid",
        "billing_cadence": "annual",
        "proration_rule": "contract_daily",
        "reviewed": True,
    }
    unrelated = client.post(
        f"/v1/cases/{case['id']}/fees",
        headers={**analyst.headers, "Idempotency-Key": "fee-unrelated-date"},
        json={**base_fee, "assertion_ids": [assertions["contract_end_date"]["id"]]},
    )
    assert unrelated.status_code == 422, unrelated.text
    assert "not relevant" in unrelated.text

    mismatched = client.post(
        f"/v1/cases/{case['id']}/fees",
        headers={**analyst.headers, "Idempotency-Key": "fee-mismatched-amount"},
        json={
            **base_fee,
            "amount_minor": 11_999_999,
            "assertion_ids": [assertions["fee.subscription"]["id"]],
        },
    )
    assert mismatched.status_code == 422, mismatched.text

    missing_cadence = client.post(
        f"/v1/cases/{case['id']}/fees",
        headers={**analyst.headers, "Idempotency-Key": "fee-missing-cadence"},
        json={
            **base_fee,
            "billing_cadence": None,
            "assertion_ids": [assertions["fee.subscription"]["id"]],
        },
    )
    assert missing_cadence.status_code == 422, missing_cadence.text

    mismatched_cadence = client.post(
        f"/v1/cases/{case['id']}/fees",
        headers={**analyst.headers, "Idempotency-Key": "fee-mismatched-cadence"},
        json={
            **base_fee,
            "billing_cadence": "monthly",
            "assertion_ids": [assertions["fee.subscription"]["id"]],
        },
    )
    assert mismatched_cadence.status_code == 422, mismatched_cadence.text

    valid = client.post(
        f"/v1/cases/{case['id']}/fees",
        headers={**analyst.headers, "Idempotency-Key": "fee-exact-evidence"},
        json={**base_fee, "assertion_ids": [assertions["fee.subscription"]["id"]]},
    )
    assert valid.status_code == 201, valid.text
    assert valid.json()["reviewed"] is True


def test_readiness_rechecks_fee_relevance_and_marks_changed_inputs_stale(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="fee-readiness@demo.local", role=Role.ANALYST)
    case, assertions = _create_and_process(client, analyst, suffix="fee-readiness")
    for key in ("contract_end_date", "fee.subscription"):
        reviewed = _review(client, analyst, assertions[key], suffix=f"readiness-{key}")
        assert reviewed.status_code == 200, reviewed.text
        assertions[key] = reviewed.json()
    fee_response = client.post(
        f"/v1/cases/{case['id']}/fees",
        headers={**analyst.headers, "Idempotency-Key": "fee-readiness-exact"},
        json={
            "category": "subscription",
            "amount_minor": 12_000_000,
            "currency": "USD",
            "service_start": "2026-01-15",
            "service_end": "2027-01-15",
            "payment_status": "unpaid",
            "billing_cadence": "annual",
            "proration_rule": "contract_daily",
            "assertion_ids": [assertions["fee.subscription"]["id"]],
            "reviewed": True,
        },
    )
    assert fee_response.status_code == 201, fee_response.text
    calculation = client.post(
        f"/v1/cases/{case['id']}/calculations",
        headers={**analyst.headers, "Idempotency-Key": "fee-readiness-calc"},
        json={"as_of_date": date(2026, 8, 20).isoformat()},
    )
    assert calculation.status_code == 201, calculation.text
    assert any("service-period dates" in item for item in calculation.json()["assumptions"])

    fee = db.get(FeeObligation, fee_response.json()["id"])
    assert fee is not None
    fee.assertion_ids = [assertions["contract_end_date"]["id"]]
    db.commit()
    unrelated_readiness = client.get(f"/v1/cases/{case['id']}/readiness", headers=analyst.headers)
    assert unrelated_readiness.status_code == 200, unrelated_readiness.text
    unrelated_payload = unrelated_readiness.json()
    assert unrelated_payload["ready_for_internal_review"] is False
    assert unrelated_payload["reproducible_calculation_exists"] is True
    assert any(
        item["code"] == f"fee.{fee.id}.citation_missing" for item in unrelated_payload["findings"]
    )

    fee.assertion_ids = [assertions["fee.subscription"]["id"]]
    fee.amount_minor += 1
    db.commit()
    stale = client.get(f"/v1/cases/{case['id']}/readiness", headers=analyst.headers)
    assert stale.status_code == 200, stale.text
    stale_payload = stale.json()
    assert stale_payload["reproducible_calculation_exists"] is False
    assert any(item["code"] == "calculation.stale" for item in stale_payload["findings"])
    current = client.get(f"/v1/cases/{case['id']}", headers=analyst.headers).json()
    blocked = client.post(
        f"/v1/cases/{case['id']}/transitions",
        headers={**analyst.headers, "Idempotency-Key": "stale-calculation-ready"},
        json={
            "target": "ready_for_internal_review",
            "expected_version": current["version"],
            "reason": "Stale calculation must block readiness",
        },
    )
    assert blocked.status_code == 409, blocked.text


@pytest.mark.parametrize("correlation", ["x" * 65, "contains space", "unsafe/slash"])
def test_mutation_rejects_unsafe_correlation_ids(
    client: TestClient, db: Session, correlation: str
) -> None:
    analyst = seed_identity(
        db,
        email=f"correlation-{len(correlation)}-{ord(correlation[0])}@demo.local",
        role=Role.ANALYST,
        organization_name=f"Correlation {len(correlation)} {ord(correlation[0])}",
    )
    response = client.post(
        "/v1/cases",
        headers={
            **analyst.headers,
            "Idempotency-Key": f"bad-correlation-{len(correlation)}-{ord(correlation[0])}",
            "X-Correlation-ID": correlation,
        },
        json={
            "display_name": "Invalid correlation",
            "applicant_company": "Northstar Systems",
            "erp_provider": "LegacySuite",
        },
    )
    assert response.status_code == 422, response.text
    assert db.query(AuditEvent).count() == 0


def test_mutation_accepts_and_persists_a_safe_64_character_correlation_id(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="safe-correlation@demo.local", role=Role.ANALYST)
    correlation = "A" + "a" * 55 + ".:_-safe"
    assert len(correlation) == 64
    response = client.post(
        "/v1/cases",
        headers={
            **analyst.headers,
            "Idempotency-Key": "safe-correlation-case",
            "X-Correlation-ID": correlation,
        },
        json={
            "display_name": "Safe correlation",
            "applicant_company": "Northstar Systems",
            "erp_provider": "LegacySuite",
        },
    )
    assert response.status_code == 201, response.text
    event = db.query(AuditEvent).filter_by(action="case.created").one()
    assert event.correlation_id == correlation


def test_changed_correction_assumption_survives_repeated_review_readiness_and_export(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="assumption-lineage@demo.local", role=Role.ANALYST)
    case, assertions = _create_and_process(client, analyst, suffix="assumption-lineage")
    fee_review = _review(
        client,
        analyst,
        assertions["fee.subscription"],
        suffix="assumption-lineage-fee",
    )
    assert fee_review.status_code == 200, fee_review.text
    corrected = _review(
        client,
        analyst,
        assertions["contract_end_date"],
        suffix="assumption-lineage-first",
        decision="correct",
        corrected_value={"type": "date", "value": "2031-04-30"},
        assumption=True,
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["evidence_basis"] == "explicit_assumption"
    assert corrected.json()["assumption"] is True
    assert corrected.json()["review_reason"]
    assert corrected.json()["reviewed_by"] == "Assumption-Lineage"

    meaningless_accept = _review(
        client,
        analyst,
        corrected.json(),
        suffix="assumption-lineage-accept",
    )
    assert meaningless_accept.status_code == 409, meaningless_accept.text

    repeated_without_assumption = _review(
        client,
        analyst,
        corrected.json(),
        suffix="assumption-lineage-repeat-false",
        decision="correct",
        corrected_value={"type": "date", "value": "2031-04-30"},
    )
    assert repeated_without_assumption.status_code == 422, repeated_without_assumption.text
    repeated = _review(
        client,
        analyst,
        corrected.json(),
        suffix="assumption-lineage-repeat-true",
        decision="correct",
        corrected_value={"type": "date", "value": "2031-04-30"},
        assumption=True,
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["evidence_basis"] == "explicit_assumption"

    fee = client.post(
        f"/v1/cases/{case['id']}/fees",
        headers={**analyst.headers, "Idempotency-Key": "assumption-lineage-fee-create"},
        json={
            "category": "subscription",
            "amount_minor": fee_review.json()["normalized_value"]["amount_minor"],
            "currency": fee_review.json()["normalized_value"]["currency"],
            "billing_cadence": fee_review.json()["normalized_value"]["cadence"],
            "service_start": "2026-01-01",
            "service_end": "2027-01-01",
            "payment_status": "unpaid",
            "proration_rule": "contract_daily",
            "assertion_ids": [fee_review.json()["id"]],
            "reviewed": True,
        },
    )
    assert fee.status_code == 201, fee.text
    calculation = client.post(
        f"/v1/cases/{case['id']}/calculations",
        headers={**analyst.headers, "Idempotency-Key": "assumption-lineage-calculation"},
        json={"as_of_date": "2026-08-21"},
    )
    assert calculation.status_code == 201, calculation.text
    readiness = client.get(f"/v1/cases/{case['id']}/readiness", headers=analyst.headers)
    assert readiness.status_code == 200, readiness.text
    assert readiness.json()["mandatory_assertions_reviewed"] is True
    assert not any(
        item["code"].endswith("governance_invalid") for item in readiness.json()["findings"]
    )

    exported = client.post(
        f"/v1/cases/{case['id']}/exports",
        headers={**analyst.headers, "Idempotency-Key": "assumption-lineage-export"},
        json={
            "kind": "machine_readable_json",
            "calculation_id": calculation.json()["id"],
        },
    )
    assert exported.status_code == 201, exported.text
    packet = client.get(exported.json()["download_url"], headers=analyst.headers).json()
    fact = next(
        item for item in packet["snapshot"]["facts"] if item["fact_id"] == repeated.json()["id"]
    )
    assert fact["provenance"]["evidence_basis"] == "explicit_assumption"
    assert fact["provenance"]["assumption"] is True

    first_decision = db.scalar(
        select(ReviewDecision)
        .where(
            ReviewDecision.decision == ReviewDecisionType.CORRECT,
            ReviewDecision.assumption.is_(True),
        )
        .order_by(ReviewDecision.created_at, ReviewDecision.id)
    )
    assert first_decision is not None
    first_decision.assumption = False
    db.commit()
    tampered_readiness = client.get(f"/v1/cases/{case['id']}/readiness", headers=analyst.headers)
    assert tampered_readiness.json()["mandatory_assertions_reviewed"] is False
    tampered_export = client.post(
        f"/v1/cases/{case['id']}/exports",
        headers={**analyst.headers, "Idempotency-Key": "assumption-lineage-tampered-export"},
        json={"kind": "evidence_csv", "calculation_id": calculation.json()["id"]},
    )
    assert tampered_export.status_code == 409, tampered_export.text
    assert "valid audited review decision" in tampered_export.text


def test_reopen_preserves_explicit_assumption_lineage_through_readiness_and_export(
    client: TestClient,
    db: Session,
) -> None:
    analyst = seed_identity(db, email="reopen-assumption@demo.local", role=Role.ANALYST)
    approver = seed_identity(db, email="reopen-approver@demo.local", role=Role.APPROVER)
    case, assertions = _create_and_process(client, analyst, suffix="reopen-assumption")
    fee_review = _review(
        client,
        analyst,
        assertions["fee.subscription"],
        suffix="reopen-assumption-fee",
    )
    assert fee_review.status_code == 200, fee_review.text
    for semantic_key, assertion in assertions.items():
        if semantic_key in {"fee.subscription", "contract_end_date"}:
            continue
        reviewed = _review(
            client,
            analyst,
            assertion,
            suffix=f"reopen-assumption-initial-{semantic_key}",
        )
        assert reviewed.status_code == 200, reviewed.text
    corrected = _review(
        client,
        analyst,
        assertions["contract_end_date"],
        suffix="reopen-assumption-end",
        decision="correct",
        corrected_value={"type": "date", "value": "2031-04-30"},
        assumption=True,
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["evidence_basis"] == "explicit_assumption"

    initial_fee = client.post(
        f"/v1/cases/{case['id']}/fees",
        headers={**analyst.headers, "Idempotency-Key": "reopen-assumption-initial-fee"},
        json={
            "category": "subscription",
            "amount_minor": fee_review.json()["normalized_value"]["amount_minor"],
            "currency": fee_review.json()["normalized_value"]["currency"],
            "billing_cadence": fee_review.json()["normalized_value"]["cadence"],
            "service_start": "2026-01-01",
            "service_end": "2027-01-01",
            "payment_status": "unpaid",
            "proration_rule": "contract_daily",
            "assertion_ids": [fee_review.json()["id"]],
            "reviewed": True,
        },
    )
    assert initial_fee.status_code == 201, initial_fee.text
    initial_calculation = client.post(
        f"/v1/cases/{case['id']}/calculations",
        headers={**analyst.headers, "Idempotency-Key": "reopen-assumption-initial-calc"},
        json={"as_of_date": "2026-08-21"},
    )
    assert initial_calculation.status_code == 201, initial_calculation.text

    current_case = client.get(f"/v1/cases/{case['id']}", headers=analyst.headers).json()
    initial_readiness = client.get(
        f"/v1/cases/{case['id']}/readiness",
        headers=analyst.headers,
    )
    assert initial_readiness.status_code == 200, initial_readiness.text
    assert [
        (item["code"], item["detail"])
        for item in initial_readiness.json()["findings"]
        if item["severity"] == "blocking" and item["status"] == "open"
    ] == []
    ready = client.post(
        f"/v1/cases/{case['id']}/transitions",
        headers={**analyst.headers, "Idempotency-Key": "reopen-assumption-ready"},
        json={
            "target": "ready_for_internal_review",
            "expected_version": current_case["version"],
            "reason": "The reviewed evidence and reproducible calculation are ready.",
        },
    )
    assert ready.status_code == 200, ready.text
    approved = client.post(
        f"/v1/cases/{case['id']}/transitions",
        headers={**approver.headers, "Idempotency-Key": "reopen-assumption-approved"},
        json={
            "target": "internal_packet_approved",
            "expected_version": ready.json()["version"],
            "reason": "Human approval seals the explicit-assumption snapshot.",
        },
    )
    assert approved.status_code == 200, approved.text
    reopened = client.post(
        f"/v1/cases/{case['id']}/transitions",
        headers={**approver.headers, "Idempotency-Key": "reopen-assumption-reopen"},
        json={
            "target": "evidence_review",
            "expected_version": approved.json()["version"],
            "reason": "New revision review while preserving governed assumptions.",
        },
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["current_revision_number"] == 2

    reopened_assertions_response = client.get(
        f"/v1/cases/{case['id']}/assertions",
        headers=analyst.headers,
    )
    assert reopened_assertions_response.status_code == 200, reopened_assertions_response.text
    reopened_assertions = {
        item["semantic_key"]: item for item in reopened_assertions_response.json()
    }
    inherited = reopened_assertions["contract_end_date"]
    assert inherited["review_state"] == "corrected"
    assert inherited["evidence_basis"] == "explicit_assumption"
    assert inherited["assumption"] is True
    laundering_attempt = _review(
        client,
        analyst,
        inherited,
        suffix="reopen-assumption-launder",
        decision="accept",
    )
    assert laundering_attempt.status_code == 409, laundering_attempt.text

    reopened_reviews: dict[str, Any] = {}
    for semantic_key, assertion in reopened_assertions.items():
        if semantic_key == "contract_end_date":
            continue
        reviewed = _review(
            client,
            analyst,
            assertion,
            suffix=f"reopen-assumption-again-{semantic_key}",
        )
        assert reviewed.status_code == 200, reviewed.text
        reopened_reviews[semantic_key] = reviewed
    reopened_fee_assertion = reopened_reviews["fee.subscription"]
    copied_fees = client.get(
        f"/v1/cases/{case['id']}/fees",
        headers=analyst.headers,
    )
    assert copied_fees.status_code == 200, copied_fees.text
    copied_fee = next(item for item in copied_fees.json() if not item["superseded"])
    superseded = client.post(
        f"/v1/cases/{case['id']}/fees/{copied_fee['id']}/supersede",
        headers={**analyst.headers, "Idempotency-Key": "reopen-assumption-retire-fee"},
        json={
            "expected_case_version": reopened.json()["version"],
            "reason": "Re-enter the obligation against the reviewed revision-two assertion.",
        },
    )
    assert superseded.status_code == 200, superseded.text
    replacement_fee = client.post(
        f"/v1/cases/{case['id']}/fees",
        headers={**analyst.headers, "Idempotency-Key": "reopen-assumption-replacement-fee"},
        json={
            "category": "subscription",
            "amount_minor": reopened_fee_assertion.json()["normalized_value"]["amount_minor"],
            "currency": reopened_fee_assertion.json()["normalized_value"]["currency"],
            "billing_cadence": reopened_fee_assertion.json()["normalized_value"]["cadence"],
            "service_start": "2026-01-01",
            "service_end": "2027-01-01",
            "payment_status": "unpaid",
            "proration_rule": "contract_daily",
            "assertion_ids": [reopened_fee_assertion.json()["id"]],
            "reviewed": True,
        },
    )
    assert replacement_fee.status_code == 201, replacement_fee.text
    calculation = client.post(
        f"/v1/cases/{case['id']}/calculations",
        headers={**analyst.headers, "Idempotency-Key": "reopen-assumption-new-calc"},
        json={"as_of_date": "2026-08-21"},
    )
    assert calculation.status_code == 201, calculation.text
    readiness = client.get(f"/v1/cases/{case['id']}/readiness", headers=analyst.headers)
    assert readiness.status_code == 200, readiness.text
    assert readiness.json()["mandatory_assertions_reviewed"] is True
    assert not any(
        item["code"].endswith("governance_invalid") for item in readiness.json()["findings"]
    )

    exported = client.post(
        f"/v1/cases/{case['id']}/exports",
        headers={**analyst.headers, "Idempotency-Key": "reopen-assumption-export"},
        json={
            "kind": "machine_readable_json",
            "calculation_id": calculation.json()["id"],
        },
    )
    assert exported.status_code == 201, exported.text
    packet = client.get(exported.json()["download_url"], headers=analyst.headers).json()
    fact = next(item for item in packet["snapshot"]["facts"] if item["fact_id"] == inherited["id"])
    assert fact["provenance"]["evidence_basis"] == "explicit_assumption"
    assert fact["provenance"]["assumption"] is True


def test_extraction_conflict_can_be_corrected_without_laundering_assumption_lineage(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="derived-conflict@demo.local", role=Role.ANALYST)
    case, _ = _create_and_process(client, analyst, suffix="derived-conflict-first")
    case_id = str(case["id"])
    second = client.post(
        f"/v1/cases/{case_id}/documents",
        headers={**analyst.headers, "Idempotency-Key": "derived-conflict-second-upload"},
        data={"source_type": "synthetic"},
        files={
            "file": (
                "fee_and_date_variants.pdf",
                (FIXTURES / "fee_and_date_variants.pdf").read_bytes(),
                "application/pdf",
            )
        },
    )
    assert second.status_code == 201, second.text
    processed = client.post(
        f"/v1/documents/{second.json()['id']}/process",
        headers={**analyst.headers, "Idempotency-Key": "derived-conflict-second-process"},
    )
    assert processed.status_code == 200, processed.text

    listed = client.get(f"/v1/cases/{case_id}/assertions", headers=analyst.headers)
    assert listed.status_code == 200, listed.text
    assertions = cast(list[dict[str, Any]], listed.json())
    conflict = next(item for item in assertions if item["semantic_key"] == "contract_end_date")
    assert conflict["source"] == "derived"
    assert conflict["review_state"] == "conflicting"
    persisted_conflict = db.get(ContractAssertion, conflict["id"])
    assert persisted_conflict is not None
    assert persisted_conflict.supersedes_assertion_id is not None
    assert (
        db.query(ReviewDecision)
        .filter_by(assertion_id=persisted_conflict.supersedes_assertion_id)
        .count()
        == 0
    )

    reviewed: dict[str, dict[str, Any]] = {}
    for assertion in assertions:
        normalized = cast(dict[str, Any], assertion["normalized_value"])
        if assertion["review_state"] == "conflicting":
            candidates = cast(list[dict[str, Any]], normalized["candidates"])
            response = _review(
                client,
                analyst,
                assertion,
                suffix=f"derived-conflict-{assertion['semantic_key']}",
                decision="correct",
                corrected_value=candidates[0],
                assumption=True,
            )
        else:
            response = _review(
                client,
                analyst,
                assertion,
                suffix=f"derived-conflict-{assertion['semantic_key']}",
            )
        assert response.status_code == 200, response.text
        reviewed[str(assertion["semantic_key"])] = cast(dict[str, Any], response.json())

    corrected_end = reviewed["contract_end_date"]
    assert corrected_end["assumption"] is True
    assert corrected_end["evidence_basis"] == "explicit_assumption"
    assert corrected_end["review_reason"] == "Adversarial evidence-contract verification"
    assert corrected_end["reviewed_by"] == "Derived-Conflict"

    subscription = reviewed["fee.subscription"]
    money = cast(dict[str, Any], subscription["normalized_value"])
    fee = client.post(
        f"/v1/cases/{case_id}/fees",
        headers={**analyst.headers, "Idempotency-Key": "derived-conflict-fee"},
        json={
            "category": "subscription",
            "amount_minor": money["amount_minor"],
            "currency": money["currency"],
            "service_start": "2026-01-15",
            "service_end": "2027-01-15",
            "payment_status": "unpaid",
            "billing_cadence": money["cadence"],
            "proration_rule": "contract_daily",
            "assertion_ids": [subscription["id"]],
            "reviewed": True,
        },
    )
    assert fee.status_code == 201, fee.text
    calculation = client.post(
        f"/v1/cases/{case_id}/calculations",
        headers={**analyst.headers, "Idempotency-Key": "derived-conflict-calculation"},
        json={"as_of_date": "2026-08-21"},
    )
    assert calculation.status_code == 201, calculation.text
    readiness = client.get(f"/v1/cases/{case_id}/readiness", headers=analyst.headers)
    assert readiness.status_code == 200, readiness.text
    assert readiness.json()["mandatory_assertions_reviewed"] is True
    assert readiness.json()["ready_for_internal_review"] is True
    assert not any(
        item["code"].endswith("governance_invalid") for item in readiness.json()["findings"]
    )

    exported = client.post(
        f"/v1/cases/{case_id}/exports",
        headers={**analyst.headers, "Idempotency-Key": "derived-conflict-export"},
        json={
            "kind": "machine_readable_json",
            "calculation_id": calculation.json()["id"],
        },
    )
    assert exported.status_code == 201, exported.text
    packet = client.get(exported.json()["download_url"], headers=analyst.headers).json()
    fact = next(
        item for item in packet["snapshot"]["facts"] if item["fact_id"] == corrected_end["id"]
    )
    assert fact["provenance"]["evidence_basis"] == "explicit_assumption"
    assert fact["provenance"]["assumption"] is True
    assert fact["provenance"]["source_assertion_id"] == conflict["id"]


def test_unknown_payment_is_blocked_and_cannot_be_laundered_through_export_snapshots(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="unknown-payment@demo.local", role=Role.ANALYST)
    case, assertions = _create_and_process(client, analyst, suffix="unknown-payment")
    for key in ("contract_end_date", "fee.subscription"):
        reviewed = _review(client, analyst, assertions[key], suffix=f"unknown-payment-{key}")
        assert reviewed.status_code == 200, reviewed.text
        assertions[key] = reviewed.json()
    fee = client.post(
        f"/v1/cases/{case['id']}/fees",
        headers={**analyst.headers, "Idempotency-Key": "unknown-payment-fee"},
        json={
            "category": "subscription",
            "amount_minor": 12_000_000,
            "currency": "USD",
            "service_start": "2026-01-15",
            "service_end": "2027-01-15",
            "payment_status": "unknown",
            "billing_cadence": "annual",
            "proration_rule": "contract_daily",
            "assertion_ids": [assertions["fee.subscription"]["id"]],
            "reviewed": True,
        },
    )
    assert fee.status_code == 201, fee.text
    calculation = client.post(
        f"/v1/cases/{case['id']}/calculations",
        headers={**analyst.headers, "Idempotency-Key": "unknown-payment-calculation"},
        json={"as_of_date": "2026-08-21"},
    )
    assert calculation.status_code == 201, calculation.text
    calculation_payload = calculation.json()
    assert calculation_payload["line_items"][0]["formula_identifier"] == "payment-status-unknown"
    assert calculation_payload["line_items"][0]["remaining_amount_minor"] is None
    assert any(
        "cannot be treated as unpaid" in item for item in calculation_payload["blocking_findings"]
    )

    readiness = client.get(f"/v1/cases/{case['id']}/readiness", headers=analyst.headers)
    assert readiness.status_code == 200, readiness.text
    assert readiness.json()["reproducible_calculation_exists"] is True
    assert readiness.json()["ready_for_internal_review"] is False
    assert any(
        "cannot be treated as unpaid" in item["detail"] for item in readiness.json()["findings"]
    )

    downloads: dict[str, bytes] = {}
    for kind in ("machine_readable_json", "evidence_csv", "internal_review_pdf"):
        generated = client.post(
            f"/v1/cases/{case['id']}/exports",
            headers={**analyst.headers, "Idempotency-Key": f"unknown-payment-{kind}"},
            json={"kind": kind, "calculation_id": calculation_payload["id"]},
        )
        assert generated.status_code == 201, generated.text
        downloaded = client.get(generated.json()["download_url"], headers=analyst.headers)
        assert downloaded.status_code == 200, downloaded.text
        downloads[kind] = downloaded.content
    machine = __import__("json").loads(downloads["machine_readable_json"])
    scenario = machine["snapshot"]["calculation"]["currency_scenarios"][0]
    assert scenario["documented_remaining_subscription_minor"] == 0
    assert scenario["unclassified_minor"] == 12_000_000
    assert machine["snapshot"]["calculation"]["lines"][0]["result_amount_minor"] is None
    assert "payment-status-unknown" in downloads["evidence_csv"].decode("utf-8")
    with fitz.open(stream=downloads["internal_review_pdf"], filetype="pdf") as document:
        pdf_text = "\n".join(page.get_text() for page in document)
    assert "payment status is unknown" in pdf_text.casefold()

    stored = db.get(CalculationRun, calculation_payload["id"])
    assert stored is not None
    tampered_snapshot = copy.deepcopy(stored.result_snapshot)
    tampered_snapshot["currencies"][0]["documented_remaining_subscription_minor"] = 12_000_000
    tampered_snapshot["currencies"][0]["unclassified_minor"] = 0
    tampered_snapshot["blocking_findings"] = []
    stored.result_snapshot = tampered_snapshot
    db.commit()

    stale_download = client.get(
        next(
            item["download_url"]
            for item in client.get(
                f"/v1/cases/{case['id']}/exports", headers=analyst.headers
            ).json()
            if item["kind"] == "machine_readable_json"
        ),
        headers=analyst.headers,
    )
    assert stale_download.status_code == 409, stale_download.text

    tampered_readiness = client.get(f"/v1/cases/{case['id']}/readiness", headers=analyst.headers)
    assert tampered_readiness.status_code == 200, tampered_readiness.text
    assert tampered_readiness.json()["reproducible_calculation_exists"] is False
    assert any(
        item["code"] == "calculation.stale" for item in tampered_readiness.json()["findings"]
    )
    assert any(
        "cannot be treated as unpaid" in item["detail"]
        for item in tampered_readiness.json()["findings"]
    )
    rejected_export = client.post(
        f"/v1/cases/{case['id']}/exports",
        headers={**analyst.headers, "Idempotency-Key": "unknown-payment-tampered-export"},
        json={
            "kind": "customer_explanation_pdf",
            "calculation_id": calculation_payload["id"],
        },
    )
    assert rejected_export.status_code == 409, rejected_export.text
