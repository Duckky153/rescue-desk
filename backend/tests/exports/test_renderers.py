import csv
import hashlib
import io
import json
from dataclasses import replace
from datetime import timedelta, timezone

import pymupdf as fitz
import pytest

from rescue_desk.demo import DEMO_COMPLETED_CASE_NAME
from rescue_desk.exports import (
    DISCLAIMER,
    ApprovalStatus,
    ExportKind,
    ReviewState,
    build_customer_explanation_pdf,
    build_evidence_csv,
    build_internal_review_pdf,
    build_machine_json,
    content_type_for,
    extension_for,
)

from ._fixtures import sample_packet


def _pdf_text(value: bytes) -> str:
    with fitz.open(stream=value, filetype="pdf") as document:
        return "\n".join(page.get_text() for page in document)


def test_internal_pdf_is_deterministic_parseable_and_complete() -> None:
    packet = sample_packet()

    first = build_internal_review_pdf(packet)
    second = build_internal_review_pdf(packet)
    text = _pdf_text(first)

    assert first == second
    assert first.startswith(b"%PDF-")
    assert DISCLAIMER in text.replace("\n", " ")
    assert packet.snapshot_hash in text.replace("\n", "")
    assert "contract-daily-half-open-v1" in text
    assert "Termination clause needs human review" in text
    assert "SUPERSEDED SOURCE" in text
    assert "EV-001" in text
    assert "evidence_review / not_approved" in text
    assert "Approver: None recorded" in text
    assert packet.calculation_result_hash in text.replace("\n", "")
    assert "<br/>" not in text
    with fitz.open(stream=first, filetype="pdf") as document:
        assert document.page_count == 2


def test_equivalent_timezone_representation_keeps_hash_and_pdf_identical() -> None:
    packet = sample_packet()
    same_instant = replace(
        packet,
        prepared_at=packet.prepared_at.astimezone(timezone(-timedelta(hours=4))),
    )

    assert same_instant.snapshot_hash == packet.snapshot_hash
    assert build_internal_review_pdf(same_instant) == build_internal_review_pdf(packet)


def test_customer_pdf_is_plain_language_deterministic_and_traceable() -> None:
    packet = sample_packet()

    first = build_customer_explanation_pdf(packet)
    second = build_customer_explanation_pdf(packet)
    text = _pdf_text(first)

    assert first == second
    assert "Switching evidence brief" in text
    assert "does not determine" in text
    assert "Formula:" in text
    assert "Sources: EV-001" in text
    assert "SUPERSEDED - DO NOT RELY" in text
    assert "explicit operator assumption" in text.replace("\n", " ")
    assert "Demo Reviewer" not in text
    assert packet.snapshot_hash in text.replace("\n", "")
    assert DISCLAIMER in text.replace("\n", " ")
    assert "<br/>" not in text
    with fitz.open(stream=first, filetype="pdf") as document:
        assert document.page_count == 2


def test_evidence_csv_is_traceable_and_formula_safe() -> None:
    packet = sample_packet()
    result = build_evidence_csv(packet)
    rows = list(csv.DictReader(io.StringIO(result.decode("utf-8"))))

    assert len(rows) == 2
    assert {row["snapshot_sha256"] for row in rows} == {packet.snapshot_hash}
    assert {row["disclaimer"] for row in rows} == {DISCLAIMER}
    superseded = next(row for row in rows if row["citation_id"] == "EV-OLD")
    assert superseded["source_status"] == "superseded"
    assert superseded["document_name"].startswith("'=")
    assert superseded["quote"].startswith("'=")
    assert "explicit_assumption" in superseded["fact_provenance"]
    assert "FACT-OLD-FEE" in superseded["referenced_by"]
    current = next(row for row in rows if row["citation_id"] == "EV-001")
    assert current["formula_ids"] == "contract-daily-half-open-v1"
    assert "original_minor" in current["formula_detail"]
    assert "BLOCK-TERMINATION:open" in current["blocker_context"]

    risky_prefixes = ("=", "+", "-", "@", "\t", "\r")
    assert not any(
        cell.startswith(risky_prefixes) for row in rows for cell in row.values() if cell is not None
    )


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r", "\n", "  ="])
def test_evidence_csv_escapes_every_risky_spreadsheet_prefix(prefix: str) -> None:
    packet = sample_packet()
    changed_citation = replace(packet.citations[0], document_name=f"{prefix}payload")
    packet = replace(packet, citations=(changed_citation, packet.citations[1]))

    rows = list(csv.DictReader(io.StringIO(build_evidence_csv(packet).decode("utf-8"))))
    current = next(row for row in rows if row["citation_id"] == "EV-001")

    assert current["document_name"].startswith("'")


