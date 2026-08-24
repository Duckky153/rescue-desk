from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

from rescue_desk.config import get_settings
from rescue_desk.domain.money import MAX_MINOR_UNITS

BASE_REVISION = "22891c4578b0"


@pytest.fixture(autouse=True)
def _clear_cached_settings_after_migration_test() -> object:
    yield
    get_settings.cache_clear()


def _config(path: Path) -> Config:
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).parents[2] / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _seed_core(connection: sa.Connection) -> None:
    timestamp = "2026-08-21 12:00:00"
    connection.execute(
        sa.text(
            "INSERT INTO organizations (id, name, created_at) "
            "VALUES ('org-1', 'Legacy migration organization', :created_at)"
        ),
        {"created_at": timestamp},
    )
    connection.execute(
        sa.text(
            "INSERT INTO users "
            "(id, email, password_hash, display_name, is_active, created_at) "
            "VALUES ('user-1', 'legacy@example.com', 'hash', 'Legacy Reviewer', 1, :created_at)"
        ),
        {"created_at": timestamp},
    )
    connection.execute(
        sa.text(
            "INSERT INTO rescue_cases "
            "(id, organization_id, display_name, applicant_company, erp_provider, "
            "assigned_analyst_id, assigned_approver_id, status, current_revision_number, "
            "version, created_at, updated_at) VALUES "
            "('case-1', 'org-1', 'Legacy case', 'Applicant', 'ERP', 'user-1', NULL, "
            "'DRAFT', 1, 1, :created_at, :created_at)"
        ),
        {"created_at": timestamp},
    )
    for revision_id, number in (("revision-1", 1), ("revision-2", 2)):
        connection.execute(
            sa.text(
                "INSERT INTO case_revisions "
                "(id, case_id, number, reason, previous_revision_id, snapshot_hash, "
                "created_by, created_at) VALUES "
                "(:id, 'case-1', :number, 'Legacy revision', NULL, NULL, 'user-1', :created_at)"
            ),
            {"id": revision_id, "number": number, "created_at": timestamp},
        )


def _seed_reviewed_fee(
    connection: sa.Connection,
    *,
    document_revision_id: str = "revision-1",
    document_superseded: bool = False,
    billing_cadence: str | None = "annual",
) -> None:
    timestamp = "2026-08-21 12:00:00"
    quote = "Annual subscription fee is USD 12,000."
    digest = hashlib.sha256(quote.encode()).hexdigest()
    connection.execute(
        sa.text(
            "INSERT INTO contract_documents "
            "(id, organization_id, case_revision_id, original_filename, stored_filename, sha256, "
            "media_type, source_type, size_bytes, page_count, safety_status, processing_status, "
            "processing_error, superseded, created_by, created_at) VALUES "
            "('document-1', 'org-1', :revision_id, 'legacy.pdf', 'legacy.pdf', :sha256, "
            "'application/pdf', 'synthetic', :size_bytes, 1, 'PASSED', 'PROCESSED', NULL, "
            ":superseded, 'user-1', :created_at)"
        ),
        {
            "revision_id": document_revision_id,
            "sha256": digest,
            "size_bytes": len(quote.encode()),
            "superseded": document_superseded,
            "created_at": timestamp,
        },
    )
    connection.execute(
        sa.text(
            "INSERT INTO document_pages "
            "(id, document_id, page_number, text, text_sha256, extraction_confidence) "
            "VALUES ('page-1', 'document-1', 1, :quote, :sha256, 1)"
        ),
        {"quote": quote, "sha256": digest},
    )
    connection.execute(
        sa.text(
            "INSERT INTO evidence_spans "
            "(id, page_id, quote, char_start, char_end, quote_sha256) "
            "VALUES ('evidence-1', 'page-1', :quote, 0, :char_end, :sha256)"
        ),
        {"quote": quote, "char_end": len(quote), "sha256": digest},
    )
    connection.execute(
        sa.text(
            "INSERT INTO contract_assertions "
            "(id, case_revision_id, extraction_run_id, semantic_key, raw_value, normalized_value, "
            "display_value, source, confidence, review_state, version, is_current, "
            "supersedes_assertion_id, created_at) VALUES "
            "('assertion-1', 'revision-1', NULL, 'fee.subscription', :raw_value, "
            ":normalized_value, 'USD 12,000.00', 'HUMAN', 1, 'ACCEPTED', 1, 1, NULL, "
            ":created_at)"
        ),
        {
            "raw_value": quote,
            "normalized_value": json.dumps(
                {
                    "type": "money",
                    "amount_minor": 1_200_000,
                    "currency": "USD",
                    "cadence": "annual",
                }
            ),
            "created_at": timestamp,
        },
    )
    connection.execute(
        sa.text(
            "INSERT INTO assertion_evidence_links (assertion_id, evidence_id) "
            "VALUES ('assertion-1', 'evidence-1')"
        )
    )
    # This is the legacy SQLAlchemy JSON(None) representation that must become SQL NULL.
    connection.execute(
        sa.text(
            "INSERT INTO review_decisions "
            "(id, assertion_id, decision, reviewer_id, reason, corrected_value, assumption, "
            "created_at) VALUES "
            "('decision-1', 'assertion-1', 'ACCEPT', 'user-1', 'Verified source', 'null', 0, "
            ":created_at)"
        ),
        {"created_at": timestamp},
    )
    connection.execute(
        sa.text(
            "INSERT INTO fee_obligations "
            "(id, case_revision_id, category, amount_minor, currency, service_start, service_end, "
            "obligation_date, payment_status, billing_cadence, proration_rule, assertion_ids, "
            "reviewed, created_at) VALUES "
            "('fee-1', 'revision-1', 'SUBSCRIPTION', 1200000, 'USD', '2026-01-01', "
            "'2027-01-01', NULL, 'unpaid', :cadence, 'contract_daily', :assertion_ids, 1, "
            ":created_at)"
        ),
        {
            "cadence": billing_cadence,
            "assertion_ids": json.dumps(["assertion-1"]),
            "created_at": timestamp,
        },
    )


