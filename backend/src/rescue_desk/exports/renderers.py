"""Deterministic RescueDesk artifact renderers."""

from __future__ import annotations

import csv
import html
import io
import json
from collections.abc import Iterable
from dataclasses import asdict
from datetime import UTC
from decimal import Decimal

from reportlab.lib import colors  # type: ignore[import-untyped]
from reportlab.lib.enums import TA_CENTER  # type: ignore[import-untyped]
from reportlab.lib.pagesizes import LETTER  # type: ignore[import-untyped]
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # type: ignore[import-untyped]
from reportlab.lib.units import inch  # type: ignore[import-untyped]
from reportlab.platypus import (  # type: ignore[import-untyped]
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from rescue_desk.demo import DEMO_COMPLETED_CASE_NAME
from rescue_desk.exports.contracts import (
    DISCLAIMER,
    EXPORT_SCHEMA_VERSION,
    ApprovalStatus,
    BlockerStatus,
    CalculationLine,
    ExportKind,
    ExportPacket,
    ReviewedFact,
    ReviewState,
    SourceCitation,
)

_CONTENT_TYPES: dict[ExportKind, str] = {
    ExportKind.INTERNAL_REVIEW_PDF: "application/pdf",
    ExportKind.CUSTOMER_EXPLANATION_PDF: "application/pdf",
    ExportKind.EVIDENCE_CSV: "text/csv; charset=utf-8",
    ExportKind.MACHINE_READABLE_JSON: "application/json",
}

_EXTENSIONS: dict[ExportKind, str] = {
    ExportKind.INTERNAL_REVIEW_PDF: ".pdf",
    ExportKind.CUSTOMER_EXPLANATION_PDF: ".pdf",
    ExportKind.EVIDENCE_CSV: ".csv",
    ExportKind.MACHINE_READABLE_JSON: ".json",
}

_TEAL = colors.HexColor("#206570")
_DARK_TEAL = colors.HexColor("#0F373E")
_RED = colors.HexColor("#C9372C")
_AMBER = colors.HexColor("#9A6700")
_GREEN = colors.HexColor("#1A7F37")
_GREY = colors.HexColor("#5B6263")


def content_type_for(kind: ExportKind) -> str:
    return _CONTENT_TYPES[kind]


def extension_for(kind: ExportKind) -> str:
    return _EXTENSIONS[kind]


def _safe(value: object) -> str:
    return html.escape(str(value), quote=True)


def _money(amount_minor: int | None, currency: str, exponent: int) -> str:
    if amount_minor is None:
        return "Not calculated"
    value = Decimal(amount_minor) / (Decimal(10) ** exponent)
    return f"{currency} {value:,.{exponent}f}"


def _spreadsheet_safe(value: object) -> str:
    """Neutralize values that spreadsheet applications could execute as formulas."""

    text = str(value)
    possible_formula = text.lstrip(" \v\f")
    if possible_formula.startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
        return f"'{text}"
    return text


def _paragraph(value: object, style: ParagraphStyle) -> Paragraph:
    escaped = _safe(value).replace("\n", "<br/>")
    return Paragraph(escaped, style)


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "RescueTitle",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=22,
            leading=27,
            textColor=_DARK_TEAL,
            spaceAfter=14,
        ),
        "subtitle": ParagraphStyle(
            "RescueSubtitle",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=10,
            leading=14,
            textColor=_GREY,
            spaceAfter=12,
        ),
        "h1": ParagraphStyle(
            "RescueH1",
            parent=base["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=14,
            leading=18,
            textColor=_DARK_TEAL,
            spaceBefore=14,
            spaceAfter=8,
        ),
        "h2": ParagraphStyle(
            "RescueH2",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=14,
            textColor=_TEAL,
            spaceBefore=8,
            spaceAfter=5,
        ),
        "body": ParagraphStyle(
            "RescueBody",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=9,
            leading=13,
            textColor=colors.HexColor("#2D3132"),
            spaceAfter=5,
        ),
        "small": ParagraphStyle(
            "RescueSmall",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=7.5,
            leading=10,
            textColor=_GREY,
        ),
        "table_header": ParagraphStyle(
            "RescueTableHeader",
            parent=base["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=7.5,
            leading=10,
            textColor=colors.white,
        ),
        "disclaimer": ParagraphStyle(
            "RescueDisclaimer",
            parent=base["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=11,
            textColor=_RED,
            borderColor=_RED,
            borderWidth=0.6,
            borderPadding=8,
            backColor=colors.HexColor("#FFF1F0"),
            spaceAfter=12,
        ),
        "status": ParagraphStyle(
            "RescueStatus",
            parent=base["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=9,
            leading=12,
            alignment=TA_CENTER,
            textColor=colors.white,
        ),
    }


def _table(
    rows: list[list[object]],
    *,
    widths: list[float] | None = None,
    header: bool = False,
) -> Table:
    table = Table(rows, colWidths=widths, repeatRows=1 if header else 0, hAlign="LEFT")
    commands: list[tuple[object, ...]] = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CED9DA")),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
    ]
    if header:
        commands.extend(
            [
                ("BACKGROUND", (0, 0), (-1, 0), _DARK_TEAL),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ]
        )
    table.setStyle(TableStyle(commands))
    return table


def _citation_text(citation_ids: Iterable[str]) -> str:
    values = sorted(set(citation_ids))
    return ", ".join(values) if values else "No document citation"


def _status_banner(packet: ExportPacket, styles: dict[str, ParagraphStyle]) -> Table:
    if packet.packet_superseded:
        text = "SUPERSEDED SNAPSHOT - DO NOT RELY ON THIS PACKET"
        background = _RED
    elif any(item.status == BlockerStatus.OPEN for item in packet.blockers):
        text = "HUMAN REVIEW REQUIRED - OPEN BLOCKERS REMAIN"
        background = _AMBER
    elif packet.approval_status == ApprovalStatus.NOT_APPROVED:
        text = "NOT APPROVED - HUMAN APPROVAL REQUIRED"
        background = _AMBER
    elif packet.case_name == DEMO_COMPLETED_CASE_NAME:
        text = "SEEDED SYNTHETIC APPROVER-ROLE SNAPSHOT"
        background = _GREEN
    else:
        text = "APPROVER-RECORDED SNAPSHOT"
        background = _GREEN
    table = Table([[_paragraph(text, styles["status"])]], colWidths=[7.1 * inch])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), background),
                ("BOX", (0, 0), (-1, -1), 0, background),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    return table


