from __future__ import annotations

from typing import cast

import pytest
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.testclient import TestClient

from rescue_desk.config import get_settings
from rescue_desk.domain.hashing import hash_payload
from rescue_desk.models import (
    AuditEvent,
    CaseRevision,
    CaseStatus,
    ContractAssertion,
    ContractDocument,
    DocumentPage,
    ExportArtifact,
    IdempotencyRecord,
    RescueCase,
    Role,
)
from rescue_desk.services.cases import revision_snapshot
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
                "reason": f"Revision-clone regression transition to {target}",
            },
        ),
    )


def _evidence_signatures(
    db: Session, assertion: ContractAssertion
) -> list[tuple[str, bool, int, str, int, int]]:
    signatures: list[tuple[str, bool, int, str, int, int]] = []
    for evidence in assertion.evidence:
        page = db.get(DocumentPage, evidence.page_id)
        assert page is not None
        document = db.get(ContractDocument, page.document_id)
        assert document is not None
        signatures.append(
            (
                document.sha256,
                document.superseded,
                page.page_number,
                evidence.quote_sha256,
                evidence.char_start,
                evidence.char_end,
            )
        )
    return sorted(signatures)


@pytest.mark.parametrize(
    "locked_status",
    [
        "ready_for_internal_review",
        "internal_packet_approved",
        "exported",
    ],
)
def test_upload_to_locked_status_is_a_complete_no_op(
    client: TestClient,
    db: Session,
    locked_status: str,
) -> None:
    analyst = seed_identity(
        db,
        email=f"locked-upload-{locked_status}-analyst@example.com",
        role=Role.ANALYST,
    )
    approver = seed_identity(
        db,
        email=f"locked-upload-{locked_status}-approver@example.com",
        role=Role.APPROVER,
    )
    case = _seed_exportable_case(
        client,
        db,
        analyst,
        key_suffix=f"locked-upload-{locked_status}",
    )
    evidence = _transition(
        client,
        analyst,
        case.case_id,
        target="evidence_review",
        expected_version=1,
        key=f"locked-upload-{locked_status}-evidence",
    )
    assert evidence.status_code == 200, evidence.text
    state = _transition(
        client,
        analyst,
        case.case_id,
        target="ready_for_internal_review",
        expected_version=int(evidence.json()["version"]),
        key=f"locked-upload-{locked_status}-ready",
    )
    assert state.status_code == 200, state.text
    if locked_status in {"internal_packet_approved", "exported"}:
        state = _transition(
            client,
            approver,
            case.case_id,
            target="internal_packet_approved",
            expected_version=int(state.json()["version"]),
            key=f"locked-upload-{locked_status}-approved",
        )
        assert state.status_code == 200, state.text
    if locked_status == "exported":
        generated = _post_export(
            client,
            approver,
            case,
            kind="machine_readable_json",
            key="locked-upload-exported-artifact",
        )
        assert generated.status_code == 201, generated.text
        state = _transition(
            client,
            approver,
            case.case_id,
            target="exported",
            expected_version=int(state.json()["version"]),
            key="locked-upload-exported-transition",
        )
        assert state.status_code == 200, state.text
    assert state.json()["status"] == locked_status

    db.expire_all()
    persisted = db.get(RescueCase, case.case_id)
    assert persisted is not None
    frozen_case_state = (
        persisted.status,
        persisted.version,
        persisted.current_revision_number,
    )
    frozen_counts = (
        db.query(CaseRevision).count(),
        db.query(ContractDocument).count(),
        db.query(AuditEvent).count(),
    )
    frozen_artifact_flags = {
        artifact.id: artifact.superseded for artifact in db.query(ExportArtifact).all()
    }
    runtime_root = get_settings().upload_dir.parent
    frozen_runtime_files = {
        path.relative_to(runtime_root).as_posix()
        for path in runtime_root.rglob("*")
        if path.is_file()
    }
    idempotency_key = f"locked-upload-{locked_status}-attempt"

    attempted = upload_document(
        client,
        analyst,
        case.case_id,
        "fee_and_date_variants.pdf",
        key=idempotency_key,
    )
    assert attempted.status_code == 409, attempted.text
    assert "revision is locked" in attempted.text.lower()

    db.expire_all()
    persisted = db.get(RescueCase, case.case_id)
    assert persisted is not None
    assert (
        persisted.status,
        persisted.version,
        persisted.current_revision_number,
    ) == frozen_case_state
    assert (
        db.query(CaseRevision).count(),
        db.query(ContractDocument).count(),
        db.query(AuditEvent).count(),
    ) == frozen_counts
    assert {
        artifact.id: artifact.superseded for artifact in db.query(ExportArtifact).all()
    } == frozen_artifact_flags
    assert db.query(IdempotencyRecord).filter_by(key=idempotency_key).one_or_none() is None
    assert {
        path.relative_to(runtime_root).as_posix()
        for path in runtime_root.rglob("*")
        if path.is_file()
    } == frozen_runtime_files


