"""Evidence-preserving contract extraction adapters."""

from rescue_desk.extraction.deterministic import (
    DeterministicClauseExtractor,
    deterministic_extract,
)
from rescue_desk.extraction.ollama import (
    ModelOutputValidationError,
    OllamaClauseExtractor,
    OllamaExtractionError,
    extract_with_ollama,
)
from rescue_desk.extraction.pdf import PdfSafetyError, extract_pdf_pages, validate_pdf_bytes
from rescue_desk.extraction.types import (
    ClauseExtractionResult,
    ExtractedAssertion,
    ExtractedPage,
    ExtractionFinding,
    FindingSeverity,
    PageExtractionStatus,
    PdfExtractionResult,
    PdfSafetyResult,
)

__all__ = [
    "ClauseExtractionResult",
    "DeterministicClauseExtractor",
    "ExtractedAssertion",
    "ExtractedPage",
    "ExtractionFinding",
    "FindingSeverity",
    "ModelOutputValidationError",
    "OllamaClauseExtractor",
    "OllamaExtractionError",
    "PageExtractionStatus",
    "PdfExtractionResult",
    "PdfSafetyError",
    "PdfSafetyResult",
    "deterministic_extract",
    "extract_pdf_pages",
    "extract_with_ollama",
    "validate_pdf_bytes",
]