def _new_document(
    buffer: io.BytesIO,
    *,
    title: str,
    packet: ExportPacket,
    compact: bool = False,
) -> SimpleDocTemplate:
    vertical_margin = (0.35 if compact else 0.65) * inch
    return SimpleDocTemplate(
        buffer,
        pagesize=LETTER,
        leftMargin=0.65 * inch,
        rightMargin=0.65 * inch,
        topMargin=vertical_margin,
        bottomMargin=vertical_margin,
        title=title,
        author="RescueDesk",
        subject=f"Snapshot {packet.snapshot_hash}",
        creator="RescueDesk deterministic export renderer",
        producer="RescueDesk / ReportLab",
        invariant=1,
        pageCompression=1,
        lang="en-US",
    )


def _identity_story(packet: ExportPacket, styles: dict[str, ParagraphStyle]) -> list[object]:
    prepared = packet.prepared_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    rows = [
        [_paragraph("Case", styles["small"]), _paragraph(packet.case_name, styles["body"])],
        [
            _paragraph("Company", styles["small"]),
            _paragraph(packet.applicant_company, styles["body"]),
        ],
        [
            _paragraph("Legacy ERP", styles["small"]),
            _paragraph(packet.erp_provider, styles["body"]),
        ],
        [
            _paragraph("Revision", styles["small"]),
            _paragraph(f"{packet.revision_number} ({packet.revision_id})", styles["body"]),
        ],
        [
            _paragraph("Prepared / as of", styles["small"]),
            _paragraph(
                f"{prepared} by {packet.prepared_by}\nAs of {packet.as_of_date.isoformat()}",
                styles["small"],
            ),
        ],
        [
            _paragraph("Workflow / approval", styles["small"]),
            _paragraph(
                f"{packet.case_status} / {packet.approval_status.value}\n"
                f"Approver: {packet.approved_by or 'None recorded'}",
                styles["small"],
            ),
        ],
        [
            _paragraph("Integrity hashes", styles["small"]),
            _paragraph(
                f"Snapshot: {packet.snapshot_hash}\n"
                f"Calculation result: {packet.calculation_result_hash}",
                styles["small"],
            ),
        ],
    ]
    return [_table(rows, widths=[1.3 * inch, 5.8 * inch]), Spacer(1, 8)]


