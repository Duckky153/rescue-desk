from typing import Any, cast

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from rescue_desk.models import AuditEvent, ExportArtifact, Role
from tests.integration.conftest import seed_identity
from tests.integration.test_export_api import _post_export, _seed_exportable_case


def test_workbench_snapshot_is_one_coherent_canonical_case_view(
    client: TestClient,
    db: Session,
) -> None:
    analyst = seed_identity(db, email="snapshot-analyst@demo.local", role=Role.ANALYST)
    case = _seed_exportable_case(client, db, analyst, key_suffix="workbench-snapshot")
    exported = _post_export(
        client,
        analyst,
        case,
        kind="machine_readable_json",
        key="snapshot-export",
    )
    assert exported.status_code == 201, exported.text

    response = client.get(
        f"/v1/cases/{case.case_id}/workbench-snapshot",
        headers=analyst.headers,
    )
    assert response.status_code == 200, response.text
    payload = cast(dict[str, Any], response.json())
    assert set(payload) == {
        "case_detail",
        "documents",
        "assertions",
        "fees",
        "calculation",
        "readiness",
        "audit_events",
        "exports",
    }

    case_detail = payload["case_detail"]
    revision_number = case_detail["current_revision_number"]
    revision_id = next(
        item["id"] for item in case_detail["revisions"] if item["number"] == revision_number
    )
    assert case_detail["status"] == "draft"
    assert {item["case_revision_id"] for item in payload["documents"]} == {revision_id}
    assert {item["case_revision_id"] for item in payload["assertions"]} == {revision_id}
    assert {item["case_revision_id"] for item in payload["fees"]} == {revision_id}
    assert payload["calculation"]["case_revision_id"] == revision_id
    assert payload["calculation"]["id"] == case.calculation_id
    assert payload["readiness"]["ready_for_internal_review"] is True
    assert payload["readiness"]["reproducible_calculation_exists"] is True
    assert payload["readiness"]["open_blocking_findings"] == 0
    assert [item["id"] for item in payload["exports"]] == [exported.json()["id"]]
    assert payload["exports"][0]["case_revision_id"] == revision_id
    assert payload["exports"][0]["calculation_id"] == case.calculation_id
    assert any(
        event["action"] == "export.created" and event["object_id"] == exported.json()["id"]
        for event in payload["audit_events"]
    )


def test_workbench_snapshot_is_authenticated_and_tenant_isolated(
    client: TestClient,
    db: Session,
) -> None:
    owner = seed_identity(db, email="snapshot-owner@demo.local", role=Role.ANALYST)
    outsider = seed_identity(
        db,
        email="snapshot-outsider@demo.local",
        role=Role.AUDITOR,
        organization_name="Snapshot outsider organization",
    )
    case = _seed_exportable_case(client, db, owner, key_suffix="snapshot-tenant")
    path = f"/v1/cases/{case.case_id}/workbench-snapshot"

    assert client.get(path).status_code == 401
    hidden = client.get(path, headers=outsider.headers)
    assert hidden.status_code == 404
    assert hidden.json() == {"detail": "Case not found"}


def test_zero_export_workbench_snapshot_rejects_tampered_audit_chain(
    client: TestClient,
    db: Session,
) -> None:
    analyst = seed_identity(db, email="snapshot-tamper@demo.local", role=Role.ANALYST)
    created = client.post(
        "/v1/cases",
        headers={**analyst.headers, "Idempotency-Key": "snapshot-tamper-case"},
        json={
            "display_name": "Zero-export audit integrity",
            "applicant_company": "Northstar Synthetic",
            "erp_provider": "LegacySuite Synthetic",
        },
    )
    assert created.status_code == 201, created.text
    assert db.query(ExportArtifact).count() == 0
    event = db.query(AuditEvent).filter_by(action="case.created").one()
    event.after = {"tampered": True}
    db.commit()

    response = client.get(
        f"/v1/cases/{created.json()['id']}/workbench-snapshot",
        headers=analyst.headers,
    )

    assert response.status_code == 409, response.text
    assert response.json() == {"detail": "Case audit chain failed its integrity check"}
    assert db.query(ExportArtifact).count() == 0
