from __future__ import annotations

import stat
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import cast

import pytest
from httpx import Response
from reportlab.pdfgen import canvas
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from rescue_desk.config import get_settings
from rescue_desk.extraction import ClauseExtractionResult, ExtractedAssertion
from rescue_desk.models import (
    AssertionReviewState,
    AssertionSource,
    AuditEvent,
    CalculationRun,
    CaseRevision,
    ContractAssertion,
    ContractDocument,
    ExportArtifact,
    FeeObligation,
    FindingSeverity,
    ReadinessFinding,
    RescueCase,
    Role,
)
from tests.integration.conftest import SeededIdentity, seed_identity

FIXTURES = Path(__file__).parents[3] / "fixtures" / "contracts"


def _create_case(client: TestClient, identity: SeededIdentity, *, key: str) -> dict[str, object]:
    response = client.post(
        "/v1/cases",
        headers={**identity.headers, "Idempotency-Key": key},
        json={
            "display_name": "Synthetic ERP exit review",
            "applicant_company": "Northstar Systems",
            "erp_provider": "LegacySuite",
        },
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, object], response.json())


def _upload(
    client: TestClient,
    identity: SeededIdentity,
    case_id: str,
    fixture_name: str,
    *,
    key: str,
    filename: str | None = None,
) -> Response:
    return cast(
        Response,
        client.post(
            f"/v1/cases/{case_id}/documents",
            headers={**identity.headers, "Idempotency-Key": key},
            data={"source_type": "synthetic"},
            files={
                "file": (
                    filename or fixture_name,
                    (FIXTURES / fixture_name).read_bytes(),
                    "application/pdf",
                )
            },
        ),
    )


def _process(
    client: TestClient, identity: SeededIdentity, document_id: str, *, key: str
) -> Response:
    return cast(
        Response,
        client.post(
            f"/v1/documents/{document_id}/process",
            headers={**identity.headers, "Idempotency-Key": key},
        ),
    )


def _pdf_bytes_with_lines(*lines: str) -> bytes:
    buffer = BytesIO()
    document = canvas.Canvas(buffer)
    y_position = 760
    for line in lines:
        document.drawString(54, y_position, line)
        y_position -= 18
    document.save()
    return buffer.getvalue()


@pytest.mark.parametrize("source_type", ["private", "x" * 31])
def test_upload_rejects_invalid_source_provenance_before_database_write(
    client: TestClient, db: Session, source_type: str
) -> None:
    analyst = seed_identity(
        db,
        email=f"invalid-source-{len(source_type)}@demo.local",
        role=Role.ANALYST,
    )
    case = _create_case(client, analyst, key=f"invalid-source-case-{len(source_type)}")

    response = client.post(
        f"/v1/cases/{case['id']}/documents",
        headers={**analyst.headers, "Idempotency-Key": f"invalid-source-{len(source_type)}"},
        data={"source_type": source_type},
        files={
            "file": (
                "clean_standard.pdf",
                (FIXTURES / "clean_standard.pdf").read_bytes(),
                "application/pdf",
            )
        },
    )

    assert response.status_code == 422, response.text
    assert db.query(ContractDocument).count() == 0


def test_upload_requires_explicit_source_provenance(
    client: TestClient,
    db: Session,
) -> None:
    analyst = seed_identity(db, email="missing-source@demo.local", role=Role.ANALYST)
    case = _create_case(client, analyst, key="missing-source-case")

    response = client.post(
        f"/v1/cases/{case['id']}/documents",
        headers={**analyst.headers, "Idempotency-Key": "missing-source-upload"},
        files={
            "file": (
                "clean_standard.pdf",
                (FIXTURES / "clean_standard.pdf").read_bytes(),
                "application/pdf",
            )
        },
    )

    assert response.status_code == 422, response.text
    assert any(item["loc"][-1] == "source_type" for item in response.json()["detail"])
    assert db.query(ContractDocument).count() == 0