def _legacy_database(
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> tuple[Config, sa.Engine]:
    monkeypatch.setenv("RESCUEDESK_DATABASE_URL", f"sqlite:///{path}")
    get_settings.cache_clear()
    config = _config(path)
    command.upgrade(config, BASE_REVISION)
    engine = sa.create_engine(f"sqlite:///{path}")
    request.addfinalizer(engine.dispose)
    return config, engine


def _seed_legacy_export(connection: sa.Connection) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO export_artifacts "
            "(id, case_revision_id, calculation_id, kind, stored_filename, sha256, "
            "packet_snapshot_sha256, revision_snapshot_sha256, approval_status, "
            "approval_case_version, approved_by, prepared_at, prepared_by, generator_version, "
            "superseded, created_by, created_at) VALUES "
            "('legacy-export-1', 'revision-1', NULL, 'MACHINE_READABLE_JSON', "
            "'legacy-export.json', :sha256, :packet_hash, :revision_hash, 'not_approved', "
            "NULL, NULL, '2026-08-21 12:00:00', 'user-1', 'rescuedesk-export-v1', 0, "
            "'user-1', '2026-08-21 12:00:00')"
        ),
        {"sha256": "a" * 64, "packet_hash": "b" * 64, "revision_hash": "c" * 64},
    )


