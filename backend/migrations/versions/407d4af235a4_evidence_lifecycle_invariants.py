"""Evidence lifecycle invariants and fee recovery metadata.

Revision ID: 407d4af235a4
Revises: 22891c4578b0
Create Date: 2026-08-21 12:58:42.594014
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import RowMapping

revision: str = "407d4af235a4"
down_revision: str | None = "22891c4578b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MAX_MINOR_UNITS = 9_000_000_000_000_000
SUPPORTED_CURRENCIES = (
    "AUD",
    "BHD",
    "CAD",
    "CHF",
    "CLP",
    "EUR",
    "GBP",
    "INR",
    "JOD",
    "JPY",
    "KRW",
    "KWD",
    "OMR",
    "TND",
    "USD",
    "VND",
)
CURRENCIES_SQL = ", ".join(f"'{value}'" for value in SUPPORTED_CURRENCIES)


def _assert_no_duplicate_current_rows() -> None:
    bind = op.get_bind()
    duplicate_assertions = bind.execute(
        sa.text(
            "SELECT case_revision_id, semantic_key FROM contract_assertions "
            "WHERE is_current IS TRUE GROUP BY case_revision_id, semantic_key "
            "HAVING COUNT(*) > 1"
        )
    ).first()
    if duplicate_assertions is not None:
        raise RuntimeError(
            "Cannot migrate: duplicate current assertions exist for one revision/semantic key"
        )
    duplicate_exports = bind.execute(
        sa.text(
            "SELECT case_revision_id, kind FROM export_artifacts "
            "WHERE superseded IS FALSE GROUP BY case_revision_id, kind HAVING COUNT(*) > 1"
        )
    ).first()
    if duplicate_exports is not None:
        raise RuntimeError(
            "Cannot migrate: duplicate unsuperseded exports exist for one revision/kind"
        )


def _backfill_reviewed_fee_evidence() -> None:
    """Bind legacy reviewed fees to their one exact typed money assertion."""

    bind = op.get_bind()
    metadata = sa.MetaData()
    fees = sa.Table("fee_obligations", metadata, autoload_with=bind)
    assertions = sa.Table("contract_assertions", metadata, autoload_with=bind)
    evidence_links = sa.Table("assertion_evidence_links", metadata, autoload_with=bind)
    evidence_spans = sa.Table("evidence_spans", metadata, autoload_with=bind)
    document_pages = sa.Table("document_pages", metadata, autoload_with=bind)
    documents = sa.Table("contract_documents", metadata, autoload_with=bind)
    decisions = sa.Table("review_decisions", metadata, autoload_with=bind)
    category_keys = {
        "SUBSCRIPTION": "fee.subscription",
        "IMPLEMENTATION": "fee.implementation",
        "TERMINATION": "fee.termination",
    }

    def assertion_row(assertion_id: str) -> RowMapping | None:
        return (
            bind.execute(sa.select(assertions).where(assertions.c.id == assertion_id))
            .mappings()
            .one_or_none()
        )

    def latest_decision(assertion_id: str) -> RowMapping | None:
        return (
            bind.execute(
                sa.select(decisions)
                .where(decisions.c.assertion_id == assertion_id)
                .order_by(decisions.c.created_at.desc(), decisions.c.id.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )

    def governance_is_valid(candidate: RowMapping) -> bool:
        candidate_id = str(candidate["id"])
        if candidate["review_state"] == "ACCEPTED":
            # Current runtime prohibits ACCEPT from laundering a corrected lineage.
            if candidate["supersedes_assertion_id"] is not None:
                return False
            decision = latest_decision(candidate_id)
            return bool(
                decision is not None
                and decision["decision"] == "ACCEPT"
                and decision["corrected_value"] is None
                and decision["assumption"] is False
            )
        if candidate["review_state"] != "CORRECTED":
            return False

        edges: list[tuple[RowMapping, RowMapping, RowMapping]] = []
        child = candidate
        visited = {candidate_id}
        while child["supersedes_assertion_id"] is not None:
            parent_id = str(child["supersedes_assertion_id"])
            if parent_id in visited:
                return False
            visited.add(parent_id)
            parent = assertion_row(parent_id)
            decision = latest_decision(parent_id)
            if parent is None or decision is None:
                return False
            if parent["case_revision_id"] != candidate["case_revision_id"]:
                return False
            edges.append((parent, child, decision))
            child = parent
        if not edges:
            return False
        inherited_assumption = False
        for parent, lineage_child, decision in reversed(edges):
            changed = parent["normalized_value"] != lineage_child["normalized_value"]
            requires_assumption = changed or inherited_assumption
            if (
                decision["decision"] != "CORRECT"
                or decision["corrected_value"] != lineage_child["normalized_value"]
                or (requires_assumption and decision["assumption"] is not True)
            ):
                return False
            inherited_assumption = requires_assumption or decision["assumption"] is True
        return True

    def money_value_matches(
        value: object,
        *,
        semantic_key: str,
        fee: RowMapping,
    ) -> bool:
        if not isinstance(value, dict) or set(value) != {
            "type",
            "amount_minor",
            "currency",
            "cadence",
        }:
            return False
        amount = value.get("amount_minor")
        currency = value.get("currency")
        cadence = value.get("cadence")
        allowed_cadences = (
            {"annual", "monthly"} if semantic_key == "fee.subscription" else {"one_time"}
        )
        return bool(
            value.get("type") == "money"
            and isinstance(amount, int)
            and not isinstance(amount, bool)
            and 0 <= amount <= MAX_MINOR_UNITS
            and amount == fee["amount_minor"]
            and isinstance(currency, str)
            and currency in SUPPORTED_CURRENCIES
            and currency == fee["currency"]
            and cadence in allowed_cadences
            and cadence == fee["billing_cadence"]
        )

    used: set[tuple[str, str]] = set()
    reviewed_rows = bind.execute(sa.select(fees).where(fees.c.reviewed.is_(True))).mappings()
    for fee in reviewed_rows:
        raw_ids = fee["assertion_ids"]
        assertion_ids = raw_ids if isinstance(raw_ids, list) else []
        semantic_key = category_keys.get(str(fee["category"]))
        if semantic_key is None or not assertion_ids:
            raise RuntimeError(
                f"Cannot migrate reviewed fee {fee['id']}: no supported money evidence"
            )
        candidates = bind.execute(
            sa.select(
                assertions.c.id,
                assertions.c.semantic_key,
                assertions.c.normalized_value,
                assertions.c.is_current,
                assertions.c.case_revision_id,
                assertions.c.review_state,
                assertions.c.supersedes_assertion_id,
            ).where(assertions.c.id.in_(assertion_ids))
        ).mappings()
        matches: list[str] = []
        for assertion in candidates:
            value: Any = assertion["normalized_value"]
            if (
                assertion["is_current"] is True
                and assertion["case_revision_id"] == fee["case_revision_id"]
                and assertion["review_state"] in {"ACCEPTED", "CORRECTED"}
                and assertion["semantic_key"] == semantic_key
                and money_value_matches(value, semantic_key=semantic_key, fee=fee)
            ):
                assertion_id = str(assertion["id"])
                has_evidence = bind.scalar(
                    sa.select(sa.func.count())
                    .select_from(
                        evidence_links.join(
                            evidence_spans,
                            evidence_spans.c.id == evidence_links.c.evidence_id,
                        )
                        .join(
                            document_pages,
                            document_pages.c.id == evidence_spans.c.page_id,
                        )
                        .join(
                            documents,
                            documents.c.id == document_pages.c.document_id,
                        )
                    )
                    .where(
                        evidence_links.c.assertion_id == assertion_id,
                        documents.c.case_revision_id == fee["case_revision_id"],
                        documents.c.superseded.is_(False),
                    )
                )
                if not has_evidence:
                    continue
                if governance_is_valid(assertion):
                    matches.append(assertion_id)
        if len(matches) != 1:
            raise RuntimeError(
                f"Cannot migrate reviewed fee {fee['id']}: expected one exact money assertion"
            )
        identity = (str(fee["case_revision_id"]), matches[0])
        if identity in used:
            raise RuntimeError(
                "Cannot migrate: one money assertion substantiates multiple active reviewed fees"
            )
        used.add(identity)
        bind.execute(
            fees.update()
            .where(fees.c.id == fee["id"])
            .values(primary_money_assertion_id=matches[0])
        )


def _assert_active_fee_aggregates_supported() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    fees = sa.Table("fee_obligations", metadata, autoload_with=bind)
    overflowing = bind.execute(
        sa.select(
            fees.c.case_revision_id,
            fees.c.currency,
            sa.func.sum(fees.c.amount_minor).label("total_minor"),
        )
        .group_by(fees.c.case_revision_id, fees.c.currency)
        .having(sa.func.sum(fees.c.amount_minor) > MAX_MINOR_UNITS)
    ).first()
    if overflowing is not None:
        raise RuntimeError(
            "Cannot migrate: active fee aggregate exceeds the exact supported minor-unit range"
        )


def _checks() -> dict[str, tuple[tuple[str, str], ...]]:
    return {
        "audit_events": (
            (
                "ck_audit_correlation_id_length",
                "length(correlation_id) >= 1 AND length(correlation_id) <= 64",
            ),
            ("ck_audit_event_hash_length", "length(event_hash) = 64"),
            (
                "ck_audit_previous_hash_length",
                "previous_hash IS NULL OR length(previous_hash) = 64",
            ),
        ),
        "calculation_line_items": (
            ("ck_calculation_line_currency_supported", f"currency IN ({CURRENCIES_SQL})"),
            (
                "ck_calculation_line_original_range",
                f"original_amount_minor >= 0 AND original_amount_minor <= {MAX_MINOR_UNITS}",
            ),
            (
                "ck_calculation_line_remaining_range",
                "remaining_amount_minor IS NULL OR "
                f"(remaining_amount_minor >= 0 AND remaining_amount_minor <= {MAX_MINOR_UNITS})",
            ),
        ),
        "calculation_runs": (
            ("ck_calculation_input_hash_length", "length(input_hash) = 64"),
            ("ck_calculation_result_hash_length", "length(result_hash) = 64"),
        ),
        "case_revisions": (
            ("ck_case_revision_number_positive", "number >= 1"),
            (
                "ck_case_revision_snapshot_hash_length",
                "snapshot_hash IS NULL OR length(snapshot_hash) = 64",
            ),
        ),
        "contract_assertions": (
            ("ck_contract_assertion_confidence_range", "confidence >= 0 AND confidence <= 1"),
            ("ck_contract_assertion_version_positive", "version >= 1"),
        ),
        "contract_documents": (
            ("ck_contract_document_pages_positive", "page_count >= 1"),
            ("ck_contract_document_sha_length", "length(sha256) = 64"),
            ("ck_contract_document_size_positive", "size_bytes > 0"),
            (
                "ck_contract_document_source_type",
                "source_type IN ('synthetic', 'public', 'redacted')",
            ),
        ),
        "document_pages": (
            (
                "ck_document_page_confidence_range",
                "extraction_confidence >= 0 AND extraction_confidence <= 1",
            ),
            ("ck_document_page_number_positive", "page_number >= 1"),
            ("ck_document_page_text_hash_length", "length(text_sha256) = 64"),
        ),
        "evidence_spans": (
            ("ck_evidence_span_order", "char_end > char_start"),
            ("ck_evidence_span_quote_hash_length", "length(quote_sha256) = 64"),
            ("ck_evidence_span_start_nonnegative", "char_start >= 0"),
        ),
        "export_artifacts": (
            (
                "ck_export_approval_consistency",
                "(approval_status = 'approved' AND approval_case_version >= 1 "
                "AND approved_by IS NOT NULL) OR (approval_status = 'not_approved' "
                "AND approval_case_version IS NULL AND approved_by IS NULL)",
            ),
            (
                "ck_export_approval_status_supported",
                "approval_status IN ('not_approved', 'approved')",
            ),
            ("ck_export_artifact_sha_length", "length(sha256) = 64"),
            ("ck_export_packet_snapshot_hash_length", "length(packet_snapshot_sha256) = 64"),
            ("ck_export_revision_snapshot_hash_length", "length(revision_snapshot_sha256) = 64"),
        ),
        "extraction_runs": (
            (
                "ck_extraction_run_output_hash_length",
                "structured_output_hash IS NULL OR length(structured_output_hash) = 64",
            ),
            (
                "ck_extraction_run_prompt_hash_length",
                "prompt_hash IS NULL OR length(prompt_hash) = 64",
            ),
            (
                "ck_extraction_run_status_lifecycle",
                "(status = 'RUNNING' AND finished_at IS NULL) OR "
                "(status = 'SUCCEEDED' AND finished_at IS NOT NULL "
                "AND structured_output_hash IS NOT NULL AND error_category IS NULL) OR "
                "(status = 'FAILED' AND finished_at IS NOT NULL "
                "AND error_category IS NOT NULL)",
            ),
        ),
        "fee_obligations": (
            (
                "ck_fee_amount_supported_range",
                f"amount_minor >= 0 AND amount_minor <= {MAX_MINOR_UNITS}",
            ),
            ("ck_fee_currency_supported", f"currency IN ({CURRENCIES_SQL})"),
            ("ck_fee_payment_status_supported", "payment_status IN ('unknown', 'unpaid', 'paid')"),
            (
                "ck_fee_billing_cadence_supported",
                "billing_cadence IS NULL OR billing_cadence IN ('annual', 'monthly', 'one_time')",
            ),
            (
                "ck_fee_reviewed_cadence_required",
                "reviewed IS FALSE OR billing_cadence IS NOT NULL",
            ),
            (
                "ck_fee_reviewed_money_evidence",
                "(reviewed IS TRUE AND primary_money_assertion_id IS NOT NULL) OR "
                "(reviewed IS FALSE AND primary_money_assertion_id IS NULL)",
            ),
            (
                "ck_fee_service_date_order",
                "service_start IS NULL OR service_end IS NULL OR service_end > service_start",
            ),
            (
                "ck_fee_supersede_lifecycle",
                "(superseded IS TRUE AND superseded_at IS NOT NULL "
                "AND superseded_by IS NOT NULL AND supersede_reason IS NOT NULL) OR "
                "(superseded IS FALSE AND superseded_at IS NULL "
                "AND superseded_by IS NULL AND supersede_reason IS NULL)",
            ),
        ),
        "idempotency_records": (
            ("ck_idempotency_request_hash_length", "length(request_hash) = 64"),
            (
                "ck_idempotency_response_status",
                "response_status = 0 OR (response_status >= 100 AND response_status <= 599)",
            ),
        ),
        "readiness_findings": (
            (
                "ck_readiness_finding_resolution_lifecycle",
                "(status = 'OPEN' AND resolved_at IS NULL AND resolution_reason IS NULL) OR "
                "(status IN ('RESOLVED', 'ACCEPTED_RISK') AND resolved_at IS NOT NULL "
                "AND resolution_reason IS NOT NULL)",
            ),
            ("ck_readiness_finding_version_positive", "version >= 1"),
        ),
        "rescue_cases": (
            ("ck_rescue_case_revision_positive", "current_revision_number >= 1"),
            ("ck_rescue_case_version_positive", "version >= 1"),
        ),
        "review_decisions": (
            ("ck_review_decision_assumption_type", "assumption IS FALSE OR decision = 'CORRECT'"),
            (
                "ck_review_decision_correction_value",
                "(decision = 'CORRECT' AND corrected_value IS NOT NULL) OR "
                "(decision IN ('ACCEPT', 'REJECT') AND corrected_value IS NULL)",
            ),
        ),
    }


def _create_checks() -> None:
    for table_name, table_checks in _checks().items():
        with op.batch_alter_table(table_name) as batch:
            for name, condition in table_checks:
                batch.create_check_constraint(name, condition)


def upgrade() -> None:
    _assert_no_duplicate_current_rows()
    _assert_active_fee_aggregates_supported()
    with op.batch_alter_table("fee_obligations") as batch:
        batch.add_column(sa.Column("primary_money_assertion_id", sa.String(36), nullable=True))
        batch.add_column(
            sa.Column("superseded", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("superseded_by", sa.String(36), nullable=True))
        batch.add_column(sa.Column("supersede_reason", sa.Text(), nullable=True))
        batch.create_foreign_key(
            "fk_fee_primary_money_assertion",
            "contract_assertions",
            ["primary_money_assertion_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_fee_superseded_by_user", "users", ["superseded_by"], ["id"], ondelete="RESTRICT"
        )
    _backfill_reviewed_fee_evidence()
    # The new calculation/extraction/export contracts are versioned and stricter.
    # Preserve legacy packet rows for audit/listing while preventing them from
    # presenting as current after upgrade.
    op.execute(sa.text("UPDATE export_artifacts SET superseded = TRUE"))
    # SQLAlchemy's legacy JSON column serialized Python None as JSON `null`.
    # Normalize those historical ACCEPT/REJECT values to SQL NULL before the
    # lifecycle CHECK is installed; the model now uses JSON(none_as_null=True).
    op.execute(
        sa.text(
            "UPDATE review_decisions SET corrected_value = NULL "
            "WHERE CAST(corrected_value AS TEXT) = 'null'"
        )
    )
    _create_checks()
    op.create_index(
        "uq_contract_assertion_current_semantic",
        "contract_assertions",
        ["case_revision_id", "semantic_key"],
        unique=True,
        postgresql_where=sa.text("is_current"),
        sqlite_where=sa.text("is_current = 1"),
    )
    op.create_index(
        "uq_export_current_revision_kind",
        "export_artifacts",
        ["case_revision_id", "kind"],
        unique=True,
        postgresql_where=sa.text("superseded IS FALSE"),
        sqlite_where=sa.text("superseded = 0"),
    )
    op.create_index(
        "uq_active_fee_money_assertion",
        "fee_obligations",
        ["case_revision_id", "primary_money_assertion_id"],
        unique=True,
        postgresql_where=sa.text("superseded IS FALSE AND primary_money_assertion_id IS NOT NULL"),
        sqlite_where=sa.text("superseded = 0 AND primary_money_assertion_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_active_fee_money_assertion", table_name="fee_obligations")
    op.drop_index("uq_export_current_revision_kind", table_name="export_artifacts")
    op.drop_index("uq_contract_assertion_current_semantic", table_name="contract_assertions")
    for table_name, table_checks in reversed(tuple(_checks().items())):
        with op.batch_alter_table(table_name) as batch:
            for name, _ in reversed(table_checks):
                batch.drop_constraint(name, type_="check")
    with op.batch_alter_table("fee_obligations") as batch:
        batch.drop_constraint("fk_fee_superseded_by_user", type_="foreignkey")
        batch.drop_constraint("fk_fee_primary_money_assertion", type_="foreignkey")
        batch.drop_column("supersede_reason")
        batch.drop_column("superseded_by")
        batch.drop_column("superseded_at")
        batch.drop_column("superseded")
        batch.drop_column("primary_money_assertion_id")
