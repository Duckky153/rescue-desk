from dataclasses import replace
from datetime import datetime

import pytest

from rescue_desk.exports import (
    ApprovalStatus,
    BlockerStatus,
    ExportContractError,
    ReviewBlocker,
    ReviewedFact,
    ReviewState,
    SourceCitation,
)

from ._fixtures import digest, sample_packet


def test_snapshot_hash_is_order_independent_and_sensitive_to_content() -> None:
    packet = sample_packet()
    reordered = replace(
        packet,
        citations=tuple(reversed(packet.citations)),
        facts=tuple(reversed(packet.facts)),
        assumptions=tuple(reversed(packet.assumptions)),
    )
    changed = replace(packet, case_name="Different case name")

    assert reordered.snapshot_hash == packet.snapshot_hash
    assert changed.snapshot_hash != packet.snapshot_hash
    assert len(packet.snapshot_hash) == 64


def test_packet_rejects_unknown_citation_reference() -> None:
    packet = sample_packet()
    bad_fact = ReviewedFact(
        fact_id="FACT-MISSING",
        label="Unsupported fact",
        value="unknown",
        review_state=ReviewState.PROPOSED,
        citation_ids=("EV-NOT-THERE",),
        provenance=replace(
            packet.facts[0].provenance,
            reviewer_id=None,
            reviewer_name=None,
            review_reason=None,
            assumption=False,
            evidence_basis="pending_review",
        ),
    )

    with pytest.raises(ExportContractError, match="references missing citations"):
        replace(packet, facts=(*packet.facts, bad_fact))


def test_packet_requires_timezone_aware_snapshot_time() -> None:
    packet = sample_packet()

    with pytest.raises(ExportContractError, match="timezone-aware"):
        replace(packet, prepared_at=datetime(2026, 8, 20, 22, 15))


def test_source_citation_rejects_tampered_quote_hash() -> None:
    quote = "Contract evidence"

    with pytest.raises(ExportContractError, match="does not match quote"):
        SourceCitation(
            citation_id="EV-X",
            document_id="DOC-X",
            document_name="contract.pdf",
            document_sha256="a" * 64,
            page_number=1,
            quote=quote,
            quote_sha256=digest(f"{quote} changed"),
            char_start=0,
            char_end=len(quote),
        )


def test_source_citation_rejects_offset_length_mismatch() -> None:
    quote = "Contract evidence"

    with pytest.raises(ExportContractError, match="exact quote length"):
        SourceCitation(
            citation_id="EV-X",
            document_id="DOC-X",
            document_name="contract.pdf",
            document_sha256="a" * 64,
            page_number=1,
            quote=quote,
            quote_sha256=digest(quote),
            char_start=0,
            char_end=len(quote) - 1,
        )


def test_packet_rejects_duplicate_citation_ids() -> None:
    packet = sample_packet()

    with pytest.raises(ExportContractError, match="duplicate citation identifier"):
        replace(packet, citations=(packet.citations[0], packet.citations[0]))


def test_approval_contract_rejects_missing_approver_and_open_blockers() -> None:
    packet = sample_packet()

    with pytest.raises(ExportContractError, match="require approved_by"):
        replace(packet, approval_status=ApprovalStatus.APPROVED)

    open_blocker = ReviewBlocker(
        blocker_id="BLOCK-OPEN",
        title="Still open",
        detail="Human review is incomplete.",
        status=BlockerStatus.OPEN,
    )
    with pytest.raises(ExportContractError, match="open blockers"):
        replace(
            packet,
            approval_status=ApprovalStatus.APPROVED,
            approved_by="Approver",
            blockers=(open_blocker,),
        )


def test_approval_contract_rejects_approver_on_unapproved_packet() -> None:
    packet = sample_packet()

    with pytest.raises(ExportContractError, match="cannot name approved_by"):
        replace(packet, approved_by="Unexpected Approver")


def test_packet_rejects_currency_exponent_mismatch() -> None:
    packet = sample_packet()
    zero_decimal_line = replace(packet.calculation_lines[0], currency_exponent=0)

    with pytest.raises(ExportContractError, match="currency exponent mismatch"):
        replace(packet, calculation_lines=(zero_decimal_line,))
