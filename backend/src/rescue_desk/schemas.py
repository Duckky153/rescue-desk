from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from rescue_desk.domain.money import MAX_MINOR_UNITS, SUPPORTED_CURRENCIES
from rescue_desk.models import (
    AssertionReviewState,
    AssertionSource,
    CaseStatus,
    ExportKind,
    FeeCategory,
    FindingSeverity,
    FindingStatus,
    ProcessingStatus,
    ReviewDecisionType,
    Role,
    SafetyStatus,
)


class OrmModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=256)
    organization_id: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    organization_id: str
    role: Role


class CurrentUserResponse(BaseModel):
    id: str
    email: str
    display_name: str
    organization_id: str
    role: Role


class CaseCreate(BaseModel):
    display_name: str = Field(min_length=2, max_length=180)
    applicant_company: str = Field(min_length=2, max_length=180)
    erp_provider: str = Field(min_length=2, max_length=120)
    assigned_analyst_id: str | None = None
    assigned_approver_id: str | None = None


class CaseSummary(OrmModel):
    id: str
    display_name: str
    applicant_company: str
    erp_provider: str
    status: CaseStatus
    current_revision_number: int
    version: int
    created_at: datetime
    updated_at: datetime


class RevisionSummary(OrmModel):
    id: str
    number: int
    reason: str
    snapshot_hash: str | None
    created_by: str
    created_at: datetime


class CaseDetail(CaseSummary):
    organization_id: str
    assigned_analyst_id: str | None
    assigned_approver_id: str | None
    revisions: list[RevisionSummary]


class TransitionRequest(BaseModel):
    target: CaseStatus
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=2000)


class DocumentSummary(OrmModel):
    id: str
    case_revision_id: str
    original_filename: str
    sha256: str
    media_type: str
    source_type: str
    size_bytes: int
    page_count: int
    safety_status: SafetyStatus
    processing_status: ProcessingStatus
    processing_error: str | None
    superseded: bool
    created_at: datetime


class DocumentPageResponse(OrmModel):
    id: str
    page_number: int
    text: str
    text_sha256: str
    extraction_confidence: Decimal


class DocumentSupersedeRequest(BaseModel):
    expected_case_version: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=3000)


class EvidenceResponse(OrmModel):
    id: str
    page_id: str
    document_id: str
    page_number: int
    quote: str
    char_start: int
    char_end: int
    quote_sha256: str


class AssertionResponse(OrmModel):
    id: str
    case_revision_id: str
    semantic_key: str
    raw_value: str
    normalized_value: dict[str, Any]
    display_value: str
    source: AssertionSource
    confidence: Decimal
    review_state: AssertionReviewState
    version: int
    is_current: bool
    created_at: datetime
    evidence: list[EvidenceResponse]
    assumption: bool = False
    review_reason: str | None = None
    reviewed_by: str | None = None
    evidence_basis: Literal[
        "pending_review",
        "source_evidence",
        "explicit_assumption",
        "rejected_source",
        "invalid_review",
    ] = "pending_review"


class AssertionReviewRequest(BaseModel):
    decision: ReviewDecisionType
    expected_assertion_version: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=3000)
    corrected_value: dict[str, Any] | None = None
    corrected_display_value: str | None = Field(default=None, max_length=2000)
    assumption: bool = False

    @field_validator("corrected_value")
    @classmethod
    def correction_requires_correct_decision(
        cls, value: dict[str, Any] | None, info: Any
    ) -> dict[str, Any] | None:
        if value is not None and info.data.get("decision") != ReviewDecisionType.CORRECT:
            raise ValueError("Corrected value is allowed only for a correction decision")
        return value


