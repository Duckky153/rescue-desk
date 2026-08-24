from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pymupdf as fitz
import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import rescue_desk.services.exports as export_service
from rescue_desk.config import get_settings
from rescue_desk.domain.audit import AuditPayload, calculate_event_hash
from rescue_desk.domain.hashing import hash_payload
from rescue_desk.exports import DISCLAIMER
from rescue_desk.models import (
    AuditEvent,
    CaseRevision,
    CaseStatus,
    ContractAssertion,
    ContractDocument,
    DocumentPage,
    EvidenceSpan,
    ExportArtifact,
    FindingSeverity,
    FindingStatus,
    ProcessingStatus,
    ReadinessFinding,
    RescueCase,
    ReviewDecision,
    ReviewDecisionType,
    Role,
    SafetyStatus,
)
from rescue_desk.services.audit import append_audit_event
from rescue_desk.services.cases import revision_snapshot
from tests.integration.conftest import SeededIdentity, seed_identity


@dataclass(frozen=True)
class ExportCase:
    case_id: str
    revision_id: str
    calculation_id: str
    calculation_result_hash: str
    contract_quote: str
    fee_quote: str


def _digest(value: str | bytes) -> str:
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture(autouse=True)
def isolated_export_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    upload_dir = tmp_path / "runtime" / "uploads"
    monkeypatch.setattr(
        export_service,
        "get_settings",
        lambda: SimpleNamespace(upload_dir=upload_dir),
    )


