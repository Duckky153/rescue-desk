"""Stable, ORM-free data-transfer contract for RescueDesk exports."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from rescue_desk.domain.money import MAX_MINOR_UNITS, SUPPORTED_CURRENCIES

EXPORT_SCHEMA_VERSION = "rescuedesk.export.v2"
DISCLAIMER = (
    "Demonstration only. This is not legal, accounting, or financial advice. "
    "RescueDesk is an independent portfolio project and is not affiliated with, "
    "endorsed by, or sponsored by Entry Inc. or DualEntry."
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ExportContractError(ValueError):
    """Raised when an export packet is incomplete or internally inconsistent."""


class ExportKind(StrEnum):
    INTERNAL_REVIEW_PDF = "internal_review_pdf"
    CUSTOMER_EXPLANATION_PDF = "customer_explanation_pdf"
    EVIDENCE_CSV = "evidence_csv"
    MACHINE_READABLE_JSON = "machine_readable_json"


class ReviewState(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    CORRECTED = "corrected"
    REJECTED = "rejected"
    CONFLICTING = "conflicting"


class BlockerStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    ACCEPTED_RISK = "accepted_risk"


class ApprovalStatus(StrEnum):
    NOT_APPROVED = "not_approved"
    APPROVED = "approved"


def _nonblank(value: str, field_name: str) -> None:
    if not value.strip():
        raise ExportContractError(f"{field_name} cannot be blank")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class SourceCitation:
    """A page-level, tamper-evident quote from one source document."""

    citation_id: str
    document_id: str
    document_name: str
    document_sha256: str
    page_number: int
    quote: str
    quote_sha256: str
    char_start: int
    char_end: int
    superseded: bool = False

    def __post_init__(self) -> None:
        for name in ("citation_id", "document_id", "document_name"):
            _nonblank(str(getattr(self, name)), name)
        if not _SHA256.fullmatch(self.document_sha256):
            raise ExportContractError("document_sha256 must be a lowercase SHA-256 digest")
        if self.page_number < 1:
            raise ExportContractError("page_number must be at least 1")
        _nonblank(self.quote, "quote")
        if not _SHA256.fullmatch(self.quote_sha256):
            raise ExportContractError("quote_sha256 must be a lowercase SHA-256 digest")
        if _sha256(self.quote.encode("utf-8")) != self.quote_sha256:
            raise ExportContractError("quote_sha256 does not match quote")
        if self.char_start < 0 or self.char_end <= self.char_start:
            raise ExportContractError("citation character offsets are invalid")
        if self.char_end - self.char_start != len(self.quote):
            raise ExportContractError("citation offsets must span the exact quote length")


@dataclass(frozen=True, slots=True)
class FactProvenance:
    """Human governance plus the extractor identity behind a reviewed fact."""

    assertion_source: str
    source_assertion_id: str
    reviewer_id: str | None
    reviewer_name: str | None
    review_reason: str | None
    assumption: bool
    evidence_basis: str
    extraction_run_id: str | None
    extractor: str | None
    model_identifier: str | None
    prompt_hash: str | None
    code_version: str | None
    structured_output_hash: str | None

    def __post_init__(self) -> None:
        if self.assertion_source not in {"deterministic", "ai", "human", "derived"}:
            raise ExportContractError("assertion_source is unsupported")
        _nonblank(self.source_assertion_id, "source_assertion_id")
        reviewer_values = (self.reviewer_id, self.reviewer_name, self.review_reason)
        if any(value is None for value in reviewer_values) != all(
            value is None for value in reviewer_values
        ):
            raise ExportContractError("reviewer identity and reason must be all present or absent")
        for value, name in zip(
            reviewer_values,
            ("reviewer_id", "reviewer_name", "review_reason"),
            strict=True,
        ):
            if value is not None:
                _nonblank(value, name)
        allowed_basis = {
            "source_evidence",
            "explicit_assumption",
            "pending_review",
            "rejected_source",
        }
        if self.evidence_basis not in allowed_basis:
            raise ExportContractError("evidence_basis is unsupported")
        if self.assumption != (self.evidence_basis == "explicit_assumption"):
            raise ExportContractError("assumption must use explicit_assumption evidence basis")
        if self.assumption and self.reviewer_id is None:
            raise ExportContractError("an explicit assumption requires an identified reviewer")
        if self.extraction_run_id is None:
            if any(
                value is not None
                for value in (
                    self.extractor,
                    self.model_identifier,
                    self.prompt_hash,
                    self.code_version,
                    self.structured_output_hash,
                )
            ):
                raise ExportContractError("extractor fields require extraction_run_id")
        else:
            _nonblank(self.extraction_run_id, "extraction_run_id")
            if self.extractor is None or self.code_version is None:
                raise ExportContractError("an extraction run requires extractor and code_version")
            _nonblank(self.extractor, "extractor")
            _nonblank(self.code_version, "code_version")
        for value, name in (
            (self.prompt_hash, "prompt_hash"),
            (self.structured_output_hash, "structured_output_hash"),
        ):
            if value is not None and not _SHA256.fullmatch(value):
                raise ExportContractError(f"{name} must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class ReviewedFact:
    fact_id: str
    label: str
    value: str
    review_state: ReviewState
    citation_ids: tuple[str, ...]
    provenance: FactProvenance
    customer_visible: bool = True
    superseded: bool = False

    def __post_init__(self) -> None:
        _nonblank(self.fact_id, "fact_id")
        _nonblank(self.label, "fact label")
        _nonblank(self.value, "fact value")
        if not self.citation_ids:
            raise ExportContractError(f"{self.fact_id} must cite at least one source")
        if self.review_state in {
            ReviewState.ACCEPTED,
            ReviewState.CORRECTED,
            ReviewState.REJECTED,
        }:
            if self.provenance.reviewer_id is None:
                raise ExportContractError(f"{self.fact_id} requires audited reviewer provenance")
            if (
                self.review_state == ReviewState.REJECTED
                and self.provenance.evidence_basis != "rejected_source"
            ):
                raise ExportContractError(
                    f"{self.fact_id} is rejected and must use rejected_source evidence basis"
                )
        elif self.provenance.evidence_basis != "pending_review":
            raise ExportContractError(
                f"{self.fact_id} is not reviewed and must use pending_review evidence basis"
            )


@dataclass(frozen=True, slots=True)
class CalculationLine:
    line_id: str
    label: str
    currency: str
    original_amount_minor: int
    result_amount_minor: int | None
    treatment: str
    formula_id: str
    formula: str
    explanation: str
    citation_ids: tuple[str, ...]
    superseded: bool = False
    currency_exponent: int = 2

    def __post_init__(self) -> None:
        for name in ("line_id", "label", "treatment", "formula_id", "formula", "explanation"):
            _nonblank(str(getattr(self, name)), name)
        if len(self.currency) != 3 or not self.currency.isalpha() or not self.currency.isupper():
            raise ExportContractError("currency must be an uppercase ISO 4217 code")
        if not 0 <= self.original_amount_minor <= MAX_MINOR_UNITS:
            raise ExportContractError("original_amount_minor is outside the exact supported range")
        if self.result_amount_minor is not None and not (
            0 <= self.result_amount_minor <= MAX_MINOR_UNITS
        ):
            raise ExportContractError("result_amount_minor is outside the exact supported range")
        if self.currency not in SUPPORTED_CURRENCIES:
            raise ExportContractError("currency is not in the supported currency registry")
        if not 0 <= self.currency_exponent <= 6:
            raise ExportContractError("currency_exponent must be between 0 and 6")
        if not self.citation_ids:
            raise ExportContractError(f"{self.line_id} must cite at least one source")


@dataclass(frozen=True, slots=True)
class CurrencyScenario:
    currency: str
    documented_remaining_subscription_minor: int
    excluded_non_subscription_minor: int
    unclassified_minor: int
    potential_coverage_min_minor: int
    potential_coverage_max_minor: int
    currency_exponent: int = 2

    def __post_init__(self) -> None:
        if len(self.currency) != 3 or not self.currency.isalpha() or not self.currency.isupper():
            raise ExportContractError("currency must be an uppercase ISO 4217 code")
        values = (
            self.documented_remaining_subscription_minor,
            self.excluded_non_subscription_minor,
            self.unclassified_minor,
            self.potential_coverage_min_minor,
            self.potential_coverage_max_minor,
        )
        if any(value < 0 for value in values):
            raise ExportContractError("currency scenario amounts cannot be negative")
        if any(value > MAX_MINOR_UNITS for value in values):
            raise ExportContractError("currency scenario amount exceeds the exact supported range")
        if self.currency not in SUPPORTED_CURRENCIES:
            raise ExportContractError("currency is not in the supported currency registry")
        if not 0 <= self.currency_exponent <= 6:
            raise ExportContractError("currency_exponent must be between 0 and 6")
        if self.potential_coverage_min_minor > self.potential_coverage_max_minor:
            raise ExportContractError("coverage minimum cannot exceed coverage maximum")


@dataclass(frozen=True, slots=True)
class ReviewBlocker:
    blocker_id: str
    title: str
    detail: str
    status: BlockerStatus
    citation_ids: tuple[str, ...] = ()
    customer_visible: bool = True

    def __post_init__(self) -> None:
        _nonblank(self.blocker_id, "blocker_id")
        _nonblank(self.title, "blocker title")
        _nonblank(self.detail, "blocker detail")


@dataclass(frozen=True, slots=True)
class ExportPacket:
    """Immutable export snapshot; its hash is shared by every artifact."""

    case_id: str
    case_name: str
    applicant_company: str
    erp_provider: str
    case_status: str
    revision_id: str
    revision_number: int
    prepared_at: datetime
    prepared_by: str
    as_of_date: date
    approval_status: ApprovalStatus
    approved_by: str | None
    citations: tuple[SourceCitation, ...]
    facts: tuple[ReviewedFact, ...]
    calculation_engine_version: str
    calculation_input_hash: str
    calculation_result_hash: str
    calculation_lines: tuple[CalculationLine, ...]
    currency_scenarios: tuple[CurrencyScenario, ...]
    blockers: tuple[ReviewBlocker, ...]
    assumptions: tuple[str, ...]
    packet_superseded: bool = False

    def __post_init__(self) -> None:
        for name in (
            "case_id",
            "case_name",
            "applicant_company",
            "erp_provider",
            "case_status",
            "revision_id",
            "prepared_by",
            "approval_status",
            "calculation_engine_version",
        ):
            _nonblank(str(getattr(self, name)), name)
        if self.revision_number < 1:
            raise ExportContractError("revision_number must be at least 1")
        if self.prepared_at.tzinfo is None or self.prepared_at.utcoffset() is None:
            raise ExportContractError("prepared_at must be timezone-aware")
        if self.approved_by is not None:
            _nonblank(self.approved_by, "approved_by")
        if self.approval_status == ApprovalStatus.APPROVED and self.approved_by is None:
            raise ExportContractError("approved packets require approved_by")
        if self.approval_status == ApprovalStatus.NOT_APPROVED and self.approved_by is not None:
            raise ExportContractError("unapproved packets cannot name approved_by")
        for name in ("calculation_input_hash", "calculation_result_hash"):
            if not _SHA256.fullmatch(str(getattr(self, name))):
                raise ExportContractError(f"{name} must be a lowercase SHA-256 digest")
        if not self.citations:
            raise ExportContractError("an export packet requires at least one source citation")
        if not self.facts:
            raise ExportContractError("an export packet requires at least one reviewed fact")
        if not self.calculation_lines:
            raise ExportContractError("an export packet requires calculation formula detail")
        if not self.currency_scenarios:
            raise ExportContractError("an export packet requires at least one currency scenario")
        scenario_currencies = [item.currency for item in self.currency_scenarios]
        if len(scenario_currencies) != len(set(scenario_currencies)):
            raise ExportContractError("duplicate currency scenario")
        if self.approval_status == ApprovalStatus.APPROVED and any(
            blocker.status == BlockerStatus.OPEN for blocker in self.blockers
        ):
            raise ExportContractError("approved packets cannot contain open blockers")
        self._validate_unique_ids()
        self._validate_references()
        scenarios_by_currency = {item.currency: item for item in self.currency_scenarios}
        line_currencies = {item.currency for item in self.calculation_lines}
        missing_currencies = line_currencies - scenarios_by_currency.keys()
        if missing_currencies:
            raise ExportContractError(
                "calculation currencies have no scenario: " + ", ".join(sorted(missing_currencies))
            )
        for line in self.calculation_lines:
            scenario = scenarios_by_currency[line.currency]
            if line.currency_exponent != scenario.currency_exponent:
                raise ExportContractError(f"currency exponent mismatch for {line.currency}")
        for assumption in self.assumptions:
            _nonblank(assumption, "assumption")

    def _validate_unique_ids(self) -> None:
        groups = (
            ("citation", tuple(item.citation_id for item in self.citations)),
            ("fact", tuple(item.fact_id for item in self.facts)),
            ("calculation line", tuple(item.line_id for item in self.calculation_lines)),
            ("blocker", tuple(item.blocker_id for item in self.blockers)),
        )
        for label, values in groups:
            if len(values) != len(set(values)):
                raise ExportContractError(f"duplicate {label} identifier")

    def _validate_references(self) -> None:
        citation_ids = {item.citation_id for item in self.citations}
        references: list[tuple[str, tuple[str, ...]]] = [
            (item.fact_id, item.citation_ids) for item in self.facts
        ]
        references.extend((item.line_id, item.citation_ids) for item in self.calculation_lines)
        references.extend((item.blocker_id, item.citation_ids) for item in self.blockers)
        for owner_id, owner_citations in references:
            missing = set(owner_citations) - citation_ids
            if missing:
                missing_text = ", ".join(sorted(missing))
                raise ExportContractError(
                    f"{owner_id} references missing citations: {missing_text}"
                )

    def canonical_payload(self) -> dict[str, object]:
        """Return the canonical snapshot content, excluding its derived hash."""

        prepared_utc = self.prepared_at.astimezone(UTC)
        return {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "case": {
                "case_id": self.case_id,
                "case_name": self.case_name,
                "applicant_company": self.applicant_company,
                "erp_provider": self.erp_provider,
                "case_status": self.case_status,
                "revision_id": self.revision_id,
                "revision_number": self.revision_number,
                "packet_superseded": self.packet_superseded,
            },
            "preparation": {
                "prepared_at": prepared_utc.isoformat().replace("+00:00", "Z"),
                "prepared_by": self.prepared_by,
                "as_of_date": self.as_of_date.isoformat(),
                "approval_status": self.approval_status.value,
                "approved_by": self.approved_by,
            },
            "citations": [
                asdict(item) for item in sorted(self.citations, key=lambda value: value.citation_id)
            ],
            "facts": [
                {
                    **asdict(item),
                    "review_state": item.review_state.value,
                    "citation_ids": sorted(item.citation_ids),
                }
                for item in sorted(self.facts, key=lambda value: value.fact_id)
            ],
            "calculation": {
                "engine_version": self.calculation_engine_version,
                "input_hash": self.calculation_input_hash,
                "result_hash": self.calculation_result_hash,
                "lines": [
                    {
                        **asdict(item),
                        "citation_ids": sorted(item.citation_ids),
                    }
                    for item in sorted(self.calculation_lines, key=lambda value: value.line_id)
                ],
                "currency_scenarios": [
                    asdict(item)
                    for item in sorted(self.currency_scenarios, key=lambda value: value.currency)
                ],
                "assumptions": sorted(self.assumptions),
            },
            "blockers": [
                {
                    **asdict(item),
                    "status": item.status.value,
                    "citation_ids": sorted(item.citation_ids),
                }
                for item in sorted(self.blockers, key=lambda value: value.blocker_id)
            ],
        }

    @property
    def snapshot_hash(self) -> str:
        return _sha256(_canonical_json(self.canonical_payload()))