def test_invalid_labelled_money_persists_blocker_and_prevents_readiness(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="invalid-labelled-money@demo.local", role=Role.ANALYST)
    case = _create_case(client, analyst, key="invalid-labelled-money-case")
    uploaded = client.post(
        f"/v1/cases/{case['id']}/documents",
        headers={**analyst.headers, "Idempotency-Key": "invalid-labelled-money-upload"},
        data={"source_type": "synthetic"},
        files={
            "file": (
                "invalid-labelled-money.pdf",
                _pdf_bytes_with_lines(
                    "Initial Term End Date: December 31, 2027",
                    "Annual Subscription Fee: KWD 1.2345",
                ),
                "application/pdf",
            )
        },
    )
    assert uploaded.status_code == 201, uploaded.text
    processed = _process(
        client,
        analyst,
        str(uploaded.json()["id"]),
        key="invalid-labelled-money-process",
    )
    assert processed.status_code == 200, processed.text

    finding = db.query(ReadinessFinding).filter_by(code="extraction.invalid_labelled_money").one()
    assert finding.severity == FindingSeverity.BLOCKING
    assert "KWD" not in finding.detail
    assert "1.2345" not in finding.detail
    readiness = client.get(f"/v1/cases/{case['id']}/readiness", headers=analyst.headers)
    assert readiness.status_code == 200, readiness.text
    assert readiness.json()["ready_for_internal_review"] is False
    assert any(
        item["code"] == "extraction.invalid_labelled_money" and item["severity"] == "blocking"
        for item in readiness.json()["findings"]
    )
    transition = client.post(
        f"/v1/cases/{case['id']}/transitions",
        headers={**analyst.headers, "Idempotency-Key": "invalid-labelled-money-ready"},
        json={
            "target": "ready_for_internal_review",
            "expected_version": processed.json().get("case_version", 2),
            "reason": "This must remain blocked by the malformed labelled obligation.",
        },
    )
    assert transition.status_code == 409, transition.text


