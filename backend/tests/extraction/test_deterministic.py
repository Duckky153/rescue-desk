from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from rescue_desk.domain.money import CURRENCY_EXPONENTS
from rescue_desk.extraction import (
    ExtractedPage,
    PageExtractionStatus,
    deterministic_extract,
    extract_pdf_pages,
)


def _assertion_signature(assertion: Any) -> tuple[object, ...]:
    return (
        assertion.semantic_key,
        json.dumps(assertion.normalized_value, sort_keys=True, separators=(",", ":")),
        assertion.page_number,
        assertion.conflicting,
    )


@pytest.mark.parametrize(
    "filename",
    [
        "clean_standard.pdf",
        "amendment_conflict.pdf",
        "redacted_terms.pdf",
        "prompt_injection.pdf",
        "fee_and_date_variants.pdf",
        "scan_needs_ocr.pdf",
    ],
)
def test_offline_extraction_matches_machine_ground_truth(
    fixture_dir: Path, ground_truth: dict[str, Any], filename: str
) -> None:
    pages = extract_pdf_pages((fixture_dir / filename).read_bytes()).pages
    result = deterministic_extract(pages)
    expected = ground_truth["fixtures"][filename]

    actual_assertions = Counter(_assertion_signature(item) for item in result.assertions)
    expected_assertions = Counter(
        (
            item["semantic_key"],
            json.dumps(item["normalized_value"], sort_keys=True, separators=(",", ":")),
            item["page_number"],
            item["conflicting"],
        )
        for item in expected["expected_assertions"]
    )
    assert actual_assertions == expected_assertions
    assert Counter(finding.code for finding in result.findings) == Counter(
        expected["expected_findings"]
    )
    assert list(result.conflicting_semantic_keys) == expected["expected_conflicts"]


@pytest.mark.parametrize(
    "filename",
    [
        "clean_standard.pdf",
        "amendment_conflict.pdf",
        "redacted_terms.pdf",
        "prompt_injection.pdf",
        "fee_and_date_variants.pdf",
    ],
)
def test_every_assertion_has_exact_contiguous_page_evidence(
    fixture_dir: Path, filename: str
) -> None:
    pages = extract_pdf_pages((fixture_dir / filename).read_bytes()).pages
    pages_by_number = {page.page_number: page for page in pages}
    result = deterministic_extract(pages)

    for assertion in result.assertions:
        page = pages_by_number[assertion.page_number]
        assert page.text[assertion.char_start : assertion.char_end] == assertion.quote
        assert assertion.raw_value.casefold() in assertion.quote.casefold()
        assert Decimal("0") <= assertion.confidence <= Decimal("1")


def test_conflicting_amendment_values_are_never_silently_resolved(
    fixture_dir: Path,
) -> None:
    pages = extract_pdf_pages((fixture_dir / "amendment_conflict.pdf").read_bytes()).pages
    result = deterministic_extract(pages)
    fee_values = [
        item.normalized_value["amount_minor"]
        for item in result.assertions
        if item.semantic_key == "fee.subscription"
    ]
    notice_values = [
        item.normalized_value["days"]
        for item in result.assertions
        if item.semantic_key == "renewal.notice_days"
    ]

    assert fee_values == [12_000_000, 14_400_000]
    assert notice_values == [90, 120]
    assert all(
        item.conflicting
        for item in result.assertions
        if item.semantic_key in {"fee.subscription", "renewal.notice_days"}
    )


def test_prompt_injection_amount_is_not_an_assertion(fixture_dir: Path) -> None:
    pages = extract_pdf_pages((fixture_dir / "prompt_injection.pdf").read_bytes()).pages
    result = deterministic_extract(pages)
    amounts = {
        item.normalized_value.get("amount_minor")
        for item in result.assertions
        if item.semantic_key.startswith("fee.")
    }

    assert 9_600_000 in amounts
    assert 100 not in amounts
    assert Counter(finding.code for finding in result.findings)["prompt_injection_text"] == 2


def test_needs_ocr_page_is_not_clause_extracted(fixture_dir: Path) -> None:
    result = deterministic_extract(
        extract_pdf_pages((fixture_dir / "scan_needs_ocr.pdf").read_bytes()).pages
    )
    assert result.assertions == ()
    assert [finding.code for finding in result.findings] == ["page_needs_ocr"]