class FeeCreate(BaseModel):
    category: FeeCategory
    amount_minor: int = Field(strict=True, ge=0, le=MAX_MINOR_UNITS)
    currency: str = Field(min_length=3, max_length=3)
    service_start: date | None = None
    service_end: date | None = None
    obligation_date: date | None = None
    payment_status: Literal["unknown", "unpaid", "paid"] = "unknown"
    billing_cadence: Literal["annual", "monthly", "one_time"] | None = None
    proration_rule: str | None = Field(default=None, max_length=80)
    assertion_ids: list[str] = Field(default_factory=list)
    reviewed: bool = False

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in SUPPORTED_CURRENCIES:
            raise ValueError(
                "Currency is not supported; choose one of: "
                + ", ".join(sorted(SUPPORTED_CURRENCIES))
            )
        return normalized

    @field_validator("payment_status", mode="before")
    @classmethod
    def normalize_payment_status(cls, value: Any) -> Any:
        return value.casefold() if isinstance(value, str) else value


class FeeResponse(OrmModel):
    id: str
    case_revision_id: str
    category: FeeCategory
    amount_minor: int
    currency: str
    service_start: date | None
    service_end: date | None
    obligation_date: date | None
    payment_status: Literal["unknown", "unpaid", "paid"]
    billing_cadence: str | None
    proration_rule: str | None
    assertion_ids: list[str]
    reviewed: bool
    primary_money_assertion_id: str | None
    superseded: bool
    superseded_at: datetime | None
    superseded_by: str | None
    supersede_reason: str | None
    created_at: datetime


class FeeSupersedeRequest(BaseModel):
    expected_case_version: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=3000)


class CalculationRequest(BaseModel):
    as_of_date: date


class CalculationCurrencyResponse(BaseModel):
    currency: str
    documented_remaining_subscription_minor: int
    excluded_non_subscription_minor: int
    unclassified_minor: int
    potential_coverage_min_minor: int
    potential_coverage_max_minor: int


class CalculationLineResponse(BaseModel):
    identifier: str
    treatment: str
    original_amount_minor: int
    remaining_amount_minor: int | None
    currency: str
    formula_identifier: str
    explanation: str


class CalculationResponse(BaseModel):
    id: str
    case_revision_id: str
    as_of_date: date
    engine_version: str
    currencies: list[CalculationCurrencyResponse]
    line_items: list[CalculationLineResponse]
    blocking_findings: list[str]
    assumptions: list[str]
    input_hash: str
    result_hash: str
    created_at: datetime


class ReadinessFindingResponse(OrmModel):
    id: str
    code: str
    title: str
    detail: str
    severity: FindingSeverity
    status: FindingStatus
    version: int
    resolution_reason: str | None
    created_at: datetime
    resolved_at: datetime | None


class FindingResolutionRequest(BaseModel):
    status: Literal[FindingStatus.RESOLVED, FindingStatus.ACCEPTED_RISK]
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=3000)


class ReadinessResponse(BaseModel):
    ready_for_internal_review: bool
    mandatory_assertions_reviewed: bool
    reproducible_calculation_exists: bool
    open_blocking_findings: int
    findings: list[ReadinessFindingResponse]
    disclaimer: str


class ExportRequest(BaseModel):
    kind: ExportKind
    calculation_id: str | None = None


class ExportResponse(OrmModel):
    id: str
    kind: ExportKind
    sha256: str
    generator_version: str
    superseded: bool
    created_at: datetime


class AuditEventResponse(OrmModel):
    id: str
    actor_id: str
    action: str
    object_type: str
    object_id: str
    correlation_id: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    previous_hash: str | None
    event_hash: str
    created_at: datetime


class ExportMetadata(BaseModel):
    id: str
    case_revision_id: str
    calculation_id: str
    kind: ExportKind
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    packet_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval_status: Literal["not_approved", "approved"]
    generator_version: str
    superseded: bool
    content_type: str
    filename: str
    size_bytes: int = Field(ge=1)
    download_url: str
    disclaimer: str
    created_at: datetime


class WorkbenchSnapshotResponse(BaseModel):
    case_detail: CaseDetail
    documents: list[DocumentSummary]
    assertions: list[AssertionResponse]
    fees: list[FeeResponse]
    calculation: CalculationResponse | None
    readiness: ReadinessResponse
    audit_events: list[AuditEventResponse]
    exports: list[ExportMetadata]
