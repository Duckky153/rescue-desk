from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from typing import Any, cast

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

import rescue_desk.services.documents as documents_service
from rescue_desk.config import get_settings
from rescue_desk.database import Base, get_db
from rescue_desk.domain.audit import AuditPayload, verify_audit_chain
from rescue_desk.domain.hashing import hash_payload
from rescue_desk.domain.money import MAX_MINOR_UNITS
from rescue_desk.main import app
from rescue_desk.models import (
    AssertionReviewState,
    AuditEvent,
    CalculationRun,
    CaseRevision,
    CaseStatus,
    ContractAssertion,
    ContractDocument,
    DocumentPage,
    EvidenceSpan,
    ExportArtifact,
    ExportKind,
    ExtractionRun,
    FeeObligation,
    FindingSeverity,
    FindingStatus,
    IdempotencyRecord,
    ReadinessFinding,
    RescueCase,
    ReviewDecision,
    Role,
)
from rescue_desk.services.audit import append_audit_event
from rescue_desk.services.cases import revision_snapshot
from rescue_desk.services.exports import _approval
from tests.integration.conftest import SeededIdentity, seed_identity
from tests.integration.test_document_api import (
    _create_case as create_document_case,
)
from tests.integration.test_document_api import (
    _process as process_document,
)
from tests.integration.test_document_api import (
    _upload as upload_document,
)
from tests.integration.test_export_api import (
    ExportCase,
    _create_case,
    _post_export,
    _seed_exportable_case,
)


def _transition(
    client: TestClient,
    identity: SeededIdentity,
    case_id: str,
    *,
    target: str,
    expected_version: int,
    key: str,
) -> Response:
    return cast(
        Response,
        client.post(
            f"/v1/cases/{case_id}/transitions",
            headers={**identity.headers, "Idempotency-Key": key},
            json={
                "target": target,
                "expected_version": expected_version,
                "reason": f"Adversarial regression transition to {target}",
            },
        ),
    )


def _approve_case(
    client: TestClient,
    *,
    analyst: SeededIdentity,
    approver: SeededIdentity,
    case: ExportCase,
    key_prefix: str,
) -> dict[str, Any]:
    evidence = _transition(
        client,
        analyst,
        case.case_id,
        target="evidence_review",
        expected_version=1,
        key=f"{key_prefix}-evidence",
    )
    assert evidence.status_code == 200, evidence.text
    ready = _transition(
        client,
        analyst,
        case.case_id,
        target="ready_for_internal_review",
        expected_version=2,
        key=f"{key_prefix}-ready",
    )
    assert ready.status_code == 200, ready.text
    approved = _transition(
        client,
        approver,
        case.case_id,
        target="internal_packet_approved",
        expected_version=3,
        key=f"{key_prefix}-approved",
    )
    assert approved.status_code == 200, approved.text
    return cast(dict[str, Any], approved.json())


def _unreviewed_fee_payload() -> dict[str, Any]:
    return {
        "category": "other",
        "amount_minor": 0,
        "currency": "USD",
        "payment_status": "paid",
        "reviewed": False,
    }


def _reviewed_subscription_fee_payload(assertion_id: str) -> dict[str, Any]:
    return {
        "category": "subscription",
        "amount_minor": 1_200_000,
        "currency": "USD",
        "service_start": "2026-01-01",
        "service_end": "2027-01-01",
        "payment_status": "unpaid",
        "billing_cadence": "annual",
        "proration_rule": "contract_daily",
        "assertion_ids": [assertion_id],
        "reviewed": True,
    }