def test_clean_pdf_upload_process_and_exact_citations_are_idempotent(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="documents@demo.local", role=Role.ANALYST)
    case = _create_case(client, analyst, key="documents-case")
    case_id = str(case["id"])

    first = _upload(
        client,
        analyst,
        case_id,
        "clean_standard.pdf",
        key="upload-clean",
        filename="../../clean_standard.pdf",
    )
    assert first.status_code == 201, first.text
    document = first.json()
    assert document["original_filename"] == "clean_standard.pdf"
    assert document["safety_status"] == "passed"
    persisted_document = db.get(ContractDocument, str(document["id"]))
    assert persisted_document is not None
    stored_path = get_settings().upload_dir / persisted_document.stored_filename
    assert stat.S_IMODE(stored_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(get_settings().upload_dir.stat().st_mode) == 0o700
    replay = _upload(
        client,
        analyst,
        case_id,
        "clean_standard.pdf",
        key="upload-clean",
        filename="../../clean_standard.pdf",
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == document["id"]
    duplicate = _upload(
        client,
        analyst,
        case_id,
        "clean_standard.pdf",
        key="upload-clean-again",
    )
    assert duplicate.status_code == 409

    processed = _process(client, analyst, str(document["id"]), key="process-clean")
    assert processed.status_code == 200, processed.text
    assert processed.json()["processing_status"] == "processed"
    process_replay = _process(client, analyst, str(document["id"]), key="process-clean")
    assert process_replay.status_code == 200
    assert process_replay.json()["id"] == document["id"]

    pages_response = client.get(f"/v1/documents/{document['id']}/pages", headers=analyst.headers)
    assert pages_response.status_code == 200
    pages = {item["page_number"]: item for item in pages_response.json()}
    assert len(pages) == 1
    assertions_response = client.get(f"/v1/cases/{case_id}/assertions", headers=analyst.headers)
    assert assertions_response.status_code == 200, assertions_response.text
    assertions = {item["semantic_key"]: item for item in assertions_response.json()}
    assert assertions["contract_end_date"]["normalized_value"] == {
        "type": "date",
        "value": "2029-01-14",
    }
    assert assertions["fee.subscription"]["normalized_value"]["amount_minor"] == 12_000_000
    for assertion in assertions.values():
        assert assertion["evidence"]
        for evidence in assertion["evidence"]:
            assert evidence["document_id"] == document["id"]
            page_text = pages[evidence["page_number"]]["text"]
            assert page_text[evidence["char_start"] : evidence["char_end"]] == evidence["quote"]
    case_response = client.get(f"/v1/cases/{case_id}", headers=analyst.headers)
    assert case_response.json()["status"] == "evidence_review"
    file_response = client.get(f"/v1/documents/{document['id']}/file", headers=analyst.headers)
    assert file_response.status_code == 200
    assert file_response.content.startswith(b"%PDF")


@pytest.mark.parametrize(
    ("fixture_name", "expected_code"),
    [
        ("safety_invalid_header.pdf", "invalid_header"),
        ("safety_encrypted.pdf", "encrypted_pdf"),
        ("safety_embedded.pdf", "embedded_files"),
    ],
)
def test_unsafe_pdf_uploads_fail_closed(
    client: TestClient,
    db: Session,
    fixture_name: str,
    expected_code: str,
) -> None:
    analyst = seed_identity(db, email=f"{expected_code}@demo.local", role=Role.ANALYST)
    case = _create_case(client, analyst, key=f"case-{expected_code}")
    response = _upload(
        client,
        analyst,
        str(case["id"]),
        fixture_name,
        key=f"upload-{expected_code}",
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == expected_code
    assert db.query(ContractDocument).count() == 0


def test_prompt_injection_text_is_flagged_and_never_changes_extracted_values(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="injection@demo.local", role=Role.ANALYST)
    case = _create_case(client, analyst, key="injection-case")
    uploaded = _upload(
        client,
        analyst,
        str(case["id"]),
        "prompt_injection.pdf",
        key="injection-upload",
    )
    assert uploaded.status_code == 201
    processed = _process(client, analyst, uploaded.json()["id"], key="injection-process")
    assert processed.status_code == 200
    assertions = client.get(f"/v1/cases/{case['id']}/assertions", headers=analyst.headers).json()
    subscription = next(item for item in assertions if item["semantic_key"] == "fee.subscription")
    assert subscription["normalized_value"]["amount_minor"] == 9_600_000
    assert subscription["normalized_value"]["amount_minor"] != 100
    findings = db.query(ReadinessFinding).all()
    assert any(item.code == "extraction.prompt_injection_text" for item in findings)


def test_scanned_pdf_requires_ocr_and_blocks_review(client: TestClient, db: Session) -> None:
    analyst = seed_identity(db, email="ocr@demo.local", role=Role.ANALYST)
    case = _create_case(client, analyst, key="ocr-case")
    uploaded = _upload(
        client,
        analyst,
        str(case["id"]),
        "scan_needs_ocr.pdf",
        key="ocr-upload",
    )
    processed = _process(client, analyst, uploaded.json()["id"], key="ocr-process")
    assert processed.status_code == 200
    assert processed.json()["processing_status"] == "needs_ocr"
    assert (
        db.query(ReadinessFinding)
        .filter_by(code="document.ocr_required", severity="BLOCKING")
        .count()
        == 1
    )
    assertions = client.get(f"/v1/cases/{case['id']}/assertions", headers=analyst.headers)
    assert assertions.json() == []
    readiness = client.get(f"/v1/cases/{case['id']}/readiness", headers=analyst.headers)
    assert readiness.json()["ready_for_internal_review"] is False


def test_document_file_and_pages_are_tenant_isolated(client: TestClient, db: Session) -> None:
    first = seed_identity(db, email="owner@demo.local", role=Role.ANALYST)
    second = seed_identity(
        db,
        email="outsider@demo.local",
        role=Role.ANALYST,
        organization_name="Outside Organization",
    )
    case = _create_case(client, first, key="tenant-doc-case")
    uploaded = _upload(
        client,
        first,
        str(case["id"]),
        "clean_standard.pdf",
        key="tenant-doc-upload",
    )
    document_id = uploaded.json()["id"]
    assert _process(client, first, document_id, key="tenant-process").status_code == 200
    for suffix in ("", "/pages", "/file"):
        response = client.get(f"/v1/documents/{document_id}{suffix}", headers=second.headers)
        assert response.status_code == 404


def test_failed_processing_is_recorded_and_can_be_retried(client: TestClient, db: Session) -> None:
    analyst = seed_identity(db, email="retry@demo.local", role=Role.ANALYST)
    case = _create_case(client, analyst, key="retry-case")
    source = (FIXTURES / "clean_standard.pdf").read_bytes()
    uploaded = _upload(
        client,
        analyst,
        str(case["id"]),
        "clean_standard.pdf",
        key="retry-upload",
    )
    document_id = uploaded.json()["id"]
    document = db.get(ContractDocument, document_id)
    assert document is not None
    stored_path = get_settings().upload_dir / document.stored_filename
    stored_path.unlink()
    failed = _process(client, analyst, document_id, key="retry-process-failed")
    assert failed.status_code == 422
    detail = client.get(f"/v1/documents/{document_id}", headers=analyst.headers).json()
    assert detail["processing_status"] == "failed"
    stored_path.write_bytes(source)
    retried = _process(client, analyst, document_id, key="retry-process-success")
    assert retried.status_code == 200
    assert retried.json()["processing_status"] == "processed"


def test_ai_disagreement_becomes_conflict_instead_of_overwriting_evidence(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rescue_desk.services import documents as document_service

    analyst = seed_identity(db, email="ai-conflict@demo.local", role=Role.ANALYST)
    case = _create_case(client, analyst, key="ai-conflict-case")
    uploaded = _upload(
        client,
        analyst,
        str(case["id"]),
        "clean_standard.pdf",
        key="ai-conflict-upload",
    )
    quote = "Initial Term End Date: January 14, 2029"

    def conflicting_result(*_: object, **__: object) -> ClauseExtractionResult:
        return ClauseExtractionResult(
            extractor="ollama-test-double",
            model_identifier="local-test-model",
            assertions=(
                ExtractedAssertion(
                    semantic_key="contract.initial_term_end_date",
                    raw_value="January 14, 2030",
                    normalized_value={"type": "date", "value": "2030-01-14"},
                    display_value="January 14, 2030",
                    confidence=Decimal("0.6000"),
                    page_number=1,
                    quote=quote,
                    char_start=148,
                    char_end=190,
                ),
            ),
        )

    monkeypatch.setenv("RESCUEDESK_ENABLE_LOCAL_AI", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(document_service, "extract_with_ollama", conflicting_result)
    processed = _process(client, analyst, uploaded.json()["id"], key="ai-conflict-process")
    assert processed.status_code == 200, processed.text
    assertions = client.get(f"/v1/cases/{case['id']}/assertions", headers=analyst.headers).json()
    contract_end = next(item for item in assertions if item["semantic_key"] == "contract_end_date")
    assert contract_end["review_state"] == "conflicting"
    assert contract_end["source"] == "derived"
    candidates = contract_end["normalized_value"]["candidates"]
    assert {item["value"] for item in candidates} == {"2029-01-14", "2030-01-14"}
    assert len(contract_end["evidence"]) == 2


def test_new_evidence_after_approval_creates_a_reviewable_revision(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="revision@demo.local", role=Role.ANALYST)
    approver = seed_identity(db, email="revision-approver@demo.local", role=Role.APPROVER)
    case = _create_case(client, analyst, key="revision-case")
    case_id = str(case["id"])
    uploaded = _upload(
        client,
        analyst,
        case_id,
        "clean_standard.pdf",
        key="revision-upload-one",
    )
    assert (
        _process(client, analyst, uploaded.json()["id"], key="revision-process-one").status_code
        == 200
    )
    assertions = client.get(f"/v1/cases/{case_id}/assertions", headers=analyst.headers).json()
    contract_end = next(item for item in assertions if item["semantic_key"] == "contract_end_date")
    accepted = client.post(
        f"/v1/cases/assertions/{contract_end['id']}/reviews",
        headers={**analyst.headers, "Idempotency-Key": "accept-contract-end"},
        json={
            "decision": "accept",
            "expected_assertion_version": 1,
            "reason": "Exact citation checked against the synthetic contract",
            "assumption": False,
        },
    )
    assert accepted.status_code == 200, accepted.text
    subscription_assertion = next(
        item for item in assertions if item["semantic_key"] == "fee.subscription"
    )
    accepted_subscription = client.post(
        f"/v1/cases/assertions/{subscription_assertion['id']}/reviews",
        headers={**analyst.headers, "Idempotency-Key": "accept-subscription-fee"},
        json={
            "decision": "accept",
            "expected_assertion_version": subscription_assertion["version"],
            "reason": "Exact fee citation checked against the synthetic contract",
            "assumption": False,
        },
    )
    assert accepted_subscription.status_code == 200, accepted_subscription.text
    fee = client.post(
        f"/v1/cases/{case_id}/fees",
        headers={**analyst.headers, "Idempotency-Key": "revision-fee"},
        json={
            "category": "subscription",
            "amount_minor": 12_000_000,
            "currency": "USD",
            "service_start": "2026-01-15",
            "service_end": "2027-01-15",
            "payment_status": "unpaid",
            "billing_cadence": "annual",
            "proration_rule": "contract_daily",
            "assertion_ids": [
                next(
                    item["id"] for item in assertions if item["semantic_key"] == "fee.subscription"
                )
            ],
            "reviewed": True,
        },
    )
    assert fee.status_code == 201, fee.text
    calculation = client.post(
        f"/v1/cases/{case_id}/calculations",
        headers={**analyst.headers, "Idempotency-Key": "revision-calculation"},
        json={"as_of_date": date(2026, 8, 20).isoformat()},
    )
    assert calculation.status_code == 201, calculation.text
    current = client.get(f"/v1/cases/{case_id}", headers=analyst.headers).json()
    premature_ready = client.post(
        f"/v1/cases/{case_id}/transitions",
        headers={**analyst.headers, "Idempotency-Key": "revision-ready-premature"},
        json={
            "target": "ready_for_internal_review",
            "expected_version": current["version"],
            "reason": "Attempt before every proposed assertion is reviewed",
        },
    )
    assert premature_ready.status_code == 409
    for assertion in assertions:
        if assertion["id"] in {contract_end["id"], subscription_assertion["id"]}:
            continue
        reviewed = client.post(
            f"/v1/cases/assertions/{assertion['id']}/reviews",
            headers={
                **analyst.headers,
                "Idempotency-Key": f"accept-revision-{assertion['id']}",
            },
            json={
                "decision": "accept",
                "expected_assertion_version": assertion["version"],
                "reason": "Exact citation checked against the synthetic contract",
                "assumption": False,
            },
        )
        assert reviewed.status_code == 200, reviewed.text
    readiness = client.get(f"/v1/cases/{case_id}/readiness", headers=analyst.headers)
    assert readiness.status_code == 200
    assert readiness.json()["ready_for_internal_review"] is True
    ready = client.post(
        f"/v1/cases/{case_id}/transitions",
        headers={**analyst.headers, "Idempotency-Key": "revision-ready"},
        json={
            "target": "ready_for_internal_review",
            "expected_version": current["version"],
            "reason": "Required evidence and calculation were reviewed",
        },
    )
    assert ready.status_code == 200, ready.text
    approved = client.post(
        f"/v1/cases/{case_id}/transitions",
        headers={**approver.headers, "Idempotency-Key": "revision-approved"},
        json={
            "target": "internal_packet_approved",
            "expected_version": ready.json()["version"],
            "reason": "Human approver completed internal review",
        },
    )
    assert approved.status_code == 200, approved.text
    old_revision_id = approved.json()["revisions"][0]["id"]
    artifact = client.post(
        f"/v1/cases/{case_id}/exports",
        headers={**analyst.headers, "Idempotency-Key": "revision-approved-export"},
        json={
            "kind": "machine_readable_json",
            "calculation_id": calculation.json()["id"],
        },
    )
    assert artifact.status_code == 201, artifact.text
    assert artifact.json()["approval_status"] == "approved"

    upload_count_before_locked_attempt = len(list(get_settings().upload_dir.glob("*.pdf")))
    revision_count_before_locked_attempt = db.query(CaseRevision).count()
    document_count_before_locked_attempt = db.query(ContractDocument).count()
    audit_count_before_locked_attempt = db.query(AuditEvent).count()
    locked_upload = _upload(
        client,
        analyst,
        case_id,
        "fee_and_date_variants.pdf",
        key="revision-upload-two",
    )
    assert locked_upload.status_code == 409
    assert "revision is locked" in locked_upload.text.lower()
    db.expire_all()
    unchanged_case = client.get(f"/v1/cases/{case_id}", headers=analyst.headers).json()
    assert unchanged_case["current_revision_number"] == 1
    assert unchanged_case["status"] == "internal_packet_approved"
    assert db.query(CaseRevision).count() == revision_count_before_locked_attempt
    assert db.query(ContractDocument).count() == document_count_before_locked_attempt
    assert db.query(AuditEvent).count() == audit_count_before_locked_attempt
    assert len(list(get_settings().upload_dir.glob("*.pdf"))) == upload_count_before_locked_attempt

    reopened = client.post(
        f"/v1/cases/{case_id}/transitions",
        headers={**analyst.headers, "Idempotency-Key": "revision-explicit-reopen"},
        json={
            "target": "evidence_review",
            "expected_version": approved.json()["version"],
            "reason": "New evidence requires an explicit, audited review revision",
        },
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["current_revision_number"] == 2
    assert reopened.json()["status"] == "evidence_review"
    assert (
        db.query(AuditEvent)
        .filter_by(action="case.revision_created", object_id=reopened.json()["revisions"][1]["id"])
        .count()
        == 1
    )

    new_upload = _upload(
        client,
        analyst,
        case_id,
        "fee_and_date_variants.pdf",
        key="revision-upload-two",
    )
    assert new_upload.status_code == 201, new_upload.text
    changed_case = client.get(f"/v1/cases/{case_id}", headers=analyst.headers).json()
    assert changed_case["current_revision_number"] == 2
    assert changed_case["status"] == "evidence_review"
    assert len(changed_case["revisions"]) == 2
    new_revision_id = next(item["id"] for item in changed_case["revisions"] if item["number"] == 2)
    assert new_upload.json()["case_revision_id"] == new_revision_id
    cloned_assertion = (
        db.query(ContractAssertion)
        .filter_by(case_revision_id=new_revision_id, semantic_key="contract_end_date")
        .one()
    )
    assert cloned_assertion.review_state == AssertionReviewState.PROPOSED
    assert cloned_assertion.source == AssertionSource.DETERMINISTIC
    assert db.query(CalculationRun).filter_by(case_revision_id=old_revision_id).count() == 1
    assert db.query(CalculationRun).filter_by(case_revision_id=new_revision_id).count() == 0
    cloned_fee = db.query(FeeObligation).filter_by(case_revision_id=new_revision_id).one()
    assert cloned_fee.reviewed is False
    assert cloned_fee.assertion_ids
    linked_assertion = db.get(ContractAssertion, cloned_fee.assertion_ids[0])
    assert linked_assertion is not None
    assert linked_assertion.case_revision_id == new_revision_id
    old_document = db.get(ContractDocument, uploaded.json()["id"])
    assert old_document is not None
    assert old_document.superseded is False
    old_artifact = db.get(ExportArtifact, artifact.json()["id"])
    assert old_artifact is not None
    assert old_artifact.superseded is True
    assert db.query(CaseRevision).filter_by(case_id=case_id).count() == 2
    persisted_case = db.get(RescueCase, case_id)
    assert persisted_case is not None
    assert persisted_case.current_revision_number == 2
