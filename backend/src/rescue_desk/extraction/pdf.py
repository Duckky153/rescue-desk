"""Fail-closed PDF safety checks and evidence-preserving text extraction."""

from __future__ import annotations

import hashlib
import re
from contextlib import closing
from decimal import Decimal

import pymupdf

from rescue_desk.extraction.types import (
    ExtractedPage,
    PageExtractionStatus,
    PdfExtractionResult,
    PdfSafetyResult,
)

_PDF_HEADER = re.compile(rb"^%PDF-(?P<version>\d\.\d)(?:\r\n|\r|\n)")
_ACTIVE_CONTENT_KEY = re.compile(r"/(?:AA|JavaScript|JS|OpenAction|URI|XFA)\b")
_ACTIVE_ACTION_SUBTYPE = re.compile(
    r"/S\s*/(?:GoTo3DView|GoToE|GoToR|Hide|ImportData|JavaScript|Launch|Movie|Named|"
    r"Rendition|ResetForm|SetOCGState|Sound|SubmitForm|Thread|Trans|URI)\b"
)
_ACTIVE_ANNOTATION_SUBTYPE = re.compile(
    r"/Subtype\s*/(?:3D|FileAttachment|Movie|RichMedia|Screen|Sound)\b"
)
_MINIMUM_PDF_BYTES = 16
_MAX_PAGE_COUNT = 500


class PdfSafetyError(ValueError):
    """A rejected PDF with a stable category safe to expose to a client."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _open_pdf(data: bytes) -> pymupdf.Document:
    try:
        return pymupdf.open(stream=data, filetype="pdf")  # type: ignore[no-untyped-call]
    except (RuntimeError, ValueError) as exc:
        raise PdfSafetyError("malformed_pdf", "The file cannot be parsed as a PDF") from exc


def _embedded_file_count(document: pymupdf.Document) -> int:
    try:
        return document.embfile_count()
    except (RuntimeError, ValueError) as exc:
        raise PdfSafetyError(
            "malformed_pdf", "The PDF attachment catalog could not be inspected"
        ) from exc


def _assert_no_active_content(document: pymupdf.Document) -> None:
    """Reject PDF actions instead of trusting them inside the evidence viewer.

    PyMuPDF renders pages without executing actions, but the stored original can
    also be opened by a browser or desktop reader. Inspecting every object catches
    document-open JavaScript, external links, launch actions, and interactive
    annotations before the file crosses that trust boundary.
    """

    try:
        for xref in range(1, document.xref_length()):  # type: ignore[no-untyped-call]
            object_source = document.xref_object(  # type: ignore[no-untyped-call]
                xref, compressed=False
            )
            if any(
                pattern.search(object_source)
                for pattern in (
                    _ACTIVE_CONTENT_KEY,
                    _ACTIVE_ACTION_SUBTYPE,
                    _ACTIVE_ANNOTATION_SUBTYPE,
                )
            ):
                raise PdfSafetyError(
                    "active_content",
                    "PDFs containing interactive actions or active content are not accepted",
                )
    except PdfSafetyError:
        raise
    except (RuntimeError, ValueError) as exc:
        raise PdfSafetyError(
            "malformed_pdf", "The PDF action catalog could not be inspected"
        ) from exc


def _assert_safe_structure(document: pymupdf.Document) -> None:
    if document.needs_pass:
        raise PdfSafetyError(
            "encrypted_pdf", "Password-protected or encrypted PDFs are not accepted"
        )
    if _embedded_file_count(document) > 0:
        raise PdfSafetyError(
            "embedded_files", "PDFs containing embedded files or attachments are not accepted"
        )
    _assert_no_active_content(document)
    if document.page_count < 1:
        raise PdfSafetyError("empty_pdf", "The PDF does not contain any pages")
    if document.page_count > _MAX_PAGE_COUNT:
        raise PdfSafetyError(
            "too_many_pages", f"The PDF exceeds the {_MAX_PAGE_COUNT}-page safety limit"
        )


def validate_pdf_bytes(data: bytes, max_bytes: int) -> PdfSafetyResult:
    """Validate a PDF upload before storage or text extraction.

    Validation intentionally rejects encryption and embedded files instead of
    attempting to unlock or unpack them.  The function raises ``PdfSafetyError``
    on every rejected input and returns metadata only for a safe structure.
    """

    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    if len(data) > max_bytes:
        raise PdfSafetyError(
            "file_too_large", f"The PDF exceeds the configured {max_bytes}-byte upload limit"
        )
    if len(data) < _MINIMUM_PDF_BYTES:
        raise PdfSafetyError("invalid_header", "The file is too short to be a valid PDF")
    header = _PDF_HEADER.match(data[:16])
    if header is None:
        raise PdfSafetyError("invalid_header", "The file does not begin with a valid PDF header")

    with closing(_open_pdf(data)) as document:
        _assert_safe_structure(document)
        return PdfSafetyResult(
            sha256=_sha256_bytes(data),
            size_bytes=len(data),
            page_count=document.page_count,
            pdf_version=header.group("version").decode("ascii"),
        )


def _printable_ratio(text: str) -> Decimal:
    if not text:
        return Decimal("0")
    printable = sum(character.isprintable() or character in "\n\r\t" for character in text)
    return Decimal(printable) / Decimal(len(text))


def _classify_page(text: str, image_count: int) -> tuple[PageExtractionStatus, Decimal]:
    meaningful_text = "".join(character for character in text if character.isalnum())
    printable_ratio = _printable_ratio(text)
    if len(meaningful_text) < 24:
        return PageExtractionStatus.NEEDS_OCR, Decimal("0.1000")
    if image_count > 0 and len(meaningful_text) < 80:
        return PageExtractionStatus.NEEDS_OCR, Decimal("0.3500")
    if printable_ratio < Decimal("0.85"):
        return PageExtractionStatus.NEEDS_OCR, Decimal("0.5000")
    return PageExtractionStatus.EXTRACTED, Decimal("1.0000")


def extract_pdf_pages(data: bytes) -> PdfExtractionResult:
    """Extract one-indexed page text with hashes and OCR-needed classification.

    Callers normally run :func:`validate_pdf_bytes` first so their configured
    byte limit is enforced.  This function repeats all structural safety checks,
    making direct use fail closed as well.
    """

    if _PDF_HEADER.match(data[:16]) is None:
        raise PdfSafetyError("invalid_header", "The file does not begin with a valid PDF header")

    pages: list[ExtractedPage] = []
    needs_ocr: list[int] = []
    with closing(_open_pdf(data)) as document:
        _assert_safe_structure(document)
        for page_index in range(document.page_count):
            index = page_index + 1
            page: pymupdf.Page = document.load_page(page_index)  # type: ignore[no-untyped-call]
            try:
                text = page.get_text("text", sort=True)  # type: ignore[no-untyped-call]
                image_count = len(page.get_images(full=True))  # type: ignore[no-untyped-call]
            except RuntimeError as exc:
                raise PdfSafetyError(
                    "page_extraction_failed", f"Page {index} could not be extracted safely"
                ) from exc
            status, confidence = _classify_page(text, image_count)
            if status is PageExtractionStatus.NEEDS_OCR:
                needs_ocr.append(index)
            pages.append(
                ExtractedPage(
                    page_number=index,
                    text=text,
                    text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    status=status,
                    extraction_confidence=confidence,
                    character_count=len(text),
                    image_count=image_count,
                )
            )

    return PdfExtractionResult(
        document_sha256=_sha256_bytes(data),
        pages=tuple(pages),
        page_count=len(pages),
        needs_ocr_pages=tuple(needs_ocr),
    )