def test_fee_supersede_preserves_history_retires_export_and_releases_evidence(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="fee-recovery@example.com", role=Role.ANALYST)
    case = _seed_exportable_case(client, db, analyst, key_suffix="fee-recovery")
    fee_assertion = db.scalar(
        select(ContractAssertion).where(
            ContractAssertion.case_revision_id == case.revision_id,
            ContractAssertion.semantic_key == "fee.subscription",
            ContractAssertion.is_current.is_(True),
        )
    )
    original_fee = db.scalar(
        select(FeeObligation).where(
            FeeObligation.case_revision_id == case.revision_id,
            FeeObligation.superseded.is_(False),
        )
    )
    assert fee_assertion is not None and original_fee is not None
    fee_payload = _reviewed_subscription_fee_payload(fee_assertion.id)

    artifact_response = _post_export(
        client,
        analyst,
        case,
        kind="machine_readable_json",
        key="fee-recovery-export",
    )
    assert artifact_response.status_code == 201, artifact_response.text
    artifact_id = str(artifact_response.json()["id"])

    repeated_id_key = "fee-recovery-repeated-id"
    repeated_id = client.post(
        f"/v1/cases/{case.case_id}/fees",
        headers={**analyst.headers, "Idempotency-Key": repeated_id_key},
        json={**fee_payload, "assertion_ids": [fee_assertion.id, fee_assertion.id]},
    )
    assert repeated_id.status_code == 422, repeated_id.text
    assert "unique" in repeated_id.text.lower()
    assert db.query(FeeObligation).filter_by(case_revision_id=case.revision_id).count() == 1
    assert db.query(AuditEvent).filter_by(action="fee.created").count() == 1
    assert db.query(IdempotencyRecord).filter_by(key=repeated_id_key).count() == 0

    duplicate = client.post(
        f"/v1/cases/{case.case_id}/fees",
        headers={**analyst.headers, "Idempotency-Key": "fee-recovery-duplicate"},
        json=fee_payload,
    )
    assert duplicate.status_code == 409, duplicate.text
    assert db.query(FeeObligation).filter_by(case_revision_id=case.revision_id).count() == 1

    stale = client.post(
        f"/v1/cases/{case.case_id}/fees/{original_fee.id}/supersede",
        headers={**analyst.headers, "Idempotency-Key": "fee-recovery-stale"},
        json={"expected_case_version": 99, "reason": "Accidental duplicate entry"},
    )
    assert stale.status_code == 409, stale.text

    supersede_headers = {
        **analyst.headers,
        "Idempotency-Key": "fee-recovery-supersede",
    }
    supersede_payload = {
        "expected_case_version": 1,
        "reason": "Accidental duplicate entry",
    }
    superseded = client.post(
        f"/v1/cases/{case.case_id}/fees/{original_fee.id}/supersede",
        headers=supersede_headers,
        json=supersede_payload,
    )
    assert superseded.status_code == 200, superseded.text
    superseded_body = superseded.json()
    assert superseded_body["superseded"] is True
    assert superseded_body["superseded_at"] is not None
    assert superseded_body["superseded_by"] == analyst.user_id
    assert superseded_body["supersede_reason"] == "Accidental duplicate entry"
    assert superseded_body["primary_money_assertion_id"] == fee_assertion.id
    assert client.get(f"/v1/cases/{case.case_id}", headers=analyst.headers).json()["version"] == 2

    replay = client.post(
        f"/v1/cases/{case.case_id}/fees/{original_fee.id}/supersede",
        headers=supersede_headers,
        json=supersede_payload,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == superseded_body
    assert (
        db.query(AuditEvent).filter_by(action="fee.superseded", object_id=original_fee.id).count()
        == 1
    )
    db.expire_all()
    artifact = db.get(ExportArtifact, artifact_id)
    assert artifact is not None and artifact.superseded is True
    retired_download = client.get(artifact_response.json()["download_url"], headers=analyst.headers)
    assert retired_download.status_code == 409, retired_download.text
    readiness = client.get(f"/v1/cases/{case.case_id}/readiness", headers=analyst.headers)
    assert readiness.status_code == 200, readiness.text
    assert readiness.json()["reproducible_calculation_exists"] is False

    replacement = client.post(
        f"/v1/cases/{case.case_id}/fees",
        headers={**analyst.headers, "Idempotency-Key": "fee-recovery-replacement"},
        json=fee_payload,
    )
    assert replacement.status_code == 201, replacement.text
    assert replacement.json()["id"] != original_fee.id
    assert replacement.json()["superseded"] is False
    listed = client.get(f"/v1/cases/{case.case_id}/fees", headers=analyst.headers)
    assert listed.status_code == 200, listed.text
    assert len(listed.json()) == 2
    assert sum(not item["superseded"] for item in listed.json()) == 1


def test_active_fee_aggregate_limit_is_released_by_supersession(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="fee-aggregate@example.com", role=Role.ANALYST)
    case_id = _create_case(client, analyst, "fee-aggregate-case")
    maximum_payload = {
        "category": "other",
        "amount_minor": MAX_MINOR_UNITS,
        "currency": "USD",
        "payment_status": "paid",
        "reviewed": False,
    }
    maximum = client.post(
        f"/v1/cases/{case_id}/fees",
        headers={**analyst.headers, "Idempotency-Key": "fee-aggregate-maximum"},
        json=maximum_payload,
    )
    assert maximum.status_code == 201, maximum.text

    overflow_headers = {**analyst.headers, "Idempotency-Key": "fee-aggregate-overflow"}
    overflow_payload = {**maximum_payload, "amount_minor": 1}
    overflow = client.post(
        f"/v1/cases/{case_id}/fees",
        headers=overflow_headers,
        json=overflow_payload,
    )
    assert overflow.status_code == 422, overflow.text
    assert db.query(FeeObligation).count() == 1

    superseded = client.post(
        f"/v1/cases/{case_id}/fees/{maximum.json()['id']}/supersede",
        headers={**analyst.headers, "Idempotency-Key": "fee-aggregate-release"},
        json={"expected_case_version": 1, "reason": "Remove accidental maximum amount"},
    )
    assert superseded.status_code == 200, superseded.text

    recovered = client.post(
        f"/v1/cases/{case_id}/fees",
        headers=overflow_headers,
        json=overflow_payload,
    )
    assert recovered.status_code == 201, recovered.text
    fees = db.query(FeeObligation).all()
    assert len(fees) == 2
    assert sum(item.amount_minor for item in fees if not item.superseded) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "assertion_review",
        "fee_create",
        "fee_supersede",
        "calculation",
        "document_upload",
        "document_process",
        "document_failure",
        "document_supersede",
        "finding_disposition",
        "case_transition",
    ],
)
def test_every_packet_material_mutation_retires_the_current_artifact(
    client: TestClient,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    analyst = seed_identity(
        db,
        email=f"artifact-mutation-{mutation}-analyst@example.com",
        role=Role.ANALYST,
        organization_name=f"Artifact mutation {mutation}",
    )
    approver = seed_identity(
        db,
        email=f"artifact-mutation-{mutation}-approver@example.com",
        role=Role.APPROVER,
        organization_name=f"Artifact mutation {mutation}",
    )
    case = _seed_exportable_case(client, db, analyst, key_suffix=f"artifact-{mutation}")
    uploaded_for_mutation: Response | None = None
    finding: ReadinessFinding | None = None

    if mutation in {"document_process", "document_failure", "document_supersede"}:
        uploaded_for_mutation = upload_document(
            client,
            analyst,
            case.case_id,
            "clean_standard.pdf",
            key=f"artifact-{mutation}-setup-upload",
        )
        assert uploaded_for_mutation.status_code == 201, uploaded_for_mutation.text
    if mutation == "finding_disposition":
        finding = ReadinessFinding(
            case_revision_id=case.revision_id,
            code="artifact.mutation.finding",
            title="Packet mutation finding",
            detail="This finding will be explicitly dispositioned after packet creation.",
            severity=FindingSeverity.WARNING,
            status=FindingStatus.OPEN,
        )
        db.add(finding)
        db.commit()

    generated = _post_export(
        client,
        analyst,
        case,
        kind="machine_readable_json",
        key=f"artifact-{mutation}-current-export",
    )
    assert generated.status_code == 201, generated.text
    artifact_id = str(generated.json()["id"])

    if mutation == "assertion_review":
        assertion = db.scalar(
            select(ContractAssertion).where(
                ContractAssertion.case_revision_id == case.revision_id,
                ContractAssertion.semantic_key == "contract_end_date",
                ContractAssertion.is_current.is_(True),
            )
        )
        assert assertion is not None
        response = client.post(
            f"/v1/cases/assertions/{assertion.id}/reviews",
            headers={**analyst.headers, "Idempotency-Key": "artifact-assertion-review"},
            json={
                "decision": "correct",
                "expected_assertion_version": assertion.version,
                "reason": "Canonicalize the same reviewed date without changing its meaning.",
                "corrected_value": assertion.normalized_value,
                "assumption": False,
            },
        )
        assert response.status_code == 200, response.text
    elif mutation == "fee_create":
        response = client.post(
            f"/v1/cases/{case.case_id}/fees",
            headers={**analyst.headers, "Idempotency-Key": "artifact-fee-create"},
            json={
                "category": "other",
                "amount_minor": 1,
                "currency": "USD",
                "payment_status": "paid",
                "reviewed": False,
            },
        )
        assert response.status_code == 201, response.text
    elif mutation == "fee_supersede":
        fee = db.scalar(
            select(FeeObligation).where(
                FeeObligation.case_revision_id == case.revision_id,
                FeeObligation.superseded.is_(False),
            )
        )
        assert fee is not None
        response = client.post(
            f"/v1/cases/{case.case_id}/fees/{fee.id}/supersede",
            headers={**analyst.headers, "Idempotency-Key": "artifact-fee-supersede"},
            json={"expected_case_version": 1, "reason": "Remove accidental obligation"},
        )
        assert response.status_code == 200, response.text
    elif mutation == "calculation":
        response = client.post(
            f"/v1/cases/{case.case_id}/calculations",
            headers={**analyst.headers, "Idempotency-Key": "artifact-calculation"},
            json={"as_of_date": "2026-07-03"},
        )
        assert response.status_code == 201, response.text
    elif mutation == "document_upload":
        response = upload_document(
            client,
            analyst,
            case.case_id,
            "clean_standard.pdf",
            key="artifact-document-upload",
        )
        assert response.status_code == 201, response.text
    elif mutation == "document_process":
        assert uploaded_for_mutation is not None
        response = process_document(
            client,
            analyst,
            uploaded_for_mutation.json()["id"],
            key="artifact-document-process",
        )
        assert response.status_code == 200, response.text
    elif mutation == "document_failure":
        assert uploaded_for_mutation is not None

        def fail_extraction(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("Injected packet-material processing failure")

        monkeypatch.setattr(documents_service, "deterministic_extract", fail_extraction)
        response = process_document(
            client,
            analyst,
            uploaded_for_mutation.json()["id"],
            key="artifact-document-failure",
        )
        assert response.status_code == 422, response.text
    elif mutation == "document_supersede":
        assert uploaded_for_mutation is not None
        current_case = client.get(f"/v1/cases/{case.case_id}", headers=analyst.headers).json()
        response = client.post(
            f"/v1/documents/{uploaded_for_mutation.json()['id']}/supersede",
            headers={**analyst.headers, "Idempotency-Key": "artifact-document-supersede"},
            json={
                "expected_case_version": current_case["version"],
                "reason": "A processed replacement now governs this revision.",
            },
        )
        assert response.status_code == 200, response.text
    elif mutation == "finding_disposition":
        assert finding is not None
        response = client.post(
            f"/v1/cases/{case.case_id}/findings/{finding.id}/disposition",
            headers={**approver.headers, "Idempotency-Key": "artifact-finding-disposition"},
            json={
                "status": "accepted_risk",
                "expected_version": 1,
                "reason": "Approver explicitly accepted this bounded synthetic ambiguity.",
            },
        )
        assert response.status_code == 200, response.text
    else:
        response = _transition(
            client,
            analyst,
            case.case_id,
            target="evidence_review",
            expected_version=1,
            key="artifact-case-transition",
        )
        assert response.status_code == 200, response.text

    db.expire_all()
    artifact = db.get(ExportArtifact, artifact_id)
    assert artifact is not None and artifact.superseded is True
    assert (
        db.query(AuditEvent).filter_by(action="export.superseded", object_id=artifact_id).count()
        == 1
    )
    retired_download = client.get(generated.json()["download_url"], headers=analyst.headers)
    assert retired_download.status_code == 409, retired_download.text


def _audit_payload(event_record: AuditEvent) -> AuditPayload:
    return AuditPayload(
        organization_id=event_record.organization_id,
        actor_id=event_record.actor_id,
        action=event_record.action,
        object_type=event_record.object_type,
        object_id=event_record.object_id,
        correlation_id=event_record.correlation_id,
        before=event_record.before,
        after=event_record.after,
        created_at=event_record.created_at,
        previous_hash=event_record.previous_hash,
    )


def test_approved_export_formats_share_one_packet_and_revision_snapshot(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="format-analyst@example.com", role=Role.ANALYST)
    approver = seed_identity(db, email="format-approver@example.com", role=Role.APPROVER)
    case = _seed_exportable_case(client, db, analyst, key_suffix="format-snapshot")
    approved = _approve_case(
        client,
        analyst=analyst,
        approver=approver,
        case=case,
        key_prefix="format-snapshot",
    )
    assert approved["status"] == "internal_packet_approved"

    payloads: list[dict[str, Any]] = []
    for kind in (
        "internal_review_pdf",
        "customer_explanation_pdf",
        "evidence_csv",
        "machine_readable_json",
    ):
        response = _post_export(
            client,
            approver,
            case,
            kind=kind,
            key=f"format-export-{kind}",
        )
        assert response.status_code == 201, response.text
        payload = cast(dict[str, Any], response.json())
        assert payload["approval_status"] == "approved"
        payloads.append(payload)

    assert len({item["packet_snapshot_sha256"] for item in payloads}) == 1
    artifacts = list(
        db.scalars(
            select(ExportArtifact).where(ExportArtifact.case_revision_id == case.revision_id)
        )
    )
    revision = db.get(CaseRevision, case.revision_id)
    assert revision is not None and revision.snapshot_hash is not None
    assert len(artifacts) == 4
    assert {artifact.kind.value for artifact in artifacts} == {
        "internal_review_pdf",
        "customer_explanation_pdf",
        "evidence_csv",
        "machine_readable_json",
    }
    assert {artifact.packet_snapshot_sha256 for artifact in artifacts} == {
        payloads[0]["packet_snapshot_sha256"]
    }
    assert {artifact.revision_snapshot_sha256 for artifact in artifacts} == {revision.snapshot_hash}
    assert {artifact.approval_case_version for artifact in artifacts} == {4}


def test_approved_export_rejects_case_identity_drift_after_human_approval(
    client: TestClient,
    db: Session,
) -> None:
    analyst = seed_identity(db, email="identity-analyst@example.com", role=Role.ANALYST)
    approver = seed_identity(db, email="identity-approver@example.com", role=Role.APPROVER)
    case = _seed_exportable_case(client, db, analyst, key_suffix="identity-snapshot")
    approved = _approve_case(
        client,
        analyst=analyst,
        approver=approver,
        case=case,
        key_prefix="identity-snapshot",
    )
    assert approved["status"] == "internal_packet_approved"
    revision = db.get(CaseRevision, case.revision_id)
    stored_case = db.get(RescueCase, case.case_id)
    assert revision is not None and revision.snapshot_hash is not None
    assert stored_case is not None
    approved_hash = revision.snapshot_hash

    stored_case.display_name = "Unsealed replacement case name"
    stored_case.applicant_company = "Unsealed replacement company"
    stored_case.erp_provider = "Unsealed replacement ERP"
    db.commit()
    assert hash_payload(revision_snapshot(db, revision.id)) != approved_hash

    blocked = _post_export(
        client,
        approver,
        case,
        kind="machine_readable_json",
        key="identity-drift-export",
    )
    assert blocked.status_code == 409, blocked.text
    assert "approved snapshot no longer matches" in blocked.text
    assert db.query(ExportArtifact).filter_by(case_revision_id=revision.id).count() == 0


def test_prior_revision_approval_event_cannot_authorize_reopened_revision(
    client: TestClient,
    db: Session,
) -> None:
    analyst = seed_identity(db, email="event-analyst@example.com", role=Role.ANALYST)
    approver = seed_identity(db, email="event-approver@example.com", role=Role.APPROVER)
    case = _seed_exportable_case(client, db, analyst, key_suffix="approval-event-binding")
    approved = _approve_case(
        client,
        analyst=analyst,
        approver=approver,
        case=case,
        key_prefix="approval-event-binding",
    )
    reopened = _transition(
        client,
        approver,
        case.case_id,
        target="evidence_review",
        expected_version=int(approved["version"]),
        key="approval-event-binding-reopen",
    )
    assert reopened.status_code == 200, reopened.text

    db.expire_all()
    stored_case = db.get(RescueCase, case.case_id)
    calculation = db.get(CalculationRun, case.calculation_id)
    assert stored_case is not None and calculation is not None
    current_revision = db.scalar(
        select(CaseRevision).where(
            CaseRevision.case_id == stored_case.id,
            CaseRevision.number == stored_case.current_revision_number,
        )
    )
    assert current_revision is not None and current_revision.id != case.revision_id
    forged_snapshot = {"calculation_result_hash": calculation.result_hash}
    forged_hash = hash_payload(forged_snapshot)
    current_revision.snapshot_hash = forged_hash
    stored_case.status = CaseStatus.INTERNAL_PACKET_APPROVED
    db.commit()

    with pytest.raises(HTTPException) as captured:
        _approval(
            db,
            case=stored_case,
            revision=current_revision,
            calculation=calculation,
            current_snapshot=forged_snapshot,
            current_snapshot_hash=forged_hash,
        )
    assert captured.value.status_code == 409
    assert "no matching approver-role event" in str(captured.value.detail)


def test_unapproved_artifact_cannot_satisfy_exported_transition(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="unapproved-analyst@example.com", role=Role.ANALYST)
    approver = seed_identity(db, email="unapproved-approver@example.com", role=Role.APPROVER)
    case = _seed_exportable_case(client, db, analyst, key_suffix="unapproved-gate")
    unapproved = _post_export(
        client,
        analyst,
        case,
        kind="machine_readable_json",
        key="unapproved-gate-export",
    )
    assert unapproved.status_code == 201, unapproved.text
    assert unapproved.json()["approval_status"] == "not_approved"
    approved = _approve_case(
        client,
        analyst=analyst,
        approver=approver,
        case=case,
        key_prefix="unapproved-gate",
    )

    attempted = _transition(
        client,
        approver,
        case.case_id,
        target="exported",
        expected_version=int(approved["version"]),
        key="unapproved-to-exported",
    )
    assert attempted.status_code == 409
    assert "approved export artifact is required" in attempted.text.lower()
    persisted = db.get(RescueCase, case.case_id)
    artifact = db.get(ExportArtifact, unapproved.json()["id"])
    assert persisted is not None and persisted.status.value == "internal_packet_approved"
    assert artifact is not None and artifact.approval_status == "not_approved"


def test_ready_and_approved_mutations_are_locked_until_controlled_revision_clone(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="locked-analyst@example.com", role=Role.ANALYST)
    approver = seed_identity(db, email="locked-approver@example.com", role=Role.APPROVER)
    case = _seed_exportable_case(client, db, analyst, key_suffix="locked-clone")
    evidence = _transition(
        client,
        analyst,
        case.case_id,
        target="evidence_review",
        expected_version=1,
        key="locked-clone-evidence",
    )
    assert evidence.status_code == 200, evidence.text
    ready = _transition(
        client,
        analyst,
        case.case_id,
        target="ready_for_internal_review",
        expected_version=2,
        key="locked-clone-ready",
    )
    assert ready.status_code == 200, ready.text

    assertion = client.get(f"/v1/cases/{case.case_id}/assertions", headers=analyst.headers).json()[
        0
    ]
    locked_review = client.post(
        f"/v1/cases/assertions/{assertion['id']}/reviews",
        headers={**analyst.headers, "Idempotency-Key": "locked-review-at-ready"},
        json={
            "decision": "accept",
            "expected_assertion_version": assertion["version"],
            "reason": "This must not mutate a ready review revision",
        },
    )
    assert locked_review.status_code == 409

    approved = _transition(
        client,
        approver,
        case.case_id,
        target="internal_packet_approved",
        expected_version=int(ready.json()["version"]),
        key="locked-clone-approved",
    )
    assert approved.status_code == 200, approved.text
    locked_fee_key = "locked-fee-at-approved"
    locked_fee = client.post(
        f"/v1/cases/{case.case_id}/fees",
        headers={**analyst.headers, "Idempotency-Key": locked_fee_key},
        json=_unreviewed_fee_payload(),
    )
    assert locked_fee.status_code == 409

    cloned = _transition(
        client,
        approver,
        case.case_id,
        target="evidence_review",
        expected_version=int(approved.json()["version"]),
        key="locked-clone-new-revision",
    )
    assert cloned.status_code == 200, cloned.text
    cloned_payload = cast(dict[str, Any], cloned.json())
    assert cloned_payload["status"] == "evidence_review"
    assert cloned_payload["current_revision_number"] == 2

    retried_fee = client.post(
        f"/v1/cases/{case.case_id}/fees",
        headers={**analyst.headers, "Idempotency-Key": "fee-after-controlled-clone"},
        json=_unreviewed_fee_payload(),
    )
    assert retried_fee.status_code == 201, retried_fee.text
    reservation = db.scalar(
        select(IdempotencyRecord).where(IdempotencyRecord.key == "fee-after-controlled-clone")
    )
    assert reservation is not None and reservation.response_status == 201

    revisions = list(db.scalars(select(CaseRevision).where(CaseRevision.case_id == case.case_id)))
    assert len(revisions) == 2
    old_revision = next(item for item in revisions if item.number == 1)
    new_revision = next(item for item in revisions if item.number == 2)
    assert new_revision.previous_revision_id == old_revision.id
    old_documents = list(
        db.scalars(
            select(ContractDocument).where(ContractDocument.case_revision_id == old_revision.id)
        )
    )
    new_documents = list(
        db.scalars(
            select(ContractDocument).where(ContractDocument.case_revision_id == new_revision.id)
        )
    )
    assert old_documents and all(not document.superseded for document in old_documents)
    assert len(new_documents) == len(old_documents)
    assert all(not document.superseded for document in new_documents)
    assert {document.sha256 for document in new_documents} == {
        document.sha256 for document in old_documents
    }
    cloned_assertions = list(
        db.scalars(
            select(ContractAssertion).where(ContractAssertion.case_revision_id == new_revision.id)
        )
    )
    assert cloned_assertions
    assert all(item.review_state == AssertionReviewState.PROPOSED for item in cloned_assertions)
    assert all(item.version == 1 and item.is_current for item in cloned_assertions)
    assert all(
        not item.reviewed
        for item in db.scalars(
            select(FeeObligation).where(FeeObligation.case_revision_id == new_revision.id)
        )
    )
    assert (
        db.scalar(
            select(CalculationRun.id).where(CalculationRun.case_revision_id == new_revision.id)
        )
        is None
    )
    assert (
        db.query(AuditEvent)
        .filter_by(action="case.revision_created", object_id=new_revision.id)
        .count()
        == 1
    )


def test_failed_mutation_rolls_back_reservation_so_same_key_can_succeed_later(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="rollback-idempotency@example.com", role=Role.ANALYST)
    case = _seed_exportable_case(client, db, analyst, key_suffix="rollback-idempotency")
    assert (
        _transition(
            client,
            analyst,
            case.case_id,
            target="evidence_review",
            expected_version=1,
            key="rollback-idempotency-evidence",
        ).status_code
        == 200
    )
    ready = _transition(
        client,
        analyst,
        case.case_id,
        target="ready_for_internal_review",
        expected_version=2,
        key="rollback-idempotency-ready",
    )
    assert ready.status_code == 200, ready.text
    retry_key = "rollback-idempotency-fee"
    blocked = client.post(
        f"/v1/cases/{case.case_id}/fees",
        headers={**analyst.headers, "Idempotency-Key": retry_key},
        json=_unreviewed_fee_payload(),
    )
    assert blocked.status_code == 409
    unlocked = _transition(
        client,
        analyst,
        case.case_id,
        target="evidence_review",
        expected_version=int(ready.json()["version"]),
        key="rollback-idempotency-unlock",
    )
    assert unlocked.status_code == 200, unlocked.text

    retry = client.post(
        f"/v1/cases/{case.case_id}/fees",
        headers={**analyst.headers, "Idempotency-Key": retry_key},
        json=_unreviewed_fee_payload(),
    )
    reservation = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.key == retry_key))
    assert (retry.status_code, reservation.response_status if reservation else None) == (201, 201)


