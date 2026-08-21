from __future__ import annotations

import enum
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from rescue_desk.database import Base
from rescue_desk.domain.money import MAX_MINOR_UNITS, SUPPORTED_CURRENCIES

_SUPPORTED_CURRENCIES_SQL = ", ".join(f"'{value}'" for value in sorted(SUPPORTED_CURRENCIES))


def utc_now() -> datetime:
    return datetime.now(UTC)


def uuid_string() -> str:
    return str(uuid.uuid4())


class Role(enum.StrEnum):
    UPLOADER = "uploader"
    ANALYST = "analyst"
    APPROVER = "approver"
    AUDITOR = "auditor"
    ADMIN = "admin"


class CaseStatus(enum.StrEnum):
    DRAFT = "draft"
    EVIDENCE_REVIEW = "evidence_review"
    READY_FOR_INTERNAL_REVIEW = "ready_for_internal_review"
    INTERNAL_PACKET_APPROVED = "internal_packet_approved"
    EXPORTED = "exported"
    ARCHIVED = "archived"


class SafetyStatus(enum.StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    REJECTED = "rejected"


class ProcessingStatus(enum.StrEnum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    PROCESSED = "processed"
    NEEDS_OCR = "needs_ocr"
    FAILED = "failed"


class ExtractionRunStatus(enum.StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AssertionSource(enum.StrEnum):
    DETERMINISTIC = "deterministic"
    AI = "ai"
    HUMAN = "human"
    DERIVED = "derived"


class AssertionReviewState(enum.StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    CORRECTED = "corrected"
    REJECTED = "rejected"
    CONFLICTING = "conflicting"


class ReviewDecisionType(enum.StrEnum):
    ACCEPT = "accept"
    CORRECT = "correct"
    REJECT = "reject"


class FeeCategory(enum.StrEnum):
    SUBSCRIPTION = "subscription"
    TAX = "tax"
    PENALTY = "penalty"
    PROFESSIONAL_SERVICE = "professional_service"
    IMPLEMENTATION = "implementation"
    TERMINATION = "termination"
    OTHER = "other"
    UNCLASSIFIED = "unclassified"


class FindingSeverity(enum.StrEnum):
    BLOCKING = "blocking"
    WARNING = "warning"
    INFO = "info"


class FindingStatus(enum.StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    ACCEPTED_RISK = "accepted_risk"


class ExportKind(enum.StrEnum):
    INTERNAL_REVIEW_PDF = "internal_review_pdf"
    CUSTOMER_EXPLANATION_PDF = "customer_explanation_pdf"
    EVIDENCE_CSV = "evidence_csv"
    MACHINE_READABLE_JSON = "machine_readable_json"


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(160))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("organization_id", "user_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[Role] = mapped_column(Enum(Role, native_enum=False))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class RescueCase(Base):
    __tablename__ = "rescue_cases"
    __table_args__ = (
        CheckConstraint("current_revision_number >= 1", name="ck_rescue_case_revision_positive"),
        CheckConstraint("version >= 1", name="ck_rescue_case_version_positive"),
        Index("ix_rescue_cases_org_status", "organization_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    display_name: Mapped[str] = mapped_column(String(180))
    applicant_company: Mapped[str] = mapped_column(String(180))
    erp_provider: Mapped[str] = mapped_column(String(120))
    assigned_analyst_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    assigned_approver_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[CaseStatus] = mapped_column(
        Enum(CaseStatus, native_enum=False), default=CaseStatus.DRAFT
    )
    current_revision_number: Mapped[int] = mapped_column(Integer, default=1)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    revisions: Mapped[list[CaseRevision]] = relationship(
        back_populates="case", cascade="all, delete-orphan", order_by="CaseRevision.number"
    )


class CaseRevision(Base):
    __tablename__ = "case_revisions"
    __table_args__ = (
        CheckConstraint("number >= 1", name="ck_case_revision_number_positive"),
        CheckConstraint(
            "snapshot_hash IS NULL OR length(snapshot_hash) = 64",
            name="ck_case_revision_snapshot_hash_length",
        ),
        UniqueConstraint("case_id", "number"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    case_id: Mapped[str] = mapped_column(
        ForeignKey("rescue_cases.id", ondelete="CASCADE"), index=True
    )
    number: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    previous_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("case_revisions.id", ondelete="SET NULL"), nullable=True
    )
    snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    case: Mapped[RescueCase] = relationship(back_populates="revisions")
    documents: Mapped[list[ContractDocument]] = relationship(
        back_populates="case_revision", cascade="all, delete-orphan"
    )


class ContractDocument(Base):
    __tablename__ = "contract_documents"
    __table_args__ = (
        CheckConstraint("size_bytes > 0", name="ck_contract_document_size_positive"),
        CheckConstraint("page_count >= 1", name="ck_contract_document_pages_positive"),
        CheckConstraint("length(sha256) = 64", name="ck_contract_document_sha_length"),
        CheckConstraint(
            "source_type IN ('synthetic', 'public', 'redacted')",
            name="ck_contract_document_source_type",
        ),
        UniqueConstraint("organization_id", "sha256", "case_revision_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    case_revision_id: Mapped[str] = mapped_column(
        ForeignKey("case_revisions.id", ondelete="CASCADE"), index=True
    )
    original_filename: Mapped[str] = mapped_column(String(255))
    stored_filename: Mapped[str] = mapped_column(String(255), unique=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    media_type: Mapped[str] = mapped_column(String(100))
    source_type: Mapped[str] = mapped_column(String(30))
    size_bytes: Mapped[int] = mapped_column(Integer)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    safety_status: Mapped[SafetyStatus] = mapped_column(
        Enum(SafetyStatus, native_enum=False), default=SafetyStatus.PENDING
    )
    processing_status: Mapped[ProcessingStatus] = mapped_column(
        Enum(ProcessingStatus, native_enum=False), default=ProcessingStatus.UPLOADED
    )
    processing_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    superseded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    case_revision: Mapped[CaseRevision] = relationship(back_populates="documents")
    pages: Mapped[list[DocumentPage]] = relationship(
        back_populates="document", cascade="all, delete-orphan", order_by="DocumentPage.page_number"
    )
    extraction_runs: Mapped[list[ExtractionRun]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class DocumentPage(Base):
    __tablename__ = "document_pages"
    __table_args__ = (
        CheckConstraint("page_number >= 1", name="ck_document_page_number_positive"),
        CheckConstraint("length(text_sha256) = 64", name="ck_document_page_text_hash_length"),
        CheckConstraint(
            "extraction_confidence >= 0 AND extraction_confidence <= 1",
            name="ck_document_page_confidence_range",
        ),
        UniqueConstraint("document_id", "page_number"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("contract_documents.id", ondelete="CASCADE"), index=True
    )
    page_number: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    text_sha256: Mapped[str] = mapped_column(String(64))
    extraction_confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=Decimal("1"))

    document: Mapped[ContractDocument] = relationship(back_populates="pages")
    evidence_spans: Mapped[list[EvidenceSpan]] = relationship(
        back_populates="page", cascade="all, delete-orphan"
    )


class ExtractionRun(Base):
    __tablename__ = "extraction_runs"
    __table_args__ = (
        CheckConstraint(
            "prompt_hash IS NULL OR length(prompt_hash) = 64",
            name="ck_extraction_run_prompt_hash_length",
        ),
        CheckConstraint(
            "structured_output_hash IS NULL OR length(structured_output_hash) = 64",
            name="ck_extraction_run_output_hash_length",
        ),
        CheckConstraint(
            "(status = 'RUNNING' AND finished_at IS NULL) OR "
            "(status = 'SUCCEEDED' AND finished_at IS NOT NULL "
            "AND structured_output_hash IS NOT NULL AND error_category IS NULL) OR "
            "(status = 'FAILED' AND finished_at IS NOT NULL AND error_category IS NOT NULL)",
            name="ck_extraction_run_status_lifecycle",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("contract_documents.id", ondelete="CASCADE"), index=True
    )
    schema_version: Mapped[str] = mapped_column(String(30))
    extractor: Mapped[str] = mapped_column(String(120))
    model_identifier: Mapped[str | None] = mapped_column(String(160), nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    code_version: Mapped[str] = mapped_column(String(80))
    status: Mapped[ExtractionRunStatus] = mapped_column(
        Enum(ExtractionRunStatus, native_enum=False)
    )
    structured_output_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(80), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    document: Mapped[ContractDocument] = relationship(back_populates="extraction_runs")
    assertions: Mapped[list[ContractAssertion]] = relationship(
        back_populates="extraction_run", cascade="all, delete-orphan"
    )


class EvidenceSpan(Base):
    __tablename__ = "evidence_spans"
    __table_args__ = (
        CheckConstraint("char_start >= 0", name="ck_evidence_span_start_nonnegative"),
        CheckConstraint("char_end > char_start", name="ck_evidence_span_order"),
        CheckConstraint("length(quote_sha256) = 64", name="ck_evidence_span_quote_hash_length"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    page_id: Mapped[str] = mapped_column(ForeignKey("document_pages.id", ondelete="CASCADE"))
    quote: Mapped[str] = mapped_column(Text)
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    quote_sha256: Mapped[str] = mapped_column(String(64))

    page: Mapped[DocumentPage] = relationship(back_populates="evidence_spans")
    assertions: Mapped[list[ContractAssertion]] = relationship(
        secondary="assertion_evidence_links", back_populates="evidence"
    )

    @property
    def page_number(self) -> int:
        return self.page.page_number

    @property
    def document_id(self) -> str:
        return self.page.document_id


class AssertionEvidenceLink(Base):
    __tablename__ = "assertion_evidence_links"

    assertion_id: Mapped[str] = mapped_column(
        ForeignKey("contract_assertions.id", ondelete="CASCADE"), primary_key=True
    )
    evidence_id: Mapped[str] = mapped_column(
        ForeignKey("evidence_spans.id", ondelete="CASCADE"), primary_key=True
    )


class ContractAssertion(Base):
    __tablename__ = "contract_assertions"
    __table_args__ = (
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_contract_assertion_confidence_range",
        ),
        CheckConstraint("version >= 1", name="ck_contract_assertion_version_positive"),
        UniqueConstraint("case_revision_id", "semantic_key", "version"),
        Index("ix_assertions_revision_current", "case_revision_id", "is_current"),
        Index(
            "uq_contract_assertion_current_semantic",
            "case_revision_id",
            "semantic_key",
            unique=True,
            postgresql_where=text("is_current"),
            sqlite_where=text("is_current = 1"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    case_revision_id: Mapped[str] = mapped_column(
        ForeignKey("case_revisions.id", ondelete="CASCADE"), index=True
    )
    extraction_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="SET NULL"), nullable=True
    )
    semantic_key: Mapped[str] = mapped_column(String(100), index=True)
    raw_value: Mapped[str] = mapped_column(Text)
    normalized_value: Mapped[dict[str, Any]] = mapped_column(JSON)
    display_value: Mapped[str] = mapped_column(Text)
    source: Mapped[AssertionSource] = mapped_column(Enum(AssertionSource, native_enum=False))
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    review_state: Mapped[AssertionReviewState] = mapped_column(
        Enum(AssertionReviewState, native_enum=False), default=AssertionReviewState.PROPOSED
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    supersedes_assertion_id: Mapped[str | None] = mapped_column(
        ForeignKey("contract_assertions.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    extraction_run: Mapped[ExtractionRun | None] = relationship(back_populates="assertions")
    evidence: Mapped[list[EvidenceSpan]] = relationship(
        secondary="assertion_evidence_links", back_populates="assertions"
    )
    decisions: Mapped[list[ReviewDecision]] = relationship(
        back_populates="assertion", cascade="all, delete-orphan"
    )


class ReviewDecision(Base):
    __tablename__ = "review_decisions"
    __table_args__ = (
        CheckConstraint(
            "assumption IS FALSE OR decision = 'CORRECT'",
            name="ck_review_decision_assumption_type",
        ),
        CheckConstraint(
            "(decision = 'CORRECT' AND corrected_value IS NOT NULL) OR "
            "(decision IN ('ACCEPT', 'REJECT') AND corrected_value IS NULL)",
            name="ck_review_decision_correction_value",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    assertion_id: Mapped[str] = mapped_column(
        ForeignKey("contract_assertions.id", ondelete="CASCADE"), index=True
    )
    decision: Mapped[ReviewDecisionType] = mapped_column(
        Enum(ReviewDecisionType, native_enum=False)
    )
    reviewer_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    reason: Mapped[str] = mapped_column(Text)
    corrected_value: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    assumption: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    assertion: Mapped[ContractAssertion] = relationship(back_populates="decisions")


class FeeObligation(Base):
    __tablename__ = "fee_obligations"
    __table_args__ = (
        CheckConstraint(
            f"amount_minor >= 0 AND amount_minor <= {MAX_MINOR_UNITS}",
            name="ck_fee_amount_supported_range",
        ),
        CheckConstraint(
            f"currency IN ({_SUPPORTED_CURRENCIES_SQL})",
            name="ck_fee_currency_supported",
        ),
        CheckConstraint(
            "payment_status IN ('unknown', 'unpaid', 'paid')",
            name="ck_fee_payment_status_supported",
        ),
        CheckConstraint(
            "billing_cadence IS NULL OR billing_cadence IN ('annual', 'monthly', 'one_time')",
            name="ck_fee_billing_cadence_supported",
        ),
        CheckConstraint(
            "reviewed IS FALSE OR billing_cadence IS NOT NULL",
            name="ck_fee_reviewed_cadence_required",
        ),
        CheckConstraint(
            "service_start IS NULL OR service_end IS NULL OR service_end > service_start",
            name="ck_fee_service_date_order",
        ),
        CheckConstraint(
            "(reviewed IS TRUE AND primary_money_assertion_id IS NOT NULL) OR "
            "(reviewed IS FALSE AND primary_money_assertion_id IS NULL)",
            name="ck_fee_reviewed_money_evidence",
        ),
        CheckConstraint(
            "(superseded IS TRUE AND superseded_at IS NOT NULL "
            "AND superseded_by IS NOT NULL AND supersede_reason IS NOT NULL) OR "
            "(superseded IS FALSE AND superseded_at IS NULL "
            "AND superseded_by IS NULL AND supersede_reason IS NULL)",
            name="ck_fee_supersede_lifecycle",
        ),
        Index(
            "uq_active_fee_money_assertion",
            "case_revision_id",
            "primary_money_assertion_id",
            unique=True,
            postgresql_where=text("superseded IS FALSE AND primary_money_assertion_id IS NOT NULL"),
            sqlite_where=text("superseded = 0 AND primary_money_assertion_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    case_revision_id: Mapped[str] = mapped_column(
        ForeignKey("case_revisions.id", ondelete="CASCADE"), index=True
    )
    category: Mapped[FeeCategory] = mapped_column(Enum(FeeCategory, native_enum=False))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    service_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    service_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    obligation_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    payment_status: Mapped[str] = mapped_column(String(30), default="unknown")
    billing_cadence: Mapped[str | None] = mapped_column(String(30), nullable=True)
    proration_rule: Mapped[str | None] = mapped_column(String(80), nullable=True)
    assertion_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    primary_money_assertion_id: Mapped[str | None] = mapped_column(
        ForeignKey("contract_assertions.id", ondelete="RESTRICT"), nullable=True
    )
    superseded: Mapped[bool] = mapped_column(Boolean, default=False)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    supersede_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CalculationRun(Base):
    __tablename__ = "calculation_runs"
    __table_args__ = (
        CheckConstraint("length(input_hash) = 64", name="ck_calculation_input_hash_length"),
        CheckConstraint("length(result_hash) = 64", name="ck_calculation_result_hash_length"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    case_revision_id: Mapped[str] = mapped_column(
        ForeignKey("case_revisions.id", ondelete="CASCADE"), index=True
    )
    as_of_date: Mapped[date] = mapped_column(Date)
    engine_version: Mapped[str] = mapped_column(String(40))
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    input_hash: Mapped[str] = mapped_column(String(64))
    assumptions: Mapped[list[str]] = mapped_column(JSON, default=list)
    result_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    result_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    line_items: Mapped[list[CalculationLineItem]] = relationship(
        back_populates="calculation", cascade="all, delete-orphan"
    )


class CalculationLineItem(Base):
    __tablename__ = "calculation_line_items"
    __table_args__ = (
        CheckConstraint(
            f"original_amount_minor >= 0 AND original_amount_minor <= {MAX_MINOR_UNITS}",
            name="ck_calculation_line_original_range",
        ),
        CheckConstraint(
            f"remaining_amount_minor IS NULL OR "
            f"(remaining_amount_minor >= 0 AND remaining_amount_minor <= {MAX_MINOR_UNITS})",
            name="ck_calculation_line_remaining_range",
        ),
        CheckConstraint(
            f"currency IN ({_SUPPORTED_CURRENCIES_SQL})",
            name="ck_calculation_line_currency_supported",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    calculation_id: Mapped[str] = mapped_column(
        ForeignKey("calculation_runs.id", ondelete="CASCADE"), index=True
    )
    fee_obligation_id: Mapped[str | None] = mapped_column(
        ForeignKey("fee_obligations.id", ondelete="SET NULL"), nullable=True
    )
    treatment: Mapped[str] = mapped_column(String(30))
    original_amount_minor: Mapped[int] = mapped_column(BigInteger)
    remaining_amount_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str] = mapped_column(String(3))
    formula_identifier: Mapped[str] = mapped_column(String(80))
    explanation: Mapped[str] = mapped_column(Text)

    calculation: Mapped[CalculationRun] = relationship(back_populates="line_items")


class ReadinessFinding(Base):
    __tablename__ = "readiness_findings"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_readiness_finding_version_positive"),
        CheckConstraint(
            "(status = 'OPEN' AND resolved_at IS NULL AND resolution_reason IS NULL) OR "
            "(status IN ('RESOLVED', 'ACCEPTED_RISK') AND resolved_at IS NOT NULL "
            "AND resolution_reason IS NOT NULL)",
            name="ck_readiness_finding_resolution_lifecycle",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    case_revision_id: Mapped[str] = mapped_column(
        ForeignKey("case_revisions.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(180))
    detail: Mapped[str] = mapped_column(Text)
    severity: Mapped[FindingSeverity] = mapped_column(Enum(FindingSeverity, native_enum=False))
    status: Mapped[FindingStatus] = mapped_column(
        Enum(FindingStatus, native_enum=False), default=FindingStatus.OPEN
    )
    resolution_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExportArtifact(Base):
    __tablename__ = "export_artifacts"
    __table_args__ = (
        CheckConstraint("length(sha256) = 64", name="ck_export_artifact_sha_length"),
        CheckConstraint(
            "length(packet_snapshot_sha256) = 64",
            name="ck_export_packet_snapshot_hash_length",
        ),
        CheckConstraint(
            "length(revision_snapshot_sha256) = 64",
            name="ck_export_revision_snapshot_hash_length",
        ),
        CheckConstraint(
            "approval_status IN ('not_approved', 'approved')",
            name="ck_export_approval_status_supported",
        ),
        CheckConstraint(
            "(approval_status = 'approved' AND approval_case_version >= 1 "
            "AND approved_by IS NOT NULL) OR "
            "(approval_status = 'not_approved' AND approval_case_version IS NULL "
            "AND approved_by IS NULL)",
            name="ck_export_approval_consistency",
        ),
        Index(
            "uq_export_current_revision_kind",
            "case_revision_id",
            "kind",
            unique=True,
            postgresql_where=text("superseded IS FALSE"),
            sqlite_where=text("superseded = 0"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    case_revision_id: Mapped[str] = mapped_column(
        ForeignKey("case_revisions.id", ondelete="CASCADE"), index=True
    )
    calculation_id: Mapped[str | None] = mapped_column(
        ForeignKey("calculation_runs.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[ExportKind] = mapped_column(Enum(ExportKind, native_enum=False))
    stored_filename: Mapped[str] = mapped_column(String(255), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    packet_snapshot_sha256: Mapped[str] = mapped_column(String(64))
    revision_snapshot_sha256: Mapped[str] = mapped_column(String(64))
    approval_status: Mapped[str] = mapped_column(String(20))
    approval_case_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    prepared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    prepared_by: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    generator_version: Mapped[str] = mapped_column(String(40))
    superseded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(
            "length(correlation_id) >= 1 AND length(correlation_id) <= 64",
            name="ck_audit_correlation_id_length",
        ),
        CheckConstraint("length(event_hash) = 64", name="ck_audit_event_hash_length"),
        CheckConstraint(
            "previous_hash IS NULL OR length(previous_hash) = 64",
            name="ck_audit_previous_hash_length",
        ),
        Index("ix_audit_org_created", "organization_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    action: Mapped[str] = mapped_column(String(100))
    object_type: Mapped[str] = mapped_column(String(80))
    object_id: Mapped[str] = mapped_column(String(36))
    correlation_id: Mapped[str] = mapped_column(String(64))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    previous_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        CheckConstraint(
            "response_status = 0 OR (response_status >= 100 AND response_status <= 599)",
            name="ck_idempotency_response_status",
        ),
        CheckConstraint("length(request_hash) = 64", name="ck_idempotency_request_hash_length"),
        UniqueConstraint("organization_id", "endpoint", "key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    endpoint: Mapped[str] = mapped_column(String(160))
    key: Mapped[str] = mapped_column(String(160))
    request_hash: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