def test_reopening_keeps_approved_revision_frozen_and_retires_prior_artifacts(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="frozen-analyst@example.com", role=Role.ANALYST)
    approver = seed_identity(db, email="frozen-approver@example.com", role=Role.APPROVER)
    case = _seed_exportable_case(client, db, analyst, key_suffix="frozen-revision")

    evidence = _transition(
        client,
        analyst,
        case.case_id,
        target="evidence_review",
        expected_version=1,
        key="frozen-revision-evidence",
    )
    assert evidence.status_code == 200, evidence.text
    ready = _transition(
        client,
        analyst,
        case.case_id,
        target="ready_for_internal_review",
        expected_version=int(evidence.json()["version"]),
        key="frozen-revision-ready",
    )
    assert ready.status_code == 200, ready.text
    approved = _transition(
        client,
        approver,
        case.case_id,
        target="internal_packet_approved",
        expected_version=int(ready.json()["version"]),
        key="frozen-revision-approved",
    )
    assert approved.status_code == 200, approved.text
    generated = _post_export(
        client,
        approver,
        case,
        kind="machine_readable_json",
        key="frozen-revision-export",
    )
    assert generated.status_code == 201, generated.text

    db.expire_all()
    old_revision = db.get(CaseRevision, case.revision_id)
    artifact = db.get(ExportArtifact, generated.json()["id"])
    assert old_revision is not None and old_revision.snapshot_hash is not None
    assert artifact is not None and artifact.superseded is False
    old_documents = list(
        db.scalars(
            select(ContractDocument).where(ContractDocument.case_revision_id == old_revision.id)
        )
    )
    assert old_documents
    frozen_document_state = {
        document.id: (
            document.superseded,
            document.stored_filename,
            document.sha256,
            document.processing_status,
        )
        for document in old_documents
    }
    frozen_artifact_content = (
        artifact.stored_filename,
        artifact.sha256,
        artifact.packet_snapshot_sha256,
        artifact.revision_snapshot_sha256,
    )
    frozen_snapshot_hash = old_revision.snapshot_hash
    frozen_snapshot = revision_snapshot(db, old_revision.id)
    assert hash_payload(frozen_snapshot) == frozen_snapshot_hash

    reopened = _transition(
        client,
        approver,
        case.case_id,
        target="evidence_review",
        expected_version=int(approved.json()["version"]),
        key="frozen-revision-reopen",
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["current_revision_number"] == 2

    db.expire_all()
    old_revision = db.get(CaseRevision, case.revision_id)
    artifact = db.get(ExportArtifact, generated.json()["id"])
    assert old_revision is not None and artifact is not None
    assert old_revision.snapshot_hash == frozen_snapshot_hash
    assert revision_snapshot(db, old_revision.id) == frozen_snapshot
    assert {
        document.id: (
            document.superseded,
            document.stored_filename,
            document.sha256,
            document.processing_status,
        )
        for document in db.scalars(
            select(ContractDocument).where(ContractDocument.case_revision_id == old_revision.id)
        )
    } == frozen_document_state
    assert artifact.superseded is True
    assert (
        artifact.stored_filename,
        artifact.sha256,
        artifact.packet_snapshot_sha256,
        artifact.revision_snapshot_sha256,
    ) == frozen_artifact_content
    listed = client.get(f"/v1/cases/{case.case_id}/exports", headers=analyst.headers)
    assert listed.status_code == 200, listed.text
    listed_artifact = next(item for item in listed.json() if item["id"] == artifact.id)
    assert listed_artifact["superseded"] is True


def test_reopening_clones_superseded_source_evidence_and_preserves_provenance(
    client: TestClient, db: Session
) -> None:
    analyst = seed_identity(db, email="superseded-evidence@example.com", role=Role.ANALYST)
    created = create_document_case(client, analyst, key="superseded-evidence-case")
    case_id = str(created["id"])
    first = upload_document(
        client,
        analyst,
        case_id,
        "clean_standard.pdf",
        key="superseded-evidence-first",
    )
    assert first.status_code == 201, first.text
    assert (
        process_document(
            client,
            analyst,
            first.json()["id"],
            key="superseded-evidence-process-first",
        ).status_code
        == 200
    )
    second = upload_document(
        client,
        analyst,
        case_id,
        "fee_and_date_variants.pdf",
        key="superseded-evidence-second",
    )
    assert second.status_code == 201, second.text
    processed_second = process_document(
        client,
        analyst,
        second.json()["id"],
        key="superseded-evidence-process-second",
    )
    assert processed_second.status_code == 200, processed_second.text

    current = client.get(f"/v1/cases/{case_id}", headers=analyst.headers)
    assert current.status_code == 200, current.text
    superseded = client.post(
        f"/v1/documents/{first.json()['id']}/supersede",
        headers={**analyst.headers, "Idempotency-Key": "superseded-evidence-retire"},
        json={
            "expected_case_version": current.json()["version"],
            "reason": "The second processed synthetic source replaces the first.",
        },
    )
    assert superseded.status_code == 200, superseded.text

    db.expire_all()
    case_record = db.get(RescueCase, case_id)
    assert case_record is not None
    old_revision = db.scalar(
        select(CaseRevision).where(
            CaseRevision.case_id == case_id,
            CaseRevision.number == 1,
        )
    )
    assert old_revision is not None
    old_documents = list(
        db.scalars(
            select(ContractDocument)
            .where(ContractDocument.case_revision_id == old_revision.id)
            .order_by(ContractDocument.id)
        )
    )
    assert len(old_documents) == 2
    assert {document.id: document.superseded for document in old_documents} == {
        first.json()["id"]: True,
        second.json()["id"]: False,
    }
    old_current_assertions = list(
        db.scalars(
            select(ContractAssertion)
            .options(selectinload(ContractAssertion.evidence))
            .where(
                ContractAssertion.case_revision_id == old_revision.id,
                ContractAssertion.is_current.is_(True),
            )
            .order_by(ContractAssertion.semantic_key)
        )
    )
    assert old_current_assertions
    old_provenance = {
        assertion.semantic_key: _evidence_signatures(db, assertion)
        for assertion in old_current_assertions
    }
    first_sha = next(
        document.sha256 for document in old_documents if document.id == first.json()["id"]
    )
    assert any(
        signature[0] == first_sha and signature[1] is True
        for signatures in old_provenance.values()
        for signature in signatures
    )
    frozen_old_assertions = {
        assertion.id: (
            assertion.semantic_key,
            assertion.version,
            assertion.is_current,
            assertion.review_state,
            tuple(evidence.id for evidence in assertion.evidence),
        )
        for assertion in old_current_assertions
    }
    old_document_flags = {document.id: document.superseded for document in old_documents}
    case_record.status = CaseStatus.INTERNAL_PACKET_APPROVED
    old_revision.snapshot_hash = hash_payload(revision_snapshot(db, old_revision.id))
    frozen_snapshot_hash = old_revision.snapshot_hash
    db.commit()

    reopened = _transition(
        client,
        analyst,
        case_id,
        target="evidence_review",
        expected_version=case_record.version,
        key="superseded-evidence-reopen",
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["current_revision_number"] == 2

    db.expire_all()
    old_revision = db.get(CaseRevision, old_revision.id)
    assert old_revision is not None and old_revision.snapshot_hash == frozen_snapshot_hash
    assert hash_payload(revision_snapshot(db, old_revision.id)) == frozen_snapshot_hash
    assert {
        document.id: document.superseded
        for document in db.scalars(
            select(ContractDocument).where(ContractDocument.case_revision_id == old_revision.id)
        )
    } == old_document_flags
    assert {
        assertion.id: (
            assertion.semantic_key,
            assertion.version,
            assertion.is_current,
            assertion.review_state,
            tuple(evidence.id for evidence in assertion.evidence),
        )
        for assertion in db.scalars(
            select(ContractAssertion)
            .options(selectinload(ContractAssertion.evidence))
            .where(
                ContractAssertion.id.in_(frozen_old_assertions),
            )
        )
    } == frozen_old_assertions

    new_revision = db.scalar(
        select(CaseRevision).where(
            CaseRevision.case_id == case_id,
            CaseRevision.number == 2,
        )
    )
    assert new_revision is not None and new_revision.previous_revision_id == old_revision.id
    new_documents = list(
        db.scalars(
            select(ContractDocument).where(ContractDocument.case_revision_id == new_revision.id)
        )
    )
    assert len(new_documents) == len(old_documents)
    assert {(document.sha256, document.superseded) for document in new_documents} == {
        (document.sha256, document.superseded) for document in old_documents
    }
    new_assertions = list(
        db.scalars(
            select(ContractAssertion)
            .options(selectinload(ContractAssertion.evidence))
            .where(
                ContractAssertion.case_revision_id == new_revision.id,
                ContractAssertion.is_current.is_(True),
            )
            .order_by(ContractAssertion.semantic_key)
        )
    )
    assert {assertion.semantic_key for assertion in new_assertions} == set(old_provenance)
    assert {
        assertion.semantic_key: _evidence_signatures(db, assertion) for assertion in new_assertions
    } == old_provenance
    new_document_ids = {document.id for document in new_documents}
    for assertion in new_assertions:
        for evidence in assertion.evidence:
            page = db.get(DocumentPage, evidence.page_id)
            assert page is not None and page.document_id in new_document_ids
        if assertion.extraction_run is not None:
            assert assertion.extraction_run.document_id in new_document_ids