def test_prior_revision_assertion_cannot_be_mutated_after_controlled_clone(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="stale-assertion-analyst@example.com", role=Role.ANALYST)
    approver = seed_identity(db, email="stale-assertion-approver@example.com", role=Role.APPROVER)
    case = _seed_exportable_case(client, db, analyst, key_suffix="stale-assertion")
    old_assertion = client.get(
        f"/v1/cases/{case.case_id}/assertions", headers=analyst.headers
    ).json()[0]
    approved = _approve_case(
        client,
        analyst=analyst,
        approver=approver,
        case=case,
        key_prefix="stale-assertion",
    )
    cloned = _transition(
        client,
        approver,
        case.case_id,
        target="evidence_review",
        expected_version=int(approved["version"]),
        key="stale-assertion-clone",
    )
    assert cloned.status_code == 200, cloned.text
    stale_review = client.post(
        f"/v1/cases/assertions/{old_assertion['id']}/reviews",
        headers={**analyst.headers, "Idempotency-Key": "stale-assertion-review"},
        json={
            "decision": "reject",
            "expected_assertion_version": old_assertion["version"],
            "reason": "A prior immutable revision must reject this mutation.",
        },
    )
    persisted = db.get(ContractAssertion, old_assertion["id"])
    assert stale_review.status_code in {404, 409}, stale_review.text
    assert persisted is not None
    assert persisted.version == old_assertion["version"]
    assert persisted.review_state.value == old_assertion["review_state"]