def test_machine_json_is_canonical_complete_and_repeatable() -> None:
    packet = sample_packet()

    first = build_machine_json(packet)
    second = build_machine_json(packet)
    payload = json.loads(first)
    canonical_snapshot = json.dumps(
        payload["snapshot"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    assert first == second
    assert first.endswith(b"\n")
    assert payload["snapshot_sha256"] == packet.snapshot_hash
    assert payload["disclaimer"] == DISCLAIMER
    assert payload["snapshot"]["case"]["revision_id"] == "REV-003"
    assert payload["snapshot"]["citations"][1]["superseded"] is True
    assert payload["snapshot"]["calculation"]["lines"][0]["formula"]
    assert payload["snapshot"]["blockers"][0]["status"] == "open"
    assert payload["integrity"]["calculation_result_sha256"] == (packet.calculation_result_hash)
    assert hashlib.sha256(canonical_snapshot).hexdigest() == packet.snapshot_hash


def test_superseded_packet_is_conspicuous_in_both_pdfs() -> None:
    packet = replace(sample_packet(), packet_superseded=True)

    internal_text = _pdf_text(build_internal_review_pdf(packet))
    customer_text = _pdf_text(build_customer_explanation_pdf(packet))
    assert "SUPERSEDED SNAPSHOT - DO NOT RELY" in internal_text
    assert "SUPERSEDED SNAPSHOT - DO NOT RELY" in customer_text


def test_seeded_completed_packet_discloses_synthetic_approver_role_history() -> None:
    packet = replace(
        sample_packet(),
        case_name=DEMO_COMPLETED_CASE_NAME,
        approval_status=ApprovalStatus.APPROVED,
        approved_by="Seeded Demo Approver",
        blockers=(),
    )

    internal_text = _pdf_text(build_internal_review_pdf(packet))
    customer_text = _pdf_text(build_customer_explanation_pdf(packet))
    assert "SEEDED SYNTHETIC APPROVER-ROLE SNAPSHOT" in internal_text
    assert "SEEDED SYNTHETIC APPROVER-ROLE SNAPSHOT" in customer_text


def test_customer_pdf_minimizes_internal_review_and_extractor_provenance() -> None:
    packet = sample_packet()

    internal_text = _pdf_text(build_internal_review_pdf(packet))
    customer_text = _pdf_text(build_customer_explanation_pdf(packet))
    normalized_customer_text = customer_text.replace("\n", " ")
    machine_text = build_machine_json(packet).decode()

    assert "USER-REVIEWER" in internal_text
    assert "Confirmed against the quoted source" in internal_text
    assert "RUN-001" in internal_text
    assert "fixture-model" in internal_text
    assert "USER-REVIEWER" in machine_text
    assert "Confirmed against the quoted source" in machine_text
    assert "RUN-001" in machine_text
    assert "fixture-model" in machine_text
    assert "USER-REVIEWER" not in customer_text
    assert "Confirmed against the quoted source" not in customer_text
    assert "RUN-001" not in customer_text
    assert "fixture-model" not in customer_text
    assert "source-confirmed evidence; review recorded" in normalized_customer_text
    assert "explicit operator assumption" in normalized_customer_text


def test_customer_pdf_never_labels_pending_proposal_as_confirmed_or_reviewed() -> None:
    packet = sample_packet()
    accepted = packet.facts[0]
    pending = replace(
        accepted,
        review_state=ReviewState.PROPOSED,
        provenance=replace(
            accepted.provenance,
            reviewer_id=None,
            reviewer_name=None,
            review_reason=None,
            evidence_basis="pending_review",
        ),
    )
    packet = replace(packet, facts=(pending, *packet.facts[1:]))

    normalized = _pdf_text(build_customer_explanation_pdf(packet)).replace("\n", " ")

    assert "source-backed proposal; review pending" in normalized
    assert "source-confirmed evidence; review recorded" not in normalized


def test_customer_pdf_discloses_calculation_scenario_inputs() -> None:
    packet = sample_packet()

    normalized = _pdf_text(build_customer_explanation_pdf(packet)).replace("\n", " ")

    assert "Scenario inputs and assumptions" in normalized
    assert "not source-confirmed contract facts" in normalized
    for assumption in packet.assumptions:
        assert assumption in normalized


def test_pdf_formats_zero_decimal_currency_without_assuming_cents() -> None:
    packet = sample_packet()
    line = replace(
        packet.calculation_lines[0],
        currency="JPY",
        currency_exponent=0,
    )
    scenario = replace(
        packet.currency_scenarios[0],
        currency="JPY",
        currency_exponent=0,
    )
    packet = replace(packet, calculation_lines=(line,), currency_scenarios=(scenario,))

    text = _pdf_text(build_customer_explanation_pdf(packet))

    assert "JPY 4,000,000" in text
    assert "JPY 4,000,000.00" not in text


def test_content_type_and_extension_contract() -> None:
    assert content_type_for(ExportKind.INTERNAL_REVIEW_PDF) == "application/pdf"
    assert content_type_for(ExportKind.CUSTOMER_EXPLANATION_PDF) == "application/pdf"
    assert content_type_for(ExportKind.EVIDENCE_CSV) == "text/csv; charset=utf-8"
    assert content_type_for(ExportKind.MACHINE_READABLE_JSON) == "application/json"
    assert extension_for(ExportKind.INTERNAL_REVIEW_PDF) == ".pdf"
    assert extension_for(ExportKind.CUSTOMER_EXPLANATION_PDF) == ".pdf"
    assert extension_for(ExportKind.EVIDENCE_CSV) == ".csv"
    assert extension_for(ExportKind.MACHINE_READABLE_JSON) == ".json"
