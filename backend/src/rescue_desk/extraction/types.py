"""Value objects shared by the PDF and clause extraction adapters.

The extraction package deliberately returns plain, immutable dataclasses.  The
application layer can persist them through SQLAlchemy without coupling document
parsing to a database session or trusting an AI response as a database model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None


class PageExtractionStatus(StrEnum):
    """Whether a page contains usable embedded text."""

    EXTRACTED = "extracted"
    NEEDS_OCR = "needs_ocr"


class FindingSeverity(StrEnum):
    """Severity local to extraction, before persistence as a readiness finding."""

    BLOCKING = "blocking"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class PdfSafetyResult:
    """Successful, fail-closed validation metadata for a PDF upload."""

    sha256: str
    size_bytes: int
    page_count: int
    pdf_version: str
    encrypted: bool = False
    has_embedded_files: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    """Text and immutable provenance for one one-indexed PDF page."""

    page_number: int
    text: str
    text_sha256: str
    status: PageExtractionStatus
    extraction_confidence: Decimal
    character_count: int
    image_count: int


@dataclass(frozen=True, slots=True)
class PdfExtractionResult:
    """Evidence-preserving output of a PDF text extraction run."""

    document_sha256: str
    pages: tuple[ExtractedPage, ...]
    page_count: int
    needs_ocr_pages: tuple[int, ...]

    @property
    def needs_ocr(self) -> bool:
        return bool(self.needs_ocr_pages)


@dataclass(frozen=True, slots=True)
class ExtractedAssertion:
    """A proposed contract fact whose evidence can be checked byte-for-byte."""

    semantic_key: str
    raw_value: str
    normalized_value: dict[str, JsonValue]
    display_value: str
    confidence: Decimal
    page_number: int
    quote: str
    char_start: int
    char_end: int
    conflicting: bool = False


@dataclass(frozen=True, slots=True)
class ExtractionFinding:
    """A problem that requires review but is not itself a contract assertion."""

    code: str
    message: str
    severity: FindingSeverity
    page_number: int | None = None
    char_start: int | None = None
    char_end: int | None = None


@dataclass(frozen=True, slots=True)
class ClauseExtractionResult:
    """Proposed assertions plus explicit uncertainty from one extractor."""

    extractor: str
    assertions: tuple[ExtractedAssertion, ...]
    findings: tuple[ExtractionFinding, ...] = ()
    conflicting_semantic_keys: tuple[str, ...] = ()
    model_identifier: str | None = None
    prompt_hash: str | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)