def test_assertion_review_replay_and_optimistic_concurrency_are_single_write(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="assertion-race@example.com", role=Role.ANALYST)
    case = _seed_exportable_case(client, db, analyst, key_suffix="assertion-version")
    target = db.scalar(
        select(ContractAssertion).where(
            ContractAssertion.case_revision_id == case.revision_id,
            ContractAssertion.semantic_key == "contract_end_date",
        )
    )
    assert target is not None
    db.query(ReviewDecision).filter_by(assertion_id=target.id).delete()
    target.review_state = AssertionReviewState.PROPOSED
    db.commit()
    assertion = client.get(f"/v1/cases/{case.case_id}/assertions", headers=analyst.headers).json()[
        0
    ]
    payload = {
        "decision": "accept",
        "expected_assertion_version": assertion["version"],
        "reason": "The exact evidence citation was reviewed again",
        "assumption": False,
    }
    headers = {**analyst.headers, "Idempotency-Key": "assertion-version-success"}
    prior_decisions = db.query(ReviewDecision).filter_by(assertion_id=assertion["id"]).count()
    accepted = client.post(
        f"/v1/cases/assertions/{assertion['id']}/reviews",
        headers=headers,
        json=payload,
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["version"] == assertion["version"] + 1
    replay = client.post(
        f"/v1/cases/assertions/{assertion['id']}/reviews",
        headers=headers,
        json=payload,
    )
    assert replay.status_code == 200
    assert replay.json() == accepted.json()

    stale = client.post(
        f"/v1/cases/assertions/{assertion['id']}/reviews",
        headers={**analyst.headers, "Idempotency-Key": "assertion-version-stale"},
        json=payload,
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["current_version"] == assertion["version"] + 1
    assert (
        db.query(ReviewDecision).filter_by(assertion_id=assertion["id"]).count()
        == prior_decisions + 1
    )
    assert (
        db.query(AuditEvent)
        .filter_by(action="assertion.reviewed", object_id=assertion["id"])
        .count()
        == 1
    )


def test_money_inputs_normalize_only_explicit_supported_values(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="money-valid@example.com", role=Role.ANALYST)
    case_id = _create_case(client, analyst, "money-valid-case")
    normalized = client.post(
        f"/v1/cases/{case_id}/fees",
        headers={**analyst.headers, "Idempotency-Key": "money-valid-normalized"},
        json={
            "category": "other",
            "amount_minor": 0,
            "currency": "usd",
            "payment_status": "UnPaId",
        },
    )
    assert normalized.status_code == 201, normalized.text
    assert normalized.json()["amount_minor"] == 0
    assert normalized.json()["currency"] == "USD"
    assert normalized.json()["payment_status"] == "unpaid"
    upper = client.post(
        f"/v1/cases/{case_id}/fees",
        headers={**analyst.headers, "Idempotency-Key": "money-valid-upper-bound"},
        json={
            "category": "other",
            "amount_minor": MAX_MINOR_UNITS,
            "currency": "JPY",
            "payment_status": "PAID",
        },
    )
    assert upper.status_code == 201, upper.text
    assert upper.json()["amount_minor"] == MAX_MINOR_UNITS
    assert upper.json()["payment_status"] == "paid"


@pytest.mark.parametrize(
    "amount_minor",
    [-1, MAX_MINOR_UNITS + 1, 1.25, 100.0, True, "100"],
    ids=[
        "negative",
        "over-safe-integer-limit",
        "fractional",
        "integral-float",
        "boolean",
        "string",
    ],
)
def test_amount_minor_rejects_out_of_domain_or_non_integer_json_types(
    client: TestClient,
    db: Session,
    amount_minor: Any,
) -> None:
    analyst = seed_identity(
        db,
        email=f"money-invalid-{type(amount_minor).__name__}-{amount_minor}@example.com",
        role=Role.ANALYST,
    )
    case_id = _create_case(client, analyst, "money-invalid-amount-case")
    response = client.post(
        f"/v1/cases/{case_id}/fees",
        headers={**analyst.headers, "Idempotency-Key": "money-invalid-amount"},
        json={
            "category": "other",
            "amount_minor": amount_minor,
            "currency": "USD",
            "payment_status": "unknown",
        },
    )
    assert response.status_code == 422, response.text
    assert db.query(FeeObligation).count() == 0


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("currency", "ZZZ"),
        ("currency", " USD "),
        ("payment_status", "settled"),
        ("payment_status", " paid "),
        ("payment_status", 1),
    ],
)
def test_currency_and_payment_status_reject_unknown_or_padded_values(
    client: TestClient,
    db: Session,
    field: str,
    invalid_value: Any,
) -> None:
    analyst = seed_identity(
        db,
        email=f"money-invalid-{field}-{str(invalid_value).strip()}@example.com",
        role=Role.ANALYST,
    )
    case_id = _create_case(client, analyst, f"money-invalid-{field}-case")
    payload: dict[str, Any] = {
        "category": "other",
        "amount_minor": 100,
        "currency": "USD",
        "payment_status": "unknown",
    }
    payload[field] = invalid_value
    response = client.post(
        f"/v1/cases/{case_id}/fees",
        headers={**analyst.headers, "Idempotency-Key": f"money-invalid-{field}"},
        json=payload,
    )
    assert response.status_code == 422, response.text
    assert db.query(FeeObligation).count() == 0