def test_populated_legacy_reviewed_fee_roundtrips_to_strict_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    config, engine = _legacy_database(tmp_path / "valid-legacy.db", monkeypatch, request)
    with engine.begin() as connection:
        _seed_core(connection)
        _seed_reviewed_fee(connection)
        _seed_legacy_export(connection)

    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text("SELECT primary_money_assertion_id FROM fee_obligations WHERE id='fee-1'")
            )
            == "assertion-1"
        )
        assert (
            connection.scalar(
                sa.text(
                    "SELECT corrected_value IS NULL FROM review_decisions WHERE id='decision-1'"
                )
            )
            == 1
        )
        assert (
            connection.scalar(
                sa.text("SELECT superseded FROM export_artifacts WHERE id='legacy-export-1'")
            )
            == 1
        )

    command.downgrade(config, BASE_REVISION)
    command.upgrade(config, "head")

    with engine.connect() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO review_decisions "
                "(id, assertion_id, decision, reviewer_id, reason, corrected_value, assumption, "
                "created_at) VALUES "
                "('valid-reject', 'assertion-1', 'REJECT', 'user-1', 'Rejected', NULL, 0, "
                "'2026-08-21 13:00:00')"
            )
        )
        connection.commit()
        with pytest.raises(IntegrityError):
            connection.execute(
                sa.text(
                    "INSERT INTO review_decisions "
                    "(id, assertion_id, decision, reviewer_id, reason, corrected_value, "
                    "assumption, created_at) VALUES "
                    "('invalid-accept', 'assertion-1', 'ACCEPT', 'user-1', 'Invalid', '{}', 0, "
                    "'2026-08-21 14:00:00')"
                )
            )
        connection.rollback()
        with pytest.raises(IntegrityError):
            connection.execute(
                sa.text(
                    "INSERT INTO review_decisions "
                    "(id, assertion_id, decision, reviewer_id, reason, corrected_value, "
                    "assumption, created_at) VALUES "
                    "('invalid-correct', 'assertion-1', 'CORRECT', 'user-1', 'Invalid', NULL, 0, "
                    "'2026-08-21 15:00:00')"
                )
            )
        connection.rollback()


@pytest.mark.parametrize(
    ("document_revision_id", "document_superseded"),
    [("revision-2", False), ("revision-1", True)],
    ids=["cross-revision-evidence", "superseded-evidence"],
)
def test_migration_rejects_reviewed_fee_without_active_same_revision_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    document_revision_id: str,
    document_superseded: bool,
) -> None:
    config, engine = _legacy_database(
        tmp_path / f"invalid-evidence-{document_revision_id}-{document_superseded}.db",
        monkeypatch,
        request,
    )
    with engine.begin() as connection:
        _seed_core(connection)
        _seed_reviewed_fee(
            connection,
            document_revision_id=document_revision_id,
            document_superseded=document_superseded,
        )

    with pytest.raises(RuntimeError, match="expected one exact money assertion"):
        command.upgrade(config, "head")


def test_migration_rejects_reviewed_fee_without_exact_cadence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    config, engine = _legacy_database(tmp_path / "missing-cadence.db", monkeypatch, request)
    with engine.begin() as connection:
        _seed_core(connection)
        _seed_reviewed_fee(connection, billing_cadence=None)

    with pytest.raises(RuntimeError, match="expected one exact money assertion"):
        command.upgrade(config, "head")


def test_migration_rejects_noncanonical_legacy_money_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    config, engine = _legacy_database(tmp_path / "noncanonical-money.db", monkeypatch, request)
    with engine.begin() as connection:
        _seed_core(connection)
        _seed_reviewed_fee(connection)
        connection.execute(
            sa.text(
                "UPDATE contract_assertions SET normalized_value=:value WHERE id='assertion-1'"
            ),
            {
                "value": json.dumps(
                    {
                        "type": "money",
                        "amount_minor": 1_200_000,
                        "currency": "USD",
                        "cadence": "annual",
                        "untrusted": True,
                    }
                )
            },
        )

    with pytest.raises(RuntimeError, match="expected one exact money assertion"):
        command.upgrade(config, "head")


def test_migration_rejects_legacy_active_aggregate_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    config, engine = _legacy_database(tmp_path / "aggregate-overflow.db", monkeypatch, request)
    with engine.begin() as connection:
        _seed_core(connection)
        timestamp = "2026-08-21 12:00:00"
        for fee_id, amount in (("fee-max", MAX_MINOR_UNITS), ("fee-one", 1)):
            connection.execute(
                sa.text(
                    "INSERT INTO fee_obligations "
                    "(id, case_revision_id, category, amount_minor, currency, service_start, "
                    "service_end, obligation_date, payment_status, billing_cadence, "
                    "proration_rule, assertion_ids, reviewed, created_at) VALUES "
                    "(:id, 'revision-1', 'OTHER', :amount, 'USD', NULL, NULL, NULL, 'paid', "
                    "NULL, NULL, '[]', 0, :created_at)"
                ),
                {"id": fee_id, "amount": amount, "created_at": timestamp},
            )

    with pytest.raises(RuntimeError, match="aggregate exceeds"):
        command.upgrade(config, "head")