def _scenario_story(packet: ExportPacket, styles: dict[str, ParagraphStyle]) -> list[object]:
    rows: list[list[object]] = [
        [
            _paragraph("Currency", styles["table_header"]),
            _paragraph("Documented remainder", styles["table_header"]),
            _paragraph("Excluded", styles["table_header"]),
            _paragraph("Unclassified", styles["table_header"]),
            _paragraph("Scenario range", styles["table_header"]),
        ]
    ]
    for scenario in sorted(packet.currency_scenarios, key=lambda item: item.currency):
        coverage_min = _money(
            scenario.potential_coverage_min_minor,
            scenario.currency,
            scenario.currency_exponent,
        )
        coverage_max = _money(
            scenario.potential_coverage_max_minor,
            scenario.currency,
            scenario.currency_exponent,
        )
        rows.append(
            [
                _paragraph(scenario.currency, styles["body"]),
                _paragraph(
                    _money(
                        scenario.documented_remaining_subscription_minor,
                        scenario.currency,
                        scenario.currency_exponent,
                    ),
                    styles["body"],
                ),
                _paragraph(
                    _money(
                        scenario.excluded_non_subscription_minor,
                        scenario.currency,
                        scenario.currency_exponent,
                    ),
                    styles["body"],
                ),
                _paragraph(
                    _money(
                        scenario.unclassified_minor,
                        scenario.currency,
                        scenario.currency_exponent,
                    ),
                    styles["body"],
                ),
                _paragraph(
                    f"{coverage_min} to {coverage_max}",
                    styles["body"],
                ),
            ]
        )
    return [
        _table(
            rows,
            widths=[0.7 * inch, 1.5 * inch, 1.15 * inch, 1.15 * inch, 2.6 * inch],
            header=True,
        )
    ]


def _blocker_story(
    packet: ExportPacket,
    styles: dict[str, ParagraphStyle],
    *,
    customer_only: bool,
) -> list[object]:
    blockers = [item for item in packet.blockers if not customer_only or item.customer_visible]
    if not blockers:
        return [_paragraph("No blockers are recorded in this snapshot.", styles["body"])]
    story: list[object] = []
    for blocker in sorted(blockers, key=lambda item: item.blocker_id):
        story.append(
            _paragraph(
                f"{blocker.status.value.upper()}: {blocker.title} - {blocker.detail} "
                f"[Sources: {_citation_text(blocker.citation_ids)}]",
                styles["body"],
            )
        )
    return story


def _formula_line_story(
    line: CalculationLine, styles: dict[str, ParagraphStyle], *, customer: bool
) -> list[object]:
    status = "SUPERSEDED" if line.superseded else "CURRENT"
    amount = _money(line.result_amount_minor, line.currency, line.currency_exponent)
    original = _money(line.original_amount_minor, line.currency, line.currency_exponent)
    if customer:
        text = (
            f"{line.label}: {amount}. {line.explanation} Formula: {line.formula}. "
            f"Sources: {_citation_text(line.citation_ids)}. Status: {status}."
        )
    else:
        text = (
            f"{line.line_id} | {line.label} | {status}\n"
            f"Original: {original} | "
            f"Result: {amount} | Treatment: {line.treatment}\n"
            f"Formula ID: {line.formula_id} | Formula: {line.formula}\n"
            f"Rationale: {line.explanation}\n"
            f"Sources: {_citation_text(line.citation_ids)}"
        )
    return [_paragraph(text, styles["body"]), Spacer(1, 3)]


def _fact_provenance_text(fact: ReviewedFact) -> str:
    provenance = fact.provenance
    parts = [
        f"Evidence basis: {provenance.evidence_basis.replace('_', ' ')}",
        f"Assertion source: {provenance.assertion_source}",
        f"Source assertion: {provenance.source_assertion_id}",
    ]
    if provenance.reviewer_name is not None:
        parts.append(f"Reviewer: {provenance.reviewer_name} ({provenance.reviewer_id})")
        parts.append(f"Review reason: {provenance.review_reason}")
    if provenance.extraction_run_id is not None:
        parts.append(
            f"Extractor: {provenance.extractor}; run {provenance.extraction_run_id}; "
            f"code {provenance.code_version}"
        )
        if provenance.model_identifier is not None:
            parts.append(f"Model: {provenance.model_identifier}")
        if provenance.prompt_hash is not None:
            parts.append(f"Prompt SHA-256: {provenance.prompt_hash}")
        if provenance.structured_output_hash is not None:
            parts.append(f"Output SHA-256: {provenance.structured_output_hash}")
    return "\n".join(parts)


