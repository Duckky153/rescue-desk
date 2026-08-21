from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from rescue_desk.extraction import (
    PageExtractionStatus,
    PdfSafetyError,
    extract_pdf_pages,
    validate_pdf_bytes,
)


def test_validates_clean_pdf_and_returns_immutable_metadata(
    fixture_dir: Path, ground_truth: dict[str, Any]
) -> None:
    path = fixture_dir / "clean_standard.pdf"
    data = path.read_bytes()
    result = validate_pdf_bytes(data, max_bytes=10_000_000)

    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert result.sha256 == ground_truth["fixtures"][path.name]["sha256"]
    assert result.size_bytes == len(data)
    assert result.page_count == 1
    assert result.pdf_version in {"1.3", "1.4", "1.5", "1.6", "1.7"}
    assert result.encrypted is False
    assert result.has_embedded_files is False
    assert result.warnings == ()


@pytest.mark.parametrize(
    ("filename", "expected_code"),
    [
        ("safety_encrypted.pdf", "encrypted_pdf"),
        ("safety_embedded.pdf", "embedded_files"),
        ("safety_invalid_header.pdf", "invalid_header"),
    ],
)
def test_rejects_unsafe_fixture(fixture_dir: Path, filename: str, expected_code: str) -> None:
    with pytest.raises(PdfSafetyError) as captured:
        validate_pdf_bytes((fixture_dir / filename).read_bytes(), max_bytes=10_000_000)
    assert captured.value.code == expected_code


def test_rejects_size_before_parsing(fixture_dir: Path) -> None:
    data = (fixture_dir / "clean_standard.pdf").read_bytes()
    with pytest.raises(PdfSafetyError) as captured:
        validate_pdf_bytes(data, max_bytes=len(data) - 1)
    assert captured.value.code == "file_too_large"


@pytest.mark.parametrize("max_bytes", [0, -1])
def test_rejects_invalid_configured_limit(max_bytes: int) -> None:
    with pytest.raises(ValueError, match="max_bytes must be positive"):
        validate_pdf_bytes(b"%PDF-1.7\ninvalid", max_bytes=max_bytes)


def test_rejects_pdf_header_with_unparseable_body() -> None:
    with pytest.raises(PdfSafetyError) as captured:
        validate_pdf_bytes(b"%PDF-1.7\nthis is not a PDF body", max_bytes=1_000)
    assert captured.value.code == "malformed_pdf"


def test_extracts_exact_page_text_hashes(fixture_dir: Path) -> None:
    data = (fixture_dir / "clean_standard.pdf").read_bytes()
    result = extract_pdf_pages(data)

    assert result.document_sha256 == hashlib.sha256(data).hexdigest()
    assert result.page_count == 1
    assert result.needs_ocr is False
    assert result.needs_ocr_pages == ()
    page = result.pages[0]
    assert page.page_number == 1
    assert page.status is PageExtractionStatus.EXTRACTED
    assert page.extraction_confidence.as_tuple().exponent == -4
    assert page.text_sha256 == hashlib.sha256(page.text.encode("utf-8")).hexdigest()
    assert page.character_count == len(page.text)
    assert "Annual Subscription Fee: USD $120,000.00" in page.text


def test_classifies_textless_scan_as_needing_ocr(fixture_dir: Path) -> None:
    result = extract_pdf_pages((fixture_dir / "scan_needs_ocr.pdf").read_bytes())
    assert result.needs_ocr is True
    assert result.needs_ocr_pages == (1,)
    assert result.pages[0].status is PageExtractionStatus.NEEDS_OCR
    assert result.pages[0].extraction_confidence < 1


@pytest.mark.parametrize("filename", ["safety_encrypted.pdf", "safety_embedded.pdf"])
def test_direct_extraction_repeats_structural_safety_checks(
    fixture_dir: Path, filename: str
) -> None:
    with pytest.raises(PdfSafetyError):
        extract_pdf_pages((fixture_dir / filename).read_bytes())


def test_direct_extraction_rejects_non_pdf() -> None:
    with pytest.raises(PdfSafetyError) as captured:
        extract_pdf_pages(b"plain text, not PDF")
    assert captured.value.code == "invalid_header"


def _pdf_with_external_link_action() -> bytes:
    document = pymupdf.open()
    try:
        page = document.new_page()
        page.insert_text((72, 72), "Contract evidence with a clickable external destination.")
        page.insert_link(
            {
                "kind": pymupdf.LINK_URI,
                "from": pymupdf.Rect(70, 60, 360, 90),
                "uri": "https://attacker.invalid/collect",
            }
        )
        return document.tobytes()
    finally:
        document.close()


def _pdf_with_document_open_javascript() -> bytes:
    document = pymupdf.open()
    try:
        document.new_page()
        action_xref = document.get_new_xref()
        document.update_object(action_xref, "<< /S /JavaScript /JS (app.alert\\(1\\)) >>")
        document.xref_set_key(document.pdf_catalog(), "OpenAction", f"{action_xref} 0 R")
        return document.tobytes()
    finally:
        document.close()


def test_rejects_active_annotation_actions_during_validation_and_extraction() -> None:
    data = _pdf_with_external_link_action()

    with pytest.raises(PdfSafetyError) as validation_error:
        validate_pdf_bytes(data, max_bytes=10_000_000)
    assert validation_error.value.code == "active_content"

    with pytest.raises(PdfSafetyError) as extraction_error:
        extract_pdf_pages(data)
    assert extraction_error.value.code == "active_content"


def test_rejects_document_open_javascript() -> None:
    with pytest.raises(PdfSafetyError) as captured:
        validate_pdf_bytes(_pdf_with_document_open_javascript(), max_bytes=10_000_000)
    assert captured.value.code == "active_content"


def _generate(project_root: Path, destination: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "generate_contract_fixtures.py"),
            "--output",
            str(destination),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=project_root,
    )


def test_fixture_generator_is_reproducible(project_root: Path, tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_run = _generate(project_root, first)
    second_run = _generate(project_root, second)

    assert first_run.returncode == 0, first_run.stderr
    assert second_run.returncode == 0, second_run.stderr
    first_truth = json.loads((first / "ground_truth.json").read_text(encoding="utf-8"))
    second_truth = json.loads((second / "ground_truth.json").read_text(encoding="utf-8"))
    assert first_truth == second_truth
    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in first.glob("*.pdf")
    } == {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in second.glob("*.pdf")}


def test_ground_truth_hashes_cover_every_pdf(
    fixture_dir: Path, ground_truth: dict[str, Any]
) -> None:
    expected_names = set(ground_truth["fixtures"])
    actual_names = {path.name for path in fixture_dir.glob("*.pdf")}
    assert actual_names == expected_names
    for name, record in ground_truth["fixtures"].items():
        data = (fixture_dir / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == record["sha256"]
        assert len(data) == record["size_bytes"]