def test_document_tampering_is_detected_before_processing_and_audited(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="tamper-document@example.com", role=Role.ANALYST)
    case = create_document_case(client, analyst, key="tamper-document-case")
    uploaded = upload_document(
        client,
        analyst,
        str(case["id"]),
        "clean_standard.pdf",
        key="tamper-document-upload",
    )
    assert uploaded.status_code == 201, uploaded.text
    document = db.get(ContractDocument, uploaded.json()["id"])
    assert document is not None
    stored_path = get_settings().upload_dir / document.stored_filename
    original = stored_path.read_bytes()
    stored_path.write_bytes(bytes([original[0] ^ 0xFF]) + original[1:])
    assert stored_path.stat().st_size == document.size_bytes
    assert hashlib.sha256(stored_path.read_bytes()).hexdigest() != document.sha256

    processed = process_document(
        client,
        analyst,
        document.id,
        key="tamper-document-process",
    )
    assert processed.status_code == 422
    assert "failed safely" in processed.text
    detail = client.get(f"/v1/documents/{document.id}", headers=analyst.headers)
    assert detail.status_code == 200
    assert detail.json()["processing_status"] == "failed"
    assert "SHA-256 integrity check" in detail.json()["processing_error"]
    failure_event = db.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "document.processing_failed",
            AuditEvent.object_id == document.id,
        )
    )
    assert failure_event is not None
    assert failure_event.after == {
        "processing_status": "failed",
        "error_type": "RuntimeError",
    }
    replayed = process_document(
        client,
        analyst,
        document.id,
        key="tamper-document-process",
    )
    assert replayed.status_code == 422
    assert replayed.json() == processed.json()
    assert (
        db.query(AuditEvent)
        .filter_by(action="document.processing_failed", object_id=document.id)
        .count()
        == 1
    )
    reservation = (
        db.query(IdempotencyRecord)
        .filter_by(
            organization_id=analyst.organization_id,
            endpoint=f"POST /v1/documents/{document.id}/process",
            key="tamper-document-process",
        )
        .one()
    )
    assert reservation.response_status == 422