def _customer_fact_provenance_text(fact: ReviewedFact) -> str:
    """Describe governance without exposing internal identities or implementation metadata."""

    basis = fact.provenance.evidence_basis
    if fact.provenance.assumption or basis == "explicit_assumption":
        return "Evidence basis: explicit operator assumption; review recorded"
    if basis == "source_evidence" and fact.review_state in {
        ReviewState.ACCEPTED,
        ReviewState.CORRECTED,
    }:
        return "Evidence basis: source-confirmed evidence; review recorded"
    if basis == "rejected_source" or fact.review_state == ReviewState.REJECTED:
        return "Evidence basis: source proposal rejected; review recorded"
    if basis == "pending_review" or fact.review_state in {
        ReviewState.PROPOSED,
        ReviewState.CONFLICTING,
    }:
        return "Evidence basis: source-backed proposal; review pending"
    return "Evidence basis: unverified provenance; review required"


def build_internal_review_pdf(packet: ExportPacket) -> bytes:
    styles = _styles()
    buffer = io.BytesIO()
    document = _new_document(buffer, title="RescueDesk Internal Review Packet", packet=packet)
    story: list[object] = [
        _paragraph("Internal review packet", styles["title"]),
        _paragraph(
            "Evidence-backed demonstration of contractual facts and deterministic scenario math.",
            styles["subtitle"],
        ),
        _paragraph(DISCLAIMER, styles["disclaimer"]),
        _status_banner(packet, styles),
        Spacer(1, 10),
    ]
    story.extend(_identity_story(packet, styles))
    story.extend([_paragraph("Scenario summary", styles["h1"]), *_scenario_story(packet, styles)])

    story.append(_paragraph("Reviewed facts", styles["h1"]))
    fact_rows: list[list[object]] = [
        [
            _paragraph("Fact", styles["table_header"]),
            _paragraph("Value", styles["table_header"]),
            _paragraph("Review / source", styles["table_header"]),
        ]
    ]
    for fact in sorted(packet.facts, key=lambda item: (item.superseded, item.fact_id)):
        status = "SUPERSEDED" if fact.superseded else fact.review_state.value.upper()
        fact_rows.append(
            [
                _paragraph(f"{fact.fact_id}: {fact.label}", styles["body"]),
                _paragraph(fact.value, styles["body"]),
                _paragraph(
                    f"{status}\n{_citation_text(fact.citation_ids)}\n{_fact_provenance_text(fact)}",
                    styles["small"],
                ),
            ]
        )
    story.append(_table(fact_rows, widths=[2.1 * inch, 2.3 * inch, 2.7 * inch], header=True))

    story.append(_paragraph("Deterministic calculation detail", styles["h1"]))
    story.append(
        _paragraph(
            f"Engine: {packet.calculation_engine_version}\n"
            f"Input SHA-256: {packet.calculation_input_hash}\n"
            f"Result SHA-256: {packet.calculation_result_hash}",
            styles["small"],
        )
    )
    for line in sorted(packet.calculation_lines, key=lambda item: item.line_id):
        story.extend(_formula_line_story(line, styles, customer=False))

    story.append(
        KeepTogether(
            [
                _paragraph("Blockers and review state", styles["h1"]),
                *_blocker_story(packet, styles, customer_only=False),
            ]
        )
    )
    story.append(_paragraph("Assumptions", styles["h1"]))
    if packet.assumptions:
        for assumption in sorted(packet.assumptions):
            story.append(_paragraph(f"- {assumption}", styles["body"]))
    else:
        story.append(_paragraph("No calculation assumptions are recorded.", styles["body"]))

    story.append(_paragraph("Evidence register", styles["title"]))
    for citation in sorted(packet.citations, key=lambda item: item.citation_id):
        status = "SUPERSEDED SOURCE" if citation.superseded else "CURRENT SOURCE"
        story.extend(
            [
                _paragraph(f"{citation.citation_id} - {status}", styles["h2"]),
                _paragraph(
                    f"Document: {citation.document_name} ({citation.document_id})\n"
                    f"Document SHA-256: {citation.document_sha256}\n"
                    f"Page {citation.page_number}; characters {citation.char_start}-"
                    f"{citation.char_end}; quote SHA-256: {citation.quote_sha256}",
                    styles["small"],
                ),
                _paragraph(f'"{citation.quote}"', styles["body"]),
            ]
        )
    story.extend(
        [
            _paragraph("Integrity", styles["h1"]),
            _paragraph(
                f"Schema: {EXPORT_SCHEMA_VERSION}. All sections in this PDF were rendered from "
                f"snapshot SHA-256 {packet.snapshot_hash}. Re-export the matching machine-readable "
                "JSON to verify the canonical content.",
                styles["body"],
            ),
            _paragraph(DISCLAIMER, styles["disclaimer"]),
        ]
    )
    document.build(story)
    return buffer.getvalue()


