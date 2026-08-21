"""Conservative offline clause extraction with exact page evidence.

This is intentionally not a general legal-language interpreter.  It recognizes
explicitly labelled commercial terms, retains their literal evidence, and marks
contradictions for human review instead of guessing which clause controls.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Final

from rescue_desk.domain.evidence import verify_evidence_span
from rescue_desk.domain.money import MAX_MINOR_UNITS, SUPPORTED_CURRENCIES, currency_exponent
from rescue_desk.extraction.types import (
    ClauseExtractionResult,
    ExtractedAssertion,
    ExtractedPage,
    ExtractionFinding,
    FindingSeverity,
    JsonValue,
    PageExtractionStatus,
)

EXTRACTOR_IDENTIFIER: Final = "deterministic-contract-rules-v2"

_DATE_VALUE = (
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},\s+\d{4}|\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2}"
)
_CURRENCY_CODES = "|".join(sorted(SUPPORTED_CURRENCIES, key=lambda item: (-len(item), item)))
# A bare "$", "£", or "¥" is not a currency: each is used by multiple currencies.
# Explicit regional dollar symbols and the euro/Indian-rupee symbols are sufficiently specific.
_UNAMBIGUOUS_CURRENCY_SYMBOLS: Final[dict[str, str]] = {
    "US$": "USD",
    "AU$": "AUD",
    "CA$": "CAD",
    "A$": "AUD",
    "C$": "CAD",
    "€": "EUR",
    "₹": "INR",
}
_KNOWN_SYMBOLS_BY_CURRENCY: Final[dict[str, frozenset[str]]] = {
    "AUD": frozenset({"$", "A$", "AU$"}),
    "CAD": frozenset({"$", "C$", "CA$"}),
    "EUR": frozenset({"€"}),
    "GBP": frozenset({"£"}),
    "INR": frozenset({"₹"}),
    "JPY": frozenset({"¥"}),
    "KRW": frozenset({"₩"}),
    "USD": frozenset({"$", "US$"}),
    "VND": frozenset({"₫"}),
}
_ALL_KNOWN_SYMBOLS = tuple(
    sorted(
        set(_UNAMBIGUOUS_CURRENCY_SYMBOLS).union(
            symbol for symbols in _KNOWN_SYMBOLS_BY_CURRENCY.values() for symbol in symbols
        ),
        key=lambda item: (-len(item), item),
    )
)
_UNAMBIGUOUS_SYMBOL_PATTERN = "|".join(
    re.escape(symbol)
    for symbol in sorted(_UNAMBIGUOUS_CURRENCY_SYMBOLS, key=lambda item: (-len(item), item))
)
_OPTIONAL_SYMBOL_PATTERN = "|".join(re.escape(symbol) for symbol in _ALL_KNOWN_SYMBOLS)
_MONEY_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_MONEY_VALUE = (
    rf"(?:(?:{_CURRENCY_CODES})\s*(?:(?:{_OPTIONAL_SYMBOL_PATTERN})\s*)?"
    rf"|(?:{_UNAMBIGUOUS_SYMBOL_PATTERN})\s*){_MONEY_NUMBER}(?!\d|,\d|\.\d)"
)
_LABELLED_MONEY_CANDIDATE = (
    rf"(?:(?:[A-Z]{{3}})\s*(?:(?:{_OPTIONAL_SYMBOL_PATTERN})\s*)?"
    rf"|(?:{_OPTIONAL_SYMBOL_PATTERN})\s*)?"
    r"[-+]?(?:\d[\d,]*)(?:\.\d+)?(?!\d|,\d|\.\d)"
)
_INJECTION_MARKERS = re.compile(
    r"(?i)(ignore\s+(?:all\s+)?previous\s+instructions|system\s+(?:message|instruction|prompt)|"
    r"assistant\s*:|developer\s+(?:message|instruction)|do\s+not\s+cite|output\s+only\s+json)"
)
_REDACTION_MARKERS = re.compile(r"(?i)(\[\s*redacted\s*\]|_{4,}|█{2,}|<redacted>)")


class DeterministicExtractionError(ValueError):
    """Raised only when internal evidence invariants fail."""


def _date_value(raw: str) -> dict[str, JsonValue]:
    cleaned = re.sub(r"\s+", " ", raw.strip())
    for date_format in ("%B %d, %Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(cleaned, date_format).date()
            return {"type": "date", "value": parsed.isoformat()}
        except ValueError:
            continue
    raise DeterministicExtractionError(f"Unsupported date value: {raw}")


def _money_value(raw: str, cadence: str) -> dict[str, JsonValue]:
    cleaned = re.sub(r"\s+", " ", raw.strip())
    if re.fullmatch(_MONEY_VALUE, cleaned, flags=re.IGNORECASE) is None:
        raise DeterministicExtractionError(f"Unsupported monetary value: {raw}")
    code_match = re.match(rf"(?i)^(?P<currency>{_CURRENCY_CODES})\b", cleaned)
    if code_match is not None:
        currency = code_match.group("currency").upper()
        remainder = cleaned[code_match.end() :].lstrip()
        for symbol in _ALL_KNOWN_SYMBOLS:
            if remainder.startswith(symbol):
                if symbol not in _KNOWN_SYMBOLS_BY_CURRENCY.get(currency, frozenset()):
                    raise DeterministicExtractionError(
                        f"Currency code and symbol disagree in monetary value: {raw}"
                    )
                remainder = remainder[len(symbol) :].lstrip()
                break
    else:
        currency = ""
        remainder = cleaned
        for symbol, symbol_currency in sorted(
            _UNAMBIGUOUS_CURRENCY_SYMBOLS.items(), key=lambda item: (-len(item[0]), item[0])
        ):
            if remainder.startswith(symbol):
                currency = symbol_currency
                remainder = remainder[len(symbol) :].lstrip()
                break
        if not currency:
            raise DeterministicExtractionError(
                f"Monetary value requires an explicit supported currency: {raw}"
            )

    if re.fullmatch(_MONEY_NUMBER, remainder) is None:
        raise DeterministicExtractionError(f"Unsupported monetary value: {raw}")
    numeric = remainder.replace(",", "")
    try:
        decimal_value = Decimal(numeric)
    except InvalidOperation as exc:
        raise DeterministicExtractionError(f"Unsupported monetary value: {raw}") from exc
    if not decimal_value.is_finite() or decimal_value < 0:
        raise DeterministicExtractionError(f"Unsupported monetary value: {raw}")
    exponent = currency_exponent(currency)
    fractional_digits = len(numeric.partition(".")[2])
    if fractional_digits > exponent:
        raise DeterministicExtractionError(
            f"Monetary value has more than {exponent} fractional digits for {currency}: {raw}"
        )
    scaled = decimal_value.scaleb(exponent)
    if scaled != scaled.to_integral_value():
        raise DeterministicExtractionError(f"Unsupported monetary value: {raw}")
    amount_minor = int(scaled)
    if amount_minor > MAX_MINOR_UNITS:
        raise DeterministicExtractionError(
            f"Monetary value exceeds the exact supported minor-unit range: {raw}"
        )
    return {
        "type": "money",
        "currency": currency,
        "amount_minor": amount_minor,
        "cadence": cadence,
    }


def _line_ranges(text: str) -> list[tuple[int, int, str]]:
    ranges: list[tuple[int, int, str]] = []
    for match in re.finditer(r"[^\r\n]+", text):
        ranges.append((match.start(), match.end(), match.group(0)))
    return ranges


def find_prompt_injection_ranges(text: str) -> tuple[tuple[int, int], ...]:
    """Return complete line ranges containing instruction-like document text."""

    return tuple(
        (start, end) for start, end, line in _line_ranges(text) if _INJECTION_MARKERS.search(line)
    )


def _overlaps_any(start: int, end: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(start < unsafe_end and end > unsafe_start for unsafe_start, unsafe_end in ranges)


def _assertion(
    *,
    semantic_key: str,
    raw_value: str,
    normalized_value: dict[str, JsonValue],
    display_value: str,
    confidence: Decimal,
    page: ExtractedPage,
    quote: str,
    char_start: int,
    char_end: int,
) -> ExtractedAssertion:
    verified = verify_evidence_span(
        page_text=page.text,
        quote=quote,
        char_start=char_start,
        char_end=char_end,
    )
    if raw_value.casefold() not in quote.casefold():
        raise DeterministicExtractionError("An extracted raw value is not present in its evidence")
    return ExtractedAssertion(
        semantic_key=semantic_key,
        raw_value=raw_value,
        normalized_value=normalized_value,
        display_value=display_value,
        confidence=confidence,
        page_number=page.page_number,
        quote=verified.quote,
        char_start=verified.char_start,
        char_end=verified.char_end,
    )


def _label_matches(
    page: ExtractedPage,
    *,
    label: str,
    value_pattern: str,
) -> list[tuple[re.Match[str], str, int, int]]:
    pattern = re.compile(
        rf"(?im)^(?P<quote>[^\r\n]*?{label}\s*(?::|is|shall\s+be|has\s+been\s+amended\s+to)?"
        rf"\s*(?P<raw>{value_pattern})[^\r\n]*)$"
    )
    matches: list[tuple[re.Match[str], str, int, int]] = []
    for match in pattern.finditer(page.text):
        quote = match.group("quote")
        start, end = match.span("quote")
        matches.append((match, quote, start, end))
    return matches


def _extract_labelled_dates(
    page: ExtractedPage, unsafe_ranges: tuple[tuple[int, int], ...]
) -> list[ExtractedAssertion]:
    assertions: list[ExtractedAssertion] = []
    labels = (
        ("contract.effective_date", r"(?:Agreement\s+)?Effective\s+Date"),
        ("contract.initial_term_end_date", r"Initial\s+Term\s+End\s+Date"),
        ("contract.expiration_date", r"(?:Agreement\s+)?Expiration\s+Date"),
        ("renewal.notice_deadline", r"Renewal\s+Notice\s+Deadline"),
    )
    for semantic_key, label in labels:
        for match, quote, start, end in _label_matches(
            page, label=label, value_pattern=_DATE_VALUE
        ):
            if _overlaps_any(start, end, unsafe_ranges):
                continue
            raw = match.group("raw")
            normalized = _date_value(raw)
            assertions.append(
                _assertion(
                    semantic_key=semantic_key,
                    raw_value=raw,
                    normalized_value=normalized,
                    display_value=str(normalized["value"]),
                    confidence=Decimal("0.9900"),
                    page=page,
                    quote=quote,
                    char_start=start,
                    char_end=end,
                )
            )
    return assertions


def _extract_labelled_fees(
    page: ExtractedPage, unsafe_ranges: tuple[tuple[int, int], ...]
) -> tuple[list[ExtractedAssertion], list[ExtractionFinding]]:
    assertions: list[ExtractedAssertion] = []
    findings: list[ExtractionFinding] = []
    labels = (
        ("fee.subscription", r"(?P<cadence>Annual|Monthly)\s+Subscription\s+Fee", None),
        ("fee.implementation", r"Implementation\s+Fee", "one_time"),
        ("fee.termination", r"(?:Early\s+)?Termination\s+Fee", "one_time"),
    )
    for semantic_key, label, fixed_cadence in labels:
        for match, quote, start, end in _label_matches(
            page, label=label, value_pattern=_LABELLED_MONEY_CANDIDATE
        ):
            if _overlaps_any(start, end, unsafe_ranges):
                continue
            raw = match.group("raw")
            cadence_match = match.groupdict().get("cadence")
            cadence = fixed_cadence or (
                "annual" if cadence_match and cadence_match.casefold() == "annual" else "monthly"
            )
            try:
                normalized = _money_value(raw, cadence)
            except DeterministicExtractionError:
                # Never let a visibly labelled obligation disappear silently. The
                # finding carries only page/offset context, not the sensitive quote.
                findings.append(
                    ExtractionFinding(
                        code="invalid_labelled_money",
                        message=(
                            "A labelled fee has an invalid, unsupported, ambiguous, "
                            "or over-precise monetary value"
                        ),
                        severity=FindingSeverity.BLOCKING,
                        page_number=page.page_number,
                        char_start=start,
                        char_end=end,
                    )
                )
                continue
            amount_minor = normalized.get("amount_minor")
            currency = normalized.get("currency")
            if (
                not isinstance(amount_minor, int)
                or isinstance(amount_minor, bool)
                or not isinstance(currency, str)
            ):
                raise DeterministicExtractionError("Canonical monetary value is invalid")
            assertions.append(
                _assertion(
                    semantic_key=semantic_key,
                    raw_value=raw,
                    normalized_value=normalized,
                    display_value=(
                        f"{currency} "
                        f"{Decimal(amount_minor).scaleb(-currency_exponent(currency)):,.{currency_exponent(currency)}f}"
                    ),
                    confidence=Decimal("0.9900"),
                    page=page,
                    quote=quote,
                    char_start=start,
                    char_end=end,
                )
            )
    return assertions, findings


def _extract_renewal_terms(
    page: ExtractedPage, unsafe_ranges: tuple[tuple[int, int], ...]
) -> list[ExtractedAssertion]:
    assertions: list[ExtractedAssertion] = []
    for start, end, line in _line_ranges(page.text):
        if _overlaps_any(start, end, unsafe_ranges):
            continue
        auto_match = re.search(r"(?i)\bautomatically\s+renews?\b", line)
        if auto_match is not None:
            raw = auto_match.group(0)
            assertions.append(
                _assertion(
                    semantic_key="renewal.auto_renews",
                    raw_value=raw,
                    normalized_value={"type": "boolean", "value": True},
                    display_value="Yes",
                    confidence=Decimal("0.9700"),
                    page=page,
                    quote=line,
                    char_start=start,
                    char_end=end,
                )
            )
        no_auto_match = re.search(
            r"(?i)\b(?:does\s+not|shall\s+not|will\s+not)\s+automatically\s+renew\b", line
        )
        if no_auto_match is not None:
            # Replace the positive assertion created by the substring match above.
            assertions = [
                item
                for item in assertions
                if not (
                    item.page_number == page.page_number
                    and item.char_start == start
                    and item.semantic_key == "renewal.auto_renews"
                )
            ]
            raw = no_auto_match.group(0)
            assertions.append(
                _assertion(
                    semantic_key="renewal.auto_renews",
                    raw_value=raw,
                    normalized_value={"type": "boolean", "value": False},
                    display_value="No",
                    confidence=Decimal("0.9700"),
                    page=page,
                    quote=line,
                    char_start=start,
                    char_end=end,
                )
            )
        if "notice" in line.casefold():
            days_match = re.search(r"(?i)\b(?P<days>\d{1,4})\s+(?:calendar\s+)?days?\b", line)
            if days_match is not None:
                raw = days_match.group(0)
                days = int(days_match.group("days"))
                assertions.append(
                    _assertion(
                        semantic_key="renewal.notice_days",
                        raw_value=raw,
                        normalized_value={"type": "duration_days", "days": days},
                        display_value=f"{days} days",
                        confidence=Decimal("0.9700"),
                        page=page,
                        quote=line,
                        char_start=start,
                        char_end=end,
                    )
                )
    return assertions


def _page_findings(
    page: ExtractedPage, unsafe_ranges: tuple[tuple[int, int], ...]
) -> list[ExtractionFinding]:
    findings: list[ExtractionFinding] = []
    if page.status is PageExtractionStatus.NEEDS_OCR:
        findings.append(
            ExtractionFinding(
                code="page_needs_ocr",
                message="The page does not contain enough reliable embedded text for extraction",
                severity=FindingSeverity.BLOCKING,
                page_number=page.page_number,
            )
        )
    for start, end in unsafe_ranges:
        findings.append(
            ExtractionFinding(
                code="prompt_injection_text",
                message=(
                    "Instruction-like text was treated as untrusted contract content and excluded"
                ),
                severity=FindingSeverity.WARNING,
                page_number=page.page_number,
                char_start=start,
                char_end=end,
            )
        )
    for start, end, line in _line_ranges(page.text):
        if _REDACTION_MARKERS.search(line):
            findings.append(
                ExtractionFinding(
                    code="redacted_value",
                    message="A redacted contract value requires human-supplied evidence",
                    severity=FindingSeverity.BLOCKING,
                    page_number=page.page_number,
                    char_start=start,
                    char_end=end,
                )
            )
        if re.search(r"(?i)\bamendment\s+(?:no\.?\s*)?\d+\b", line):
            findings.append(
                ExtractionFinding(
                    code="amendment_detected",
                    message="An amendment was detected; conflicting terms require human review",
                    severity=FindingSeverity.INFO,
                    page_number=page.page_number,
                    char_start=start,
                    char_end=end,
                )
            )
    return findings


def _mark_conflicts(
    assertions: list[ExtractedAssertion], findings: list[ExtractionFinding]
) -> tuple[list[ExtractedAssertion], tuple[str, ...]]:
    values_by_key: dict[str, set[str]] = {}
    for assertion in assertions:
        canonical = json.dumps(assertion.normalized_value, sort_keys=True, separators=(",", ":"))
        values_by_key.setdefault(assertion.semantic_key, set()).add(canonical)
    conflict_keys = tuple(sorted(key for key, values in values_by_key.items() if len(values) > 1))
    if conflict_keys:
        conflict_set = set(conflict_keys)
        assertions = [
            replace(assertion, conflicting=True)
            if assertion.semantic_key in conflict_set
            else assertion
            for assertion in assertions
        ]
        findings.extend(
            ExtractionFinding(
                code="conflicting_assertions",
                message=f"Multiple different values were extracted for {semantic_key}",
                severity=FindingSeverity.BLOCKING,
            )
            for semantic_key in conflict_keys
        )
    return assertions, conflict_keys


def deterministic_extract(pages: tuple[ExtractedPage, ...]) -> ClauseExtractionResult:
    """Extract supported explicit terms without network or model access."""

    assertions: list[ExtractedAssertion] = []
    findings: list[ExtractionFinding] = []
    for page in pages:
        unsafe_ranges = find_prompt_injection_ranges(page.text)
        findings.extend(_page_findings(page, unsafe_ranges))
        if page.status is PageExtractionStatus.NEEDS_OCR:
            continue
        assertions.extend(_extract_labelled_dates(page, unsafe_ranges))
        fee_assertions, fee_findings = _extract_labelled_fees(page, unsafe_ranges)
        assertions.extend(fee_assertions)
        findings.extend(fee_findings)
        assertions.extend(_extract_renewal_terms(page, unsafe_ranges))

    unique: dict[tuple[str, int, int, str], ExtractedAssertion] = {}
    for assertion in assertions:
        key = (
            assertion.semantic_key,
            assertion.page_number,
            assertion.char_start,
            json.dumps(assertion.normalized_value, sort_keys=True),
        )
        unique[key] = assertion
    ordered = sorted(
        unique.values(), key=lambda item: (item.page_number, item.char_start, item.semantic_key)
    )
    ordered, conflict_keys = _mark_conflicts(ordered, findings)
    return ClauseExtractionResult(
        extractor=EXTRACTOR_IDENTIFIER,
        assertions=tuple(ordered),
        findings=tuple(findings),
        conflicting_semantic_keys=conflict_keys,
    )


class DeterministicClauseExtractor:
    """Object adapter for services that inject extractor implementations."""

    identifier = EXTRACTOR_IDENTIFIER

    def extract(self, pages: tuple[ExtractedPage, ...]) -> ClauseExtractionResult:
        return deterministic_extract(pages)
