import hashlib
from datetime import UTC, date, datetime

from rescue_desk.exports import (
    ApprovalStatus,
    BlockerStatus,
    CalculationLine,
    CurrencyScenario,
    ExportPacket,
    FactProvenance,
    ReviewBlocker,
    ReviewedFact,
    ReviewState,
    SourceCitation,
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sample_packet() -> ExportPacket:
    current_quote = "Annual subscription fee is $120,000."
    superseded_quote = '=HYPERLINK("https://example.invalid","source")'
    citations = (
        SourceCitation(
            citation_id="EV-001",
            document_id="DOC-001",
            document_name="LegacyERP-Order-Form.pdf",
            document_sha256="a" * 64,
            page_number=3,
            quote=current_quote,
            quote_sha256=digest(current_quote),
            char_start=210,
            char_end=210 + len(current_quote),
        ),
        SourceCitation(
            citation_id="EV-OLD",
            document_id="DOC-OLD",
            document_name='=HYPERLINK("https://example.invalid","old-contract")',
            document_sha256="b" * 64,
            page_number=8,
            quote=superseded_quote,
            quote_sha256=digest(superseded_quote),
            char_start=42,
            char_end=42 + len(superseded_quote),
            superseded=True,
        ),
    )
    return ExportPacket(
        case_id="CASE-042",
        case_name="Northstar ERP exit review",
        applicant_company="Northstar Components (synthetic)",
        erp_provider="LegacyERP (synthetic)",
        case_status="evidence_review",
        revision_id="REV-003",
        revision_number=3,
        prepared_at=datetime(2026, 8, 20, 22, 15, tzinfo=UTC),
        prepared_by="Demo Analyst",
        as_of_date=date(2026, 8, 20),
        approval_status=ApprovalStatus.NOT_APPROVED,
        approved_by=None,
        citations=citations,
        facts=(
            ReviewedFact(
                fact_id="FACT-RENEWAL",
                label="Renewal deadline",
                value="2026-10-01",
                review_state=ReviewState.ACCEPTED,
                citation_ids=("EV-001",),
                provenance=FactProvenance(
                    assertion_source="ai",
                    source_assertion_id="ASSERT-RENEWAL",
                    reviewer_id="USER-REVIEWER",
                    reviewer_name="Demo Reviewer",
                    review_reason="Confirmed against the quoted source.",
                    assumption=False,
                    evidence_basis="source_evidence",
                    extraction_run_id="RUN-001",
                    extractor="fixture-extractor",
                    model_identifier="fixture-model",
                    prompt_hash="e" * 64,
                    code_version="fixture-v1",
                    structured_output_hash="f" * 64,
                ),
            ),
            ReviewedFact(
                fact_id="FACT-OLD-FEE",
                label="Prior fee schedule",
                value="$98,000",
                review_state=ReviewState.CORRECTED,
                citation_ids=("EV-OLD",),
                provenance=FactProvenance(
                    assertion_source="human",
                    source_assertion_id="ASSERT-OLD-FEE",
                    reviewer_id="USER-REVIEWER",
                    reviewer_name="Demo Reviewer",
                    review_reason="Recorded as a scenario assumption.",
                    assumption=True,
                    evidence_basis="explicit_assumption",
                    extraction_run_id=None,
                    extractor=None,
                    model_identifier=None,
                    prompt_hash=None,
                    code_version=None,
                    structured_output_hash=None,
                ),
                superseded=True,
            ),
        ),
        calculation_engine_version="remaining-subscription-v1",
        calculation_input_hash="c" * 64,
        calculation_result_hash="d" * 64,
        calculation_lines=(
            CalculationLine(
                line_id="CALC-SUB-001",
                label="Documented remaining subscription",
                currency="USD",
                original_amount_minor=12_000_000,
                result_amount_minor=4_000_000,
                treatment="included",
                formula_id="contract-daily-half-open-v1",
                formula=("round_half_up(original_minor * remaining_days / total_service_days)"),
                explanation="Uses the reviewed service interval and the packet as-of date.",
                citation_ids=("EV-001",),
            ),
        ),
        currency_scenarios=(
            CurrencyScenario(
                currency="USD",
                documented_remaining_subscription_minor=4_000_000,
                excluded_non_subscription_minor=500_000,
                unclassified_minor=250_000,
                potential_coverage_min_minor=0,
                potential_coverage_max_minor=4_000_000,
            ),
        ),
        blockers=(
            ReviewBlocker(
                blocker_id="BLOCK-TERMINATION",
                title="Termination clause needs human review",
                detail="The contract excerpt does not establish enforceability or eligibility.",
                status=BlockerStatus.OPEN,
                citation_ids=("EV-001",),
            ),
        ),
        assumptions=(
            "Daily proration uses a half-open service interval.",
            "Currencies remain separate; no foreign-exchange conversion is performed.",
        ),
    )