def build_customer_explanation_pdf(packet: ExportPacket) -> bytes:
    styles = _styles()
    buffer = io.BytesIO()
    document = _new_document(
        buffer,
        title="RescueDesk Switching Evidence Brief",
        packet=packet,
        compact=True,
    )
    story: list[object] = [
        _paragraph("Switching evidence brief", styles["title"]),
        _paragraph(
            "A plain-language summary of reviewed contract evidence and scenario calculations.",
            styles["subtitle"],
        ),
        _paragraph(DISCLAIMER, styles["disclaimer"]),
        _status_banner(packet, styles),
        Spacer(1, 10),
    ]
    story.extend(_identity_story(packet, styles))
    story.extend(
        [
            _paragraph("What the documents currently show", styles["h1"]),
            _paragraph(
                "This brief records evidence and mathematical scenarios. It does not determine "
                "contract enforceability, eligibility, available credits, or a recommended action.",
                styles["body"],
            ),
        ]
    )
    for fact in sorted(packet.facts, key=lambda item: (item.superseded, item.fact_id)):
        if not fact.customer_visible:
            continue
        status = "SUPERSEDED - DO NOT RELY" if fact.superseded else fact.review_state.value.upper()
        story.append(
            _paragraph(
                f"{fact.label}: {fact.value}. Review status: {status}. "
                f"Sources: {_citation_text(fact.citation_ids)}. "
                f"{_customer_fact_provenance_text(fact)}.",
                styles["body"],
            )
        )
    story.extend([_paragraph("Scenario summary", styles["h1"]), *_scenario_story(packet, styles)])
    story.append(_paragraph("How the figures were calculated", styles["h1"]))
    for line in sorted(packet.calculation_lines, key=lambda item: item.line_id):
        story.extend(_formula_line_story(line, styles, customer=True))
    story.append(_paragraph("Scenario inputs and assumptions", styles["h1"]))
    story.append(
        _paragraph(
            "These inputs shape the calculation scenario; they are not source-confirmed "
            "contract facts unless separately shown above.",
            styles["body"],
        )
    )
    if packet.assumptions:
        for assumption in sorted(packet.assumptions):
            story.append(_paragraph(f"- {assumption}", styles["body"]))
    else:
        story.append(_paragraph("No calculation assumptions are recorded.", styles["body"]))
    story.append(_paragraph("Items that still need human review", styles["h1"]))
    story.extend(_blocker_story(packet, styles, customer_only=True))

    visible_citations: set[str] = set()
    for fact in packet.facts:
        if fact.customer_visible:
            visible_citations.update(fact.citation_ids)
    for line in packet.calculation_lines:
        visible_citations.update(line.citation_ids)
    for blocker in packet.blockers:
        if blocker.customer_visible:
            visible_citations.update(blocker.citation_ids)
    citations_by_id = {item.citation_id: item for item in packet.citations}
    story.append(_paragraph("Source notes", styles["h1"]))
    for citation_id in sorted(visible_citations):
        citation = citations_by_id[citation_id]
        status = "SUPERSEDED SOURCE" if citation.superseded else "CURRENT SOURCE"
        story.append(
            _paragraph(
                f"{citation.citation_id}: {citation.document_name}, page "
                f'{citation.page_number} ({status}) - "{citation.quote}"',
                styles["small"],
            )
        )
    document.build(story)
    return buffer.getvalue()