def test_extraction_is_deterministic_and_stably_ordered(fixture_dir: Path) -> None:
    pages = extract_pdf_pages((fixture_dir / "clean_standard.pdf").read_bytes()).pages
    first = deterministic_extract(pages)
    second = deterministic_extract(pages)

    assert first == second
    assert list(first.assertions) == sorted(
        first.assertions,
        key=lambda item: (item.page_number, item.char_start, item.semantic_key),
    )


def test_negative_auto_renewal_is_not_misclassified(fixture_dir: Path) -> None:
    pages = extract_pdf_pages((fixture_dir / "fee_and_date_variants.pdf").read_bytes()).pages
    result = deterministic_extract(pages)
    auto_renewals = [
        item for item in result.assertions if item.semantic_key == "renewal.auto_renews"
    ]
    assert len(auto_renewals) == 1
    assert auto_renewals[0].normalized_value == {"type": "boolean", "value": False}


def test_identical_duplicate_values_are_not_conflicts() -> None:
    text = "Effective Date: January 1, 2026\nEffective Date: January 1, 2026\n"
    page = ExtractedPage(
        page_number=1,
        text=text,
        text_sha256="0" * 64,
        status=PageExtractionStatus.EXTRACTED,
        extraction_confidence=Decimal("1.0000"),
        character_count=len(text),
        image_count=0,
    )
    result = deterministic_extract((page,))
    assert len(result.assertions) == 2
    assert result.conflicting_semantic_keys == ()
    assert all(not item.conflicting for item in result.assertions)


@pytest.mark.parametrize(("currency", "exponent"), sorted(CURRENCY_EXPONENTS.items()))
def test_every_supported_currency_code_uses_its_canonical_exponent(
    currency: str, exponent: int
) -> None:
    text = f"Implementation Fee: {currency} 1\n"
    page = ExtractedPage(
        page_number=1,
        text=text,
        text_sha256="1" * 64,
        status=PageExtractionStatus.EXTRACTED,
        extraction_confidence=Decimal("1.0000"),
        character_count=len(text),
        image_count=0,
    )

    assertion = deterministic_extract((page,)).assertions[0]
    assert assertion.normalized_value == {
        "type": "money",
        "currency": currency,
        "amount_minor": 10**exponent,
        "cadence": "one_time",
    }


def test_jpy_and_kwd_are_normalized_without_two_decimal_assumption() -> None:
    text = "Annual Subscription Fee: JPY 1,234\nImplementation Fee: KWD 1.234\n"
    page = ExtractedPage(
        page_number=1,
        text=text,
        text_sha256="2" * 64,
        status=PageExtractionStatus.EXTRACTED,
        extraction_confidence=Decimal("1.0000"),
        character_count=len(text),
        image_count=0,
    )

    assertions = deterministic_extract((page,)).assertions
    values = {item.semantic_key: item for item in assertions}
    assert values["fee.subscription"].normalized_value["amount_minor"] == 1_234
    assert values["fee.subscription"].display_value == "JPY 1,234"
    assert values["fee.implementation"].normalized_value["amount_minor"] == 1_234
    assert values["fee.implementation"].display_value == "KWD 1.234"


def test_overprecise_and_ambiguous_money_is_not_extracted_as_evidence() -> None:
    text = (
        "Annual Subscription Fee: JPY 1.50\n"
        "Implementation Fee: KWD 1.2345\n"
        "Termination Fee: $99.00\n"
        "Termination Fee: ¥99.00\n"
    )
    page = ExtractedPage(
        page_number=1,
        text=text,
        text_sha256="3" * 64,
        status=PageExtractionStatus.EXTRACTED,
        extraction_confidence=Decimal("1.0000"),
        character_count=len(text),
        image_count=0,
    )

    result = deterministic_extract((page,))
    assert result.assertions == ()
    assert [item.code for item in result.findings] == [
        "invalid_labelled_money",
        "invalid_labelled_money",
        "invalid_labelled_money",
        "invalid_labelled_money",
    ]
    assert all(item.severity.value == "blocking" for item in result.findings)
    assert all("99.00" not in item.message for item in result.findings)