def test_mid_extraction_failure_rolls_back_every_partial_row_and_replays_cleanly(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    analyst = seed_identity(db, email="partial-extraction@example.com", role=Role.ANALYST)
    case = create_document_case(client, analyst, key="partial-extraction-case")
    uploaded = upload_document(
        client,
        analyst,
        str(case["id"]),
        "clean_standard.pdf",
        key="partial-extraction-upload",
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = str(uploaded.json()["id"])
    original_persist = documents_service._persist_extraction_result
    attempts = 0

    def fail_after_partial_persist(*args: Any, **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        original_persist(*args, **kwargs)
        session = cast(Session, args[0])
        session.flush()
        raise RuntimeError("injected failure after extraction rows were flushed")

    monkeypatch.setattr(
        documents_service,
        "_persist_extraction_result",
        fail_after_partial_persist,
    )
    first = process_document(
        client,
        analyst,
        document_id,
        key="partial-extraction-process",
    )
    assert first.status_code == 422, first.text
    replay = process_document(
        client,
        analyst,
        document_id,
        key="partial-extraction-process",
    )
    assert replay.status_code == 422
    assert replay.json() == first.json()
    assert attempts == 1

    db.expire_all()
    document = db.get(ContractDocument, document_id)
    assert document is not None
    assert document.processing_status.value == "failed"
    assert "injected failure" in (document.processing_error or "")
    assert db.query(DocumentPage).filter_by(document_id=document_id).count() == 0
    assert db.query(ExtractionRun).filter_by(document_id=document_id).count() == 0
    assert db.query(EvidenceSpan).count() == 0
    assert db.query(ContractAssertion).count() == 0
    assert (
        db.query(AuditEvent)
        .filter_by(action="document.processing_failed", object_id=document_id)
        .count()
        == 1
    )
    reservations = (
        db.query(IdempotencyRecord)
        .filter_by(
            organization_id=analyst.organization_id,
            endpoint=f"POST /v1/documents/{document_id}/process",
            key="partial-extraction-process",
        )
        .all()
    )
    assert len(reservations) == 1
    assert reservations[0].response_status == 422

    monkeypatch.setattr(documents_service, "_persist_extraction_result", original_persist)
    recovered = process_document(
        client,
        analyst,
        document_id,
        key="partial-extraction-recovery",
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["processing_status"] == "processed"
    assert db.query(DocumentPage).filter_by(document_id=document_id).count() > 0
    assert db.query(ExtractionRun).filter_by(document_id=document_id).count() == 1
    assert db.query(ContractAssertion).count() > 0
    assert (
        db.query(AuditEvent)
        .filter_by(action="document.processing_failed", object_id=document_id)
        .count()
        == 1
    )
    assert (
        db.query(AuditEvent).filter_by(action="document.processed", object_id=document_id).count()
        == 1
    )


def test_optional_ai_mid_persist_failure_rolls_back_deterministic_and_ai_lineage(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    analyst = seed_identity(db, email="partial-ai-extraction@example.com", role=Role.ANALYST)
    case = create_document_case(client, analyst, key="partial-ai-case")
    uploaded = upload_document(
        client,
        analyst,
        str(case["id"]),
        "clean_standard.pdf",
        key="partial-ai-upload",
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = str(uploaded.json()["id"])
    original_settings = documents_service.get_settings()
    original_persist = documents_service._persist_extraction_result
    persistence_calls = 0

    monkeypatch.setattr(
        documents_service,
        "get_settings",
        lambda: original_settings.model_copy(update={"enable_local_ai": True}),
    )
    monkeypatch.setattr(
        documents_service,
        "extract_with_ollama",
        lambda pages, **_kwargs: documents_service.deterministic_extract(pages),
    )

    def fail_during_ai_persistence(*args: Any, **kwargs: Any) -> Any:
        nonlocal persistence_calls
        persistence_calls += 1
        result = original_persist(*args, **kwargs)
        if persistence_calls == 2:
            session = cast(Session, args[0])
            session.flush()
            raise RuntimeError("injected failure during optional AI persistence")
        return result

    monkeypatch.setattr(
        documents_service,
        "_persist_extraction_result",
        fail_during_ai_persistence,
    )
    failed = process_document(client, analyst, document_id, key="partial-ai-process")
    assert failed.status_code == 422, failed.text
    assert persistence_calls == 2

    db.expire_all()
    document = db.get(ContractDocument, document_id)
    assert document is not None and document.processing_status.value == "failed"
    assert "optional AI persistence" in (document.processing_error or "")
    assert db.query(DocumentPage).filter_by(document_id=document_id).count() == 0
    assert db.query(ExtractionRun).filter_by(document_id=document_id).count() == 0
    assert db.query(EvidenceSpan).count() == 0
    assert db.query(ContractAssertion).count() == 0
    assert (
        db.query(AuditEvent)
        .filter_by(action="document.processing_failed", object_id=document_id)
        .count()
        == 1
    )

    monkeypatch.setattr(documents_service, "_persist_extraction_result", original_persist)
    monkeypatch.setattr(
        documents_service,
        "get_settings",
        lambda: original_settings.model_copy(update={"enable_local_ai": False}),
    )
    recovered = process_document(client, analyst, document_id, key="partial-ai-recovery")
    assert recovered.status_code == 200, recovered.text
    assert db.query(ExtractionRun).filter_by(document_id=document_id).count() == 1
    assert (
        db.query(AuditEvent).filter_by(action="document.processed", object_id=document_id).count()
        == 1
    )


def test_already_processed_document_rechecks_bytes_before_idempotent_return(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="tamper-processed@example.com", role=Role.ANALYST)
    case = create_document_case(client, analyst, key="tamper-processed-case")
    uploaded = upload_document(
        client,
        analyst,
        str(case["id"]),
        "clean_standard.pdf",
        key="tamper-processed-upload",
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = str(uploaded.json()["id"])
    first_process = process_document(
        client,
        analyst,
        document_id,
        key="tamper-processed-first-process",
    )
    assert first_process.status_code == 200, first_process.text
    document = db.get(ContractDocument, document_id)
    assert document is not None
    stored_path = get_settings().upload_dir / document.stored_filename
    original = stored_path.read_bytes()
    stored_path.write_bytes(bytes([original[0] ^ 0xFF]) + original[1:])

    reprocessed = process_document(
        client,
        analyst,
        document_id,
        key="tamper-processed-recheck",
    )
    assert reprocessed.status_code == 422, reprocessed.text
    assert "failed safely" in reprocessed.text


def test_processing_corrupted_document_from_prior_revision_cannot_mutate_history(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="old-doc-analyst@example.com", role=Role.ANALYST)
    approver = seed_identity(db, email="old-doc-approver@example.com", role=Role.APPROVER)
    case = _seed_exportable_case(client, db, analyst, key_suffix="old-doc-immutable")
    old_document = db.scalar(
        select(ContractDocument).where(ContractDocument.case_revision_id == case.revision_id)
    )
    assert old_document is not None
    assert old_document.processing_status.value == "processed"
    approved = _approve_case(
        client,
        analyst=analyst,
        approver=approver,
        case=case,
        key_prefix="old-doc-immutable",
    )
    reopened = _transition(
        client,
        approver,
        case.case_id,
        target="evidence_review",
        expected_version=int(approved["version"]),
        key="old-doc-immutable-reopen",
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["current_revision_number"] == 2

    stored_path = get_settings().upload_dir / old_document.stored_filename
    original = stored_path.read_bytes()
    stored_path.write_bytes(bytes([original[0] ^ 0xFF]) + original[1:])
    failure_audits_before = (
        db.query(AuditEvent)
        .filter_by(action="document.processing_failed", object_id=old_document.id)
        .count()
    )

    rejected = process_document(
        client,
        analyst,
        old_document.id,
        key="old-doc-immutable-reprocess",
    )

    assert rejected.status_code == 409, rejected.text
    assert "prior revision is immutable" in rejected.text.lower()
    db.expire_all()
    persisted = db.get(ContractDocument, old_document.id)
    assert persisted is not None
    assert persisted.processing_status.value == "processed"
    assert persisted.processing_error is None
    assert (
        db.query(AuditEvent)
        .filter_by(action="document.processing_failed", object_id=old_document.id)
        .count()
        == failure_audits_before
    )
    assert db.query(IdempotencyRecord).filter_by(key="old-doc-immutable-reprocess").count() == 0


def test_missing_approved_export_bytes_cannot_satisfy_exported_transition(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="missing-export-analyst@example.com", role=Role.ANALYST)
    approver = seed_identity(db, email="missing-export-approver@example.com", role=Role.APPROVER)
    case = _seed_exportable_case(client, db, analyst, key_suffix="missing-export")
    approved = _approve_case(
        client,
        analyst=analyst,
        approver=approver,
        case=case,
        key_prefix="missing-export",
    )
    generated = _post_export(
        client,
        approver,
        case,
        kind="machine_readable_json",
        key="missing-export-artifact",
    )
    assert generated.status_code == 201, generated.text
    artifact = db.get(ExportArtifact, generated.json()["id"])
    assert artifact is not None and artifact.approval_status == "approved"
    stored_path = get_settings().upload_dir.parent / "exports" / artifact.stored_filename
    assert stored_path.is_file()
    stored_path.unlink()

    exported = _transition(
        client,
        approver,
        case.case_id,
        target="exported",
        expected_version=int(approved["version"]),
        key="missing-export-transition",
    )
    assert exported.status_code == 409, exported.text
    assert "artifact" in exported.text.lower()


def test_exported_transition_keeps_the_approved_artifact_current_and_downloadable(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="delivery-analyst@example.com", role=Role.ANALYST)
    approver = seed_identity(db, email="delivery-approver@example.com", role=Role.APPROVER)
    case = _seed_exportable_case(client, db, analyst, key_suffix="delivery-retention")
    approved = _approve_case(
        client,
        analyst=analyst,
        approver=approver,
        case=case,
        key_prefix="delivery-retention",
    )
    generated = _post_export(
        client,
        approver,
        case,
        kind="machine_readable_json",
        key="delivery-retention-export",
    )
    assert generated.status_code == 201, generated.text
    before_bytes = client.get(generated.json()["download_url"], headers=approver.headers)
    assert before_bytes.status_code == 200, before_bytes.text

    exported = _transition(
        client,
        approver,
        case.case_id,
        target="exported",
        expected_version=int(approved["version"]),
        key="delivery-retention-exported",
    )
    assert exported.status_code == 200, exported.text
    assert exported.json()["status"] == "exported"

    db.expire_all()
    artifact = db.get(ExportArtifact, generated.json()["id"])
    assert artifact is not None and artifact.superseded is False
    assert (
        db.query(AuditEvent).filter_by(action="export.superseded", object_id=artifact.id).count()
        == 0
    )
    after_bytes = client.get(generated.json()["download_url"], headers=approver.headers)
    assert after_bytes.status_code == 200, after_bytes.text
    assert after_bytes.content == before_bytes.content
    assert after_bytes.headers["X-Artifact-SHA256"] == generated.json()["sha256"]
    assert (
        after_bytes.headers["X-Packet-Snapshot-SHA256"]
        == generated.json()["packet_snapshot_sha256"]
    )


def test_legacy_export_is_quarantined_but_remains_integrity_listable(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="legacy-export@example.com", role=Role.ANALYST)
    case = _seed_exportable_case(client, db, analyst, key_suffix="legacy-export")
    rendered = b'{"legacy":true}\n'
    stored_filename = "legacy-export-v1.json"
    export_dir = get_settings().upload_dir.parent / "exports"
    export_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    (export_dir / stored_filename).write_bytes(rendered)
    artifact = ExportArtifact(
        case_revision_id=case.revision_id,
        calculation_id=case.calculation_id,
        kind=ExportKind.MACHINE_READABLE_JSON,
        stored_filename=stored_filename,
        sha256=hashlib.sha256(rendered).hexdigest(),
        packet_snapshot_sha256="b" * 64,
        revision_snapshot_sha256="c" * 64,
        approval_status="not_approved",
        approval_case_version=None,
        approved_by=None,
        prepared_at=datetime.now(UTC),
        prepared_by=analyst.user_id,
        generator_version="rescuedesk-export-v1",
        superseded=True,
        created_by=analyst.user_id,
    )
    db.add(artifact)
    db.flush()
    append_audit_event(
        db,
        organization_id=analyst.organization_id,
        actor_id=analyst.user_id,
        action="export.created",
        object_type="export_artifact",
        object_id=artifact.id,
        correlation_id="legacy-export-created",
        after={
            "case_id": case.case_id,
            "case_revision_id": case.revision_id,
            "calculation_id": case.calculation_id,
            "kind": artifact.kind.value,
            "artifact_sha256": artifact.sha256,
            "packet_snapshot_sha256": artifact.packet_snapshot_sha256,
            "approval_status": artifact.approval_status,
            "generator_version": artifact.generator_version,
            "size_bytes": len(rendered),
        },
    )
    db.commit()

    listed = client.get(f"/v1/cases/{case.case_id}/exports", headers=analyst.headers)
    assert listed.status_code == 200, listed.text
    legacy = next(item for item in listed.json() if item["id"] == artifact.id)
    assert legacy["superseded"] is True
    assert legacy["generator_version"] == "rescuedesk-export-v1"
    assert legacy["download_url"].endswith(f"/exports/{artifact.id}/download")
    retired_download = client.get(legacy["download_url"], headers=analyst.headers)
    assert retired_download.status_code == 409, retired_download.text


def test_finding_disposition_and_document_supersession_are_versioned_and_idempotent(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="disposition-analyst@example.com", role=Role.ANALYST)
    approver = seed_identity(db, email="disposition-approver@example.com", role=Role.APPROVER)
    case = create_document_case(client, analyst, key="disposition-case")
    case_id = str(case["id"])
    first = upload_document(
        client,
        analyst,
        case_id,
        "clean_standard.pdf",
        key="disposition-first-upload",
    )
    second = upload_document(
        client,
        analyst,
        case_id,
        "fee_and_date_variants.pdf",
        key="disposition-second-upload",
    )
    assert first.status_code == second.status_code == 201
    assert (
        process_document(
            client, analyst, first.json()["id"], key="disposition-first-process"
        ).status_code
        == 200
    )
    assert (
        process_document(
            client, analyst, second.json()["id"], key="disposition-second-process"
        ).status_code
        == 200
    )
    revision = db.query(CaseRevision).filter_by(case_id=case_id, number=1).one()
    finding = ReadinessFinding(
        case_revision_id=revision.id,
        code="contract.manual_review",
        title="Manual contract review",
        detail="An approver must explicitly disposition this synthetic finding.",
        severity=FindingSeverity.WARNING,
        status=FindingStatus.OPEN,
    )
    db.add(finding)
    db.commit()

    disposition_payload = {
        "status": "accepted_risk",
        "expected_version": 1,
        "reason": "Approver reviewed and accepted this synthetic ambiguity.",
    }
    forbidden = client.post(
        f"/v1/cases/{case_id}/findings/{finding.id}/disposition",
        headers={**analyst.headers, "Idempotency-Key": "disposition-forbidden"},
        json=disposition_payload,
    )
    assert forbidden.status_code == 403
    accepted_headers = {**approver.headers, "Idempotency-Key": "disposition-accepted"}
    accepted = client.post(
        f"/v1/cases/{case_id}/findings/{finding.id}/disposition",
        headers=accepted_headers,
        json=disposition_payload,
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "accepted_risk"
    assert accepted.json()["version"] == 2
    assert accepted.json()["resolved_at"] is not None
    disposition_replay = client.post(
        f"/v1/cases/{case_id}/findings/{finding.id}/disposition",
        headers=accepted_headers,
        json=disposition_payload,
    )
    assert disposition_replay.status_code == 200
    assert disposition_replay.json() == accepted.json()

    current = client.get(f"/v1/cases/{case_id}", headers=analyst.headers).json()
    stale_supersede = client.post(
        f"/v1/documents/{first.json()['id']}/supersede",
        headers={**analyst.headers, "Idempotency-Key": "supersede-stale-version"},
        json={
            "expected_case_version": int(current["version"]) - 1,
            "reason": "This stale request must not supersede evidence.",
        },
    )
    assert stale_supersede.status_code == 409
    supersede_headers = {**analyst.headers, "Idempotency-Key": "supersede-success"}
    supersede_payload = {
        "expected_case_version": current["version"],
        "reason": "A processed replacement is present and reviewed.",
    }
    superseded = client.post(
        f"/v1/documents/{first.json()['id']}/supersede",
        headers=supersede_headers,
        json=supersede_payload,
    )
    assert superseded.status_code == 200, superseded.text
    assert superseded.json()["superseded"] is True
    supersede_replay = client.post(
        f"/v1/documents/{first.json()['id']}/supersede",
        headers=supersede_headers,
        json=supersede_payload,
    )
    assert supersede_replay.status_code == 200
    assert supersede_replay.json() == superseded.json()
    persisted_case = db.get(RescueCase, case_id)
    assert persisted_case is not None
    assert persisted_case.version == int(current["version"]) + 1
    finding_event = db.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "finding.disposition_recorded",
            AuditEvent.object_id == finding.id,
        )
    )
    document_event = db.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "document.superseded",
            AuditEvent.object_id == first.json()["id"],
        )
    )
    assert finding_event is not None and finding_event.before == {
        "status": "open",
        "version": 1,
        "resolution_reason": None,
    }
    assert finding_event.after is not None and finding_event.after["version"] == 2
    assert document_event is not None and document_event.before == {
        "superseded": False,
        "case_version": current["version"],
    }
    assert document_event.after is not None and document_event.after["superseded"] is True


def test_case_audit_endpoint_covers_material_actions_and_chain_verifies(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="audit-complete-analyst@example.com", role=Role.ANALYST)
    approver = seed_identity(db, email="audit-complete-approver@example.com", role=Role.APPROVER)
    case = _seed_exportable_case(client, db, analyst, key_suffix="audit-complete")
    assertion = client.get(f"/v1/cases/{case.case_id}/assertions", headers=analyst.headers).json()[
        0
    ]
    reviewed = client.post(
        f"/v1/cases/assertions/{assertion['id']}/reviews",
        headers={**analyst.headers, "Idempotency-Key": "audit-complete-review"},
        json={
            "decision": "correct",
            "expected_assertion_version": assertion["version"],
            "reason": "Canonicalize the same source-backed date for audit coverage.",
            "corrected_value": assertion["normalized_value"],
            "assumption": False,
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    finding = ReadinessFinding(
        case_revision_id=case.revision_id,
        code="audit.synthetic_warning",
        title="Synthetic warning",
        detail="This warning exists to prove audit endpoint coverage.",
        severity=FindingSeverity.WARNING,
        status=FindingStatus.OPEN,
    )
    db.add(finding)
    db.commit()
    disposed = client.post(
        f"/v1/cases/{case.case_id}/findings/{finding.id}/disposition",
        headers={**approver.headers, "Idempotency-Key": "audit-complete-finding"},
        json={
            "status": "resolved",
            "expected_version": 1,
            "reason": "Synthetic warning reviewed and resolved.",
        },
    )
    assert disposed.status_code == 200, disposed.text
    _approve_case(
        client,
        analyst=analyst,
        approver=approver,
        case=case,
        key_prefix="audit-complete",
    )
    generated = _post_export(
        client,
        approver,
        case,
        kind="machine_readable_json",
        key="audit-complete-export",
    )
    assert generated.status_code == 201, generated.text
    downloaded = client.get(generated.json()["download_url"], headers=approver.headers)
    assert downloaded.status_code == 200, downloaded.text

    response = client.get(
        f"/v1/cases/{case.case_id}/audit-events",
        headers={**analyst.headers, "X-Result-Limit": "500"},
    )
    assert response.status_code == 200, response.text
    events = cast(list[dict[str, Any]], response.json())
    actions = {item["action"] for item in events}
    assert {
        "case.created",
        "assertion.reviewed",
        "fee.created",
        "calculation.created",
        "finding.disposition_recorded",
        "case.transitioned",
        "export.created",
        "export.downloaded",
    }.issubset(actions)
    assert all(item["actor_id"] in {analyst.user_id, approver.user_id} for item in events)
    assert all(item["correlation_id"] for item in events)
    assert all(len(item["event_hash"]) == 64 for item in events)
    assert all(item["after"] is not None for item in events)

    persisted = list(
        db.scalars(
            select(AuditEvent)
            .where(AuditEvent.organization_id == analyst.organization_id)
            .order_by(AuditEvent.created_at, AuditEvent.id)
        )
    )
    assert len(persisted) == len(events)
    assert verify_audit_chain([(_audit_payload(item), item.event_hash) for item in persisted])


def test_concurrent_identical_case_requests_commit_one_business_write_and_one_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "idempotency-concurrency.db"
    runtime_engine = create_engine(
        f"sqlite+pysqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    session_factory = sessionmaker(
        bind=runtime_engine,
        autoflush=False,
        expire_on_commit=False,
    )
    Base.metadata.create_all(runtime_engine)
    monkeypatch.setenv("RESCUEDESK_UPLOAD_DIR", str(tmp_path / "runtime" / "uploads"))
    get_settings.cache_clear()
    with session_factory() as seed_session:
        identity = seed_identity(
            seed_session,
            email="idempotency-concurrency@example.com",
            role=Role.ANALYST,
        )

    def override_db() -> Any:
        session = session_factory()
        try:
            yield session
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    request_barrier = Barrier(2)
    thread_state = threading.local()

    @event.listens_for(runtime_engine, "before_cursor_execute")
    def synchronize_reservation_inserts(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        is_reservation_insert = "INSERT INTO IDEMPOTENCY_RECORDS" in statement.upper()
        if is_reservation_insert and not getattr(thread_state, "reservation_insert", False):
            thread_state.reservation_insert = True
            request_barrier.wait(timeout=5)

    payload = {
        "display_name": "Concurrent idempotency review",
        "applicant_company": "Synthetic Concurrent Company",
        "erp_provider": "Synthetic ERP",
    }
    headers = {**identity.headers, "Idempotency-Key": "concurrent-case-create"}
    app.dependency_overrides[get_db] = override_db
    try:
        with (
            TestClient(app) as first_client,
            TestClient(app) as second_client,
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            first_future = executor.submit(
                first_client.post,
                "/v1/cases",
                headers=headers,
                json=payload,
            )
            second_future = executor.submit(
                second_client.post,
                "/v1/cases",
                headers=headers,
                json=payload,
            )
            first = first_future.result(timeout=15)
            second = second_future.result(timeout=15)
        assert first.status_code == second.status_code == 201, (first.text, second.text)
        assert first.json() == second.json()
    finally:
        app.dependency_overrides.clear()
        event.remove(
            runtime_engine,
            "before_cursor_execute",
            synchronize_reservation_inserts,
        )

    with session_factory() as verification_session:
        cases = list(verification_session.scalars(select(RescueCase)))
        reservations = list(verification_session.scalars(select(IdempotencyRecord)))
        created_events = list(
            verification_session.scalars(
                select(AuditEvent).where(AuditEvent.action == "case.created")
            )
        )
        assert len(cases) == 1
        assert len(reservations) == 1
        assert reservations[0].response_status == 201
        assert len(created_events) == 1

    Base.metadata.drop_all(runtime_engine)
    runtime_engine.dispose()
    get_settings.cache_clear()