def _references_for(packet: ExportPacket, citation_id: str) -> tuple[str, ...]:
    references: list[str] = []
    references.extend(item.fact_id for item in packet.facts if citation_id in item.citation_ids)
    references.extend(
        item.line_id for item in packet.calculation_lines if citation_id in item.citation_ids
    )
    references.extend(
        item.blocker_id for item in packet.blockers if citation_id in item.citation_ids
    )
    return tuple(sorted(references))


def _formula_context_for(packet: ExportPacket, citation_id: str) -> tuple[str, str]:
    lines = [item for item in packet.calculation_lines if citation_id in item.citation_ids]
    identifiers = ";".join(sorted(item.formula_id for item in lines))
    formulas = ";".join(
        f"{item.line_id}: {item.formula}" for item in sorted(lines, key=lambda item: item.line_id)
    )
    return identifiers, formulas


def _fact_provenance_context_for(packet: ExportPacket, citation_id: str) -> str:
    facts = [item for item in packet.facts if citation_id in item.citation_ids]
    payload = [
        {
            "fact_id": fact.fact_id,
            "review_state": fact.review_state.value,
            "provenance": asdict(fact.provenance),
        }
        for fact in sorted(facts, key=lambda item: item.fact_id)
    ]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _blocker_context_for(packet: ExportPacket, citation_id: str) -> str:
    blockers = [item for item in packet.blockers if citation_id in item.citation_ids]
    return ";".join(
        f"{item.blocker_id}:{item.status.value}:{item.title}"
        for item in sorted(blockers, key=lambda item: item.blocker_id)
    )


def build_evidence_csv(packet: ExportPacket) -> bytes:
    buffer = io.StringIO(newline="")
    fieldnames = (
        "schema_version",
        "snapshot_sha256",
        "disclaimer",
        "case_id",
        "revision_id",
        "citation_id",
        "referenced_by",
        "formula_ids",
        "formula_detail",
        "blocker_context",
        "fact_provenance",
        "source_status",
        "document_id",
        "document_name",
        "document_sha256",
        "page_number",
        "char_start",
        "char_end",
        "quote",
        "quote_sha256",
    )
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for citation in sorted(packet.citations, key=lambda item: item.citation_id):
        formula_ids, formula_detail = _formula_context_for(packet, citation.citation_id)
        row: dict[str, object] = {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "snapshot_sha256": packet.snapshot_hash,
            "disclaimer": DISCLAIMER,
            "case_id": packet.case_id,
            "revision_id": packet.revision_id,
            "citation_id": citation.citation_id,
            "referenced_by": ";".join(_references_for(packet, citation.citation_id)),
            "formula_ids": formula_ids,
            "formula_detail": formula_detail,
            "blocker_context": _blocker_context_for(packet, citation.citation_id),
            "fact_provenance": _fact_provenance_context_for(packet, citation.citation_id),
            "source_status": "superseded" if citation.superseded else "current",
            "document_id": citation.document_id,
            "document_name": citation.document_name,
            "document_sha256": citation.document_sha256,
            "page_number": citation.page_number,
            "char_start": citation.char_start,
            "char_end": citation.char_end,
            "quote": citation.quote,
            "quote_sha256": citation.quote_sha256,
        }
        writer.writerow({key: _spreadsheet_safe(value) for key, value in row.items()})
    return buffer.getvalue().encode("utf-8")


def build_machine_json(packet: ExportPacket) -> bytes:
    payload: dict[str, object] = {
        "artifact_kind": ExportKind.MACHINE_READABLE_JSON.value,
        "disclaimer": DISCLAIMER,
        "snapshot_sha256": packet.snapshot_hash,
        "snapshot": packet.canonical_payload(),
        "integrity": {
            "hash_algorithm": "SHA-256",
            "canonicalization": "UTF-8 JSON; keys sorted; compact separators",
            "calculation_input_sha256": packet.calculation_input_hash,
            "calculation_result_sha256": packet.calculation_result_hash,
        },
    }
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def citation_as_dict(citation: SourceCitation) -> dict[str, object]:
    """Expose a typed serialization helper for integrations and tests."""

    return asdict(citation)