def _create_case(client: TestClient, identity: SeededIdentity, key: str) -> str:
    response = client.post(
        "/v1/cases",
        headers={**identity.headers, "Idempotency-Key": key},
        json={
            "display_name": "Legacy ERP exit review",
            "applicant_company": "Northstar Manufacturing (synthetic)",
            "erp_provider": "LegacySuite (synthetic)",
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _seed_exportable_case(
    client: TestClient,
    db: Session,
    identity: SeededIdentity,
    *,
    key_suffix: str,
) -> ExportCase:
    case_id = _create_case(client, identity, f"case-{key_suffix}")
    revision = db.query(CaseRevision).filter_by(case_id=case_id, number=1).one()
    contract_quote = "Contract ends on December 31, 2027."
    fee_quote = "Annual subscription fee is USD 12,000."
    page_text = f"{contract_quote}\n{fee_quote}"
    stored_filename = f"synthetic-{key_suffix}.pdf"
    stored_path = get_settings().upload_dir / stored_filename
    stored_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stored_path.write_bytes(page_text.encode("utf-8"))
    stored_path.chmod(0o600)
    document = ContractDocument(
        organization_id=identity.organization_id,
        case_revision_id=revision.id,
        original_filename="synthetic-legacy-contract.pdf",
        stored_filename=stored_filename,
        sha256=_digest(page_text),
        media_type="application/pdf",
        source_type="synthetic",
        size_bytes=len(page_text.encode("utf-8")),
        page_count=1,
        safety_status=SafetyStatus.PASSED,
        processing_status=ProcessingStatus.PROCESSED,
        created_by=identity.user_id,
    )
    db.add(document)
    db.flush()
    page = DocumentPage(
        document_id=document.id,
        page_number=1,
        text=page_text,
        text_sha256=_digest(page_text),
        extraction_confidence=1,
    )
    db.add(page)
    db.flush()
    contract_span = EvidenceSpan(
        page_id=page.id,
        quote=contract_quote,
        char_start=0,
        char_end=len(contract_quote),
        quote_sha256=_digest(contract_quote),
    )
    fee_start = page_text.index(fee_quote)
    fee_span = EvidenceSpan(
        page_id=page.id,
        quote=fee_quote,
        char_start=fee_start,
        char_end=fee_start + len(fee_quote),
        quote_sha256=_digest(fee_quote),
    )
    db.add_all([contract_span, fee_span])
    db.flush()
    contract_assertion = ContractAssertion(
        case_revision_id=revision.id,
        semantic_key="contract_end_date",
        raw_value="2027-12-31",
        normalized_value={"type": "date", "value": "2027-12-31"},
        display_value="2027-12-31",
        source="human",
        confidence=1,
        review_state="accepted",
        evidence=[contract_span],
    )
    fee_assertion = ContractAssertion(
        case_revision_id=revision.id,
        semantic_key="fee.subscription",
        raw_value="USD 12,000",
        normalized_value={
            "type": "money",
            "amount_minor": 1_200_000,
            "currency": "USD",
            "cadence": "annual",
        },
        display_value="USD 12,000.00",
        source="human",
        confidence=1,
        review_state="accepted",
        evidence=[fee_span],
    )
    db.add_all([contract_assertion, fee_assertion])
    db.flush()
    db.add_all(
        [
            ReviewDecision(
                assertion_id=assertion.id,
                decision=ReviewDecisionType.ACCEPT,
                reviewer_id=identity.user_id,
                reason="Fixture reviewer confirmed the exact quoted source.",
                assumption=False,
            )
            for assertion in (contract_assertion, fee_assertion)
        ]
    )
    db.flush()
    fee_response = client.post(
        f"/v1/cases/{case_id}/fees",
        headers={**identity.headers, "Idempotency-Key": f"fee-{key_suffix}"},
        json={
            "category": "subscription",
            "amount_minor": 12_000_00,
            "currency": "USD",
            "service_start": "2026-01-01",
            "service_end": "2027-01-01",
            "payment_status": "unpaid",
            "billing_cadence": "annual",
            "proration_rule": "contract_daily",
            "assertion_ids": [fee_assertion.id],
            "reviewed": True,
        },
    )
    assert fee_response.status_code == 201, fee_response.text
    calculation = client.post(
        f"/v1/cases/{case_id}/calculations",
        headers={**identity.headers, "Idempotency-Key": f"calculation-{key_suffix}"},
        json={"as_of_date": date(2026, 7, 2).isoformat()},
    )
    assert calculation.status_code == 201, calculation.text
    payload = cast(dict[str, Any], calculation.json())
    return ExportCase(
        case_id=case_id,
        revision_id=revision.id,
        calculation_id=str(payload["id"]),
        calculation_result_hash=str(payload["result_hash"]),
        contract_quote=contract_quote,
        fee_quote=fee_quote,
    )


def _post_export(
    client: TestClient,
    identity: SeededIdentity,
    case: ExportCase,
    *,
    kind: str,
    key: str,
    calculation_id: str | None = None,
) -> Any:
    return client.post(
        f"/v1/cases/{case.case_id}/exports",
        headers={**identity.headers, "Idempotency-Key": key},
        json={
            "kind": kind,
            "calculation_id": calculation_id or case.calculation_id,
        },
    )


def _pdf_text(value: bytes) -> str:
    with fitz.open(stream=value, filetype="pdf") as document:
        return "\n".join(page.get_text() for page in document)


def test_export_endpoints_are_idempotent_downloadable_and_explicitly_unapproved(
    client: TestClient,
    db: Session,
) -> None:
    analyst = seed_identity(db, email="export-analyst@demo.local", role=Role.ANALYST)
    case = _seed_exportable_case(client, db, analyst, key_suffix="endpoint")

    first = _post_export(
        client,
        analyst,
        case,
        kind="internal_review_pdf",
        key="export-internal-endpoint",
    )
    assert first.status_code == 201, first.text
    first_payload = first.json()
    assert first_payload["approval_status"] == "not_approved"
    assert first_payload["disclaimer"] == DISCLAIMER
    assert first_payload["content_type"] == "application/pdf"
    assert first_payload["size_bytes"] > 0
    assert "stored_filename" not in first_payload

    replay = _post_export(
        client,
        analyst,
        case,
        kind="internal_review_pdf",
        key="export-internal-endpoint",
    )
    assert replay.status_code == 201, replay.text
    assert replay.json() == first_payload
    assert db.query(ExportArtifact).count() == 1
    assert db.query(AuditEvent).filter_by(action="export.created").count() == 1

    key_conflict = _post_export(
        client,
        analyst,
        case,
        kind="evidence_csv",
        key="export-internal-endpoint",
    )
    assert key_conflict.status_code == 409

    customer = _post_export(
        client,
        analyst,
        case,
        kind="customer_explanation_pdf",
        key="export-customer-endpoint",
    )
    assert customer.status_code == 201, customer.text
    customer_id = customer.json()["id"]
    customer_download = client.get(
        f"/v1/cases/{case.case_id}/exports/{customer_id}/download",
        headers=analyst.headers,
    )
    assert customer_download.status_code == 200, customer_download.text
    customer_text = _pdf_text(customer_download.content).replace("\n", " ")
    assert "NOT APPROVED - HUMAN APPROVAL REQUIRED" in customer_text
    assert "APPROVER-RECORDED SNAPSHOT" not in customer_text
    assert "does not determine" in customer_text
    assert DISCLAIMER in customer_text

    listing = client.get(f"/v1/cases/{case.case_id}/exports", headers=analyst.headers)
    assert listing.status_code == 200, listing.text
    assert {item["id"] for item in listing.json()} == {first_payload["id"], customer_id}
    metadata = client.get(
        f"/v1/cases/{case.case_id}/exports/{first_payload['id']}",
        headers=analyst.headers,
    )
    assert metadata.status_code == 200
    assert metadata.json()["sha256"] == first_payload["sha256"]

    unauthenticated = client.get(f"/v1/cases/{case.case_id}/exports/{first_payload['id']}/download")
    assert unauthenticated.status_code == 401
    download = client.get(
        f"/v1/cases/{case.case_id}/exports/{first_payload['id']}/download",
        headers=analyst.headers,
    )
    assert download.status_code == 200, download.text
    assert _digest(download.content) == first_payload["sha256"]
    assert download.headers["x-artifact-sha256"] == first_payload["sha256"]
    assert download.headers["x-packet-snapshot-sha256"] == first_payload["packet_snapshot_sha256"]
    assert db.query(AuditEvent).filter_by(action="export.downloaded").count() == 2


def test_customer_export_labels_unreviewed_assertion_as_pending_proposal(
    client: TestClient,
    db: Session,
) -> None:
    analyst = seed_identity(db, email="pending-export@demo.local", role=Role.ANALYST)
    case = _seed_exportable_case(client, db, analyst, key_suffix="pending-provenance")
    source = (
        db.query(ContractAssertion)
        .filter_by(case_revision_id=case.revision_id, semantic_key="contract_end_date")
        .one()
    )
    pending = ContractAssertion(
        case_revision_id=case.revision_id,
        semantic_key="renewal.notice_days",
        raw_value="30 days",
        normalized_value={"type": "duration_days", "days": 30},
        display_value="30 days",
        source="human",
        confidence=1,
        review_state="proposed",
        evidence=[source.evidence[0]],
    )
    db.add(pending)
    db.commit()

    generated = _post_export(
        client,
        analyst,
        case,
        kind="customer_explanation_pdf",
        key="pending-customer-export",
    )
    assert generated.status_code == 201, generated.text
    downloaded = client.get(generated.json()["download_url"], headers=analyst.headers)
    assert downloaded.status_code == 200, downloaded.text
    normalized = _pdf_text(downloaded.content).replace("\n", " ")
    assert "Review status: PROPOSED" in normalized
    assert "source-backed proposal; review pending" in normalized


def test_machine_export_requires_latest_calculation_and_preserves_formula_and_page_citations(
    client: TestClient,
    db: Session,
) -> None:
    analyst = seed_identity(db, email="fidelity-analyst@demo.local", role=Role.ANALYST)
    case = _seed_exportable_case(client, db, analyst, key_suffix="fidelity")
    later = client.post(
        f"/v1/cases/{case.case_id}/calculations",
        headers={**analyst.headers, "Idempotency-Key": "calculation-fidelity-later"},
        json={"as_of_date": "2026-10-01"},
    )
    assert later.status_code == 201, later.text
    assert later.json()["id"] != case.calculation_id

    stale = _post_export(
        client,
        analyst,
        case,
        kind="machine_readable_json",
        key="export-machine-fidelity-stale",
        calculation_id=case.calculation_id,
    )
    assert stale.status_code == 409, stale.text
    assert "not the latest" in stale.text

    generated = _post_export(
        client,
        analyst,
        case,
        kind="machine_readable_json",
        key="export-machine-fidelity",
        calculation_id=str(later.json()["id"]),
    )
    assert generated.status_code == 201, generated.text
    assert generated.json()["calculation_id"] == later.json()["id"]
    downloaded = client.get(
        generated.json()["download_url"],
        headers=analyst.headers,
    )
    assert downloaded.status_code == 200, downloaded.text
    payload = json.loads(downloaded.content)
    assert payload["disclaimer"] == DISCLAIMER
    assert payload["integrity"]["calculation_result_sha256"] == later.json()["result_hash"]
    calculation = payload["snapshot"]["calculation"]
    assert calculation["result_hash"] == later.json()["result_hash"]
    assert len(calculation["lines"]) == 1
    line = calculation["lines"][0]
    assert line["formula_id"] == "contract-daily-half-open-v1"
    assert line["formula"] == (
        "round_half_up(original_minor * remaining_days / total_service_days)"
    )
    assert line["original_amount_minor"] == 1_200_000
    assert line["result_amount_minor"] == later.json()["line_items"][0]["remaining_amount_minor"]
    fee_citation = next(
        item for item in payload["snapshot"]["citations"] if item["quote"] == case.fee_quote
    )
    assert fee_citation["page_number"] == 1
    assert fee_citation["quote_sha256"] == _digest(case.fee_quote)
    assert line["citation_ids"] == [fee_citation["citation_id"]]
    assert payload["snapshot_sha256"] == generated.json()["packet_snapshot_sha256"]

    approver = seed_identity(db, email="fidelity-approver@demo.local", role=Role.APPROVER)
    evidence_review = client.post(
        f"/v1/cases/{case.case_id}/transitions",
        headers={**analyst.headers, "Idempotency-Key": "fidelity-to-evidence-review"},
        json={
            "target": "evidence_review",
            "expected_version": 1,
            "reason": "Processed evidence is ready",
        },
    )
    assert evidence_review.status_code == 200, evidence_review.text
    ready = client.post(
        f"/v1/cases/{case.case_id}/transitions",
        headers={**analyst.headers, "Idempotency-Key": "fidelity-to-ready"},
        json={
            "target": "ready_for_internal_review",
            "expected_version": 2,
            "reason": "Evidence and latest calculation reviewed",
        },
    )
    assert ready.status_code == 200, ready.text
    approved = client.post(
        f"/v1/cases/{case.case_id}/transitions",
        headers={**approver.headers, "Idempotency-Key": "fidelity-to-approved"},
        json={
            "target": "internal_packet_approved",
            "expected_version": 3,
            "reason": "Human approval of the latest calculation snapshot",
        },
    )
    assert approved.status_code == 200, approved.text

    unapproved_calculation = _post_export(
        client,
        approver,
        case,
        kind="customer_explanation_pdf",
        key="export-customer-with-old-calculation",
        calculation_id=case.calculation_id,
    )
    assert unapproved_calculation.status_code == 409
    assert "not the latest" in unapproved_calculation.text
    approved_customer = _post_export(
        client,
        approver,
        case,
        kind="customer_explanation_pdf",
        key="export-customer-with-approved-calculation",
        calculation_id=str(later.json()["id"]),
    )
    assert approved_customer.status_code == 201, approved_customer.text
    assert approved_customer.json()["approval_status"] == "approved"
    approved_text = _pdf_text(
        client.get(approved_customer.json()["download_url"], headers=approver.headers).content
    ).replace("\n", " ")
    assert "APPROVER-RECORDED SNAPSHOT" in approved_text
    assert "Approver: Fidelity-Approver" in approved_text


def test_open_blocker_is_exported_for_review_but_cannot_be_marked_approved(
    client: TestClient,
    db: Session,
) -> None:
    analyst = seed_identity(db, email="blocker-analyst@demo.local", role=Role.ANALYST)
    approver = seed_identity(db, email="blocker-approver@demo.local", role=Role.APPROVER)
    case_data = _seed_exportable_case(client, db, analyst, key_suffix="blocker")
    finding = ReadinessFinding(
        case_revision_id=case_data.revision_id,
        code="termination.ambiguous",
        title="Termination language needs human review",
        detail="The cited clause does not establish enforceability.",
        severity=FindingSeverity.BLOCKING,
        status=FindingStatus.OPEN,
    )
    db.add(finding)
    db.commit()

    review_export = _post_export(
        client,
        analyst,
        case_data,
        kind="machine_readable_json",
        key="export-with-open-blocker",
    )
    assert review_export.status_code == 201, review_export.text
    review_payload = client.get(
        review_export.json()["download_url"], headers=analyst.headers
    ).json()
    assert review_payload["snapshot"]["preparation"]["approval_status"] == "not_approved"
    assert review_payload["snapshot"]["blockers"][0]["status"] == "open"

    case = db.get(RescueCase, case_data.case_id)
    revision = db.get(CaseRevision, case_data.revision_id)
    assert case is not None and revision is not None
    case.status = CaseStatus.INTERNAL_PACKET_APPROVED
    revision.snapshot_hash = hash_payload(revision_snapshot(db, revision.id))
    append_audit_event(
        db,
        organization_id=approver.organization_id,
        actor_id=approver.user_id,
        action="case.transitioned",
        object_type="rescue_case",
        object_id=case.id,
        correlation_id="human-approval-with-blocker-test",
        before={
            "status": CaseStatus.READY_FOR_INTERNAL_REVIEW.value,
            "version": case.version - 1,
        },
        after={
            "status": CaseStatus.INTERNAL_PACKET_APPROVED.value,
            "version": case.version,
            "revision_id": revision.id,
            "revision_snapshot_sha256": revision.snapshot_hash,
        },
    )
    db.commit()

    inconsistent = _post_export(
        client,
        approver,
        case_data,
        kind="customer_explanation_pdf",
        key="approved-export-with-open-blocker",
    )
    assert inconsistent.status_code == 409
    assert "approved packets cannot contain open blockers" in inconsistent.text


def test_exports_are_tenant_isolated_and_download_detects_tampering(
    client: TestClient,
    db: Session,
    tmp_path: Path,
) -> None:
    first = seed_identity(db, email="first-export@demo.local", role=Role.ANALYST)
    second = seed_identity(
        db,
        email="second-export@demo.local",
        role=Role.ANALYST,
        organization_name="Second Export Demo",
    )
    case = _seed_exportable_case(client, db, first, key_suffix="tenant")
    generated = _post_export(
        client,
        first,
        case,
        kind="evidence_csv",
        key="export-tenant-csv",
    )
    assert generated.status_code == 201, generated.text
    artifact_id = generated.json()["id"]

    for path in (
        f"/v1/cases/{case.case_id}/exports",
        f"/v1/cases/{case.case_id}/exports/{artifact_id}",
        f"/v1/cases/{case.case_id}/exports/{artifact_id}/download",
    ):
        hidden = client.get(path, headers=second.headers)
        assert hidden.status_code == 404, (path, hidden.text)
    hidden_post = _post_export(
        client,
        second,
        case,
        kind="evidence_csv",
        key="second-org-export-attempt",
    )
    assert hidden_post.status_code == 404

    artifact = db.get(ExportArtifact, artifact_id)
    assert artifact is not None
    stored = tmp_path / "runtime" / "exports" / artifact.stored_filename
    assert stored.is_file()
    stored.write_bytes(b"tampered")
    tampered = client.get(
        f"/v1/cases/{case.case_id}/exports/{artifact_id}/download",
        headers=first.headers,
    )
    assert tampered.status_code == 409
    assert "SHA-256 integrity check" in tampered.text

    stored.unlink()
    neighboring_artifact = stored.parent / "neighboring-artifact.csv"
    neighboring_artifact.write_bytes(b"not this artifact")
    stored.symlink_to(neighboring_artifact)
    symlinked = client.get(
        f"/v1/cases/{case.case_id}/exports/{artifact_id}/download",
        headers=first.headers,
    )
    assert symlinked.status_code == 409
    assert "symbolic link" in symlinked.text


def test_export_rejects_synchronized_bytes_metadata_and_creation_event_tampering(
    client: TestClient, db: Session, tmp_path: Path
) -> None:
    analyst = seed_identity(db, email="artifact-chain@demo.local", role=Role.ANALYST)
    case = _seed_exportable_case(client, db, analyst, key_suffix="artifact-chain")
    generated = _post_export(
        client,
        analyst,
        case,
        kind="evidence_csv",
        key="artifact-chain-export",
    )
    assert generated.status_code == 201, generated.text
    artifact_id = str(generated.json()["id"])
    first_download = client.get(generated.json()["download_url"], headers=analyst.headers)
    assert first_download.status_code == 200, first_download.text

    artifact = db.get(ExportArtifact, artifact_id)
    assert artifact is not None
    stored = tmp_path / "runtime" / "exports" / artifact.stored_filename
    changed_bytes = stored.read_bytes() + b"\n"
    stored.write_bytes(changed_bytes)
    artifact.sha256 = hashlib.sha256(changed_bytes).hexdigest()
    created = (
        db.query(AuditEvent)
        .filter_by(
            object_type="export_artifact",
            object_id=artifact_id,
            action="export.created",
        )
        .one()
    )
    changed_after = dict(created.after or {})
    changed_after["artifact_sha256"] = artifact.sha256
    changed_after["size_bytes"] = len(changed_bytes)
    created.after = changed_after
    # Even recomputing this event's hash cannot rewrite its already-appended
    # successor. The organization chain must remain the immutable trust anchor.
    created.event_hash = calculate_event_hash(
        AuditPayload(
            organization_id=created.organization_id,
            actor_id=created.actor_id,
            action=created.action,
            object_type=created.object_type,
            object_id=created.object_id,
            correlation_id=created.correlation_id,
            before=created.before,
            after=created.after,
            created_at=created.created_at,
            previous_hash=created.previous_hash,
        )
    )
    db.commit()

    rejected = client.get(generated.json()["download_url"], headers=analyst.headers)
    assert rejected.status_code == 409, rejected.text
    assert "audit chain failed its integrity check" in rejected.text


def test_export_generation_rejects_a_calculation_from_a_previous_revision(
    client: TestClient,
    db: Session,
) -> None:
    analyst = seed_identity(db, email="revision-analyst@demo.local", role=Role.ANALYST)
    case_data = _seed_exportable_case(client, db, analyst, key_suffix="revision")
    case = db.get(RescueCase, case_data.case_id)
    assert case is not None
    second_revision = CaseRevision(
        case_id=case.id,
        number=2,
        reason="New evidence requires a fresh calculation",
        previous_revision_id=case_data.revision_id,
        created_by=analyst.user_id,
    )
    db.add(second_revision)
    case.current_revision_number = 2
    db.commit()
    db.expire(case, ["revisions"])

    selected_old_calculation = _post_export(
        client,
        analyst,
        case_data,
        kind="machine_readable_json",
        key="export-old-calculation",
        calculation_id=case_data.calculation_id,
    )
    assert selected_old_calculation.status_code == 422
    assert "current case revision" in selected_old_calculation.text
    latest_old_calculation = client.post(
        f"/v1/cases/{case.id}/exports",
        headers={**analyst.headers, "Idempotency-Key": "export-no-current-calculation"},
        json={"kind": "machine_readable_json", "calculation_id": None},
    )
    assert latest_old_calculation.status_code == 409
    assert "current-revision calculation" in latest_old_calculation.text
