"""Canonical contracts for assertion values and fee evidence relevance.

The extractor, human-review flow, readiness evaluator, and fee ledger all use
the same semantic value shapes.  Keeping this contract in one module prevents
a human correction from turning arbitrary JSON into an approved contract fact.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Final

from rescue_desk.domain.money import MAX_MINOR_UNITS, SUPPORTED_CURRENCIES, currency_exponent
from rescue_desk.models import FeeCategory


class SemanticValueError(ValueError):
    """Raised when a normalized assertion does not match its semantic key."""


DATE_SEMANTIC_KEYS: Final = frozenset(
    {
        "contract.effective_date",
        "contract.initial_term_end_date",
        "contract.expiration_date",
        "renewal.notice_deadline",
        # Canonical persistence keys produced by services.documents.
        "contract_start_date",
        "contract_end_date",
        "notice_deadline",
    }
)
MONEY_SEMANTIC_KEYS: Final = frozenset(
    {"fee.subscription", "fee.implementation", "fee.termination"}
)
BOOLEAN_SEMANTIC_KEYS: Final = frozenset({"renewal.auto_renews", "auto_renewal"})
DURATION_SEMANTIC_KEYS: Final = frozenset({"renewal.notice_days"})

FEE_ASSERTION_KEYS: Final = {
    FeeCategory.SUBSCRIPTION: "fee.subscription",
    FeeCategory.IMPLEMENTATION: "fee.implementation",
    FeeCategory.TERMINATION: "fee.termination",
}


def _require_exact_keys(value: dict[str, Any], expected: set[str]) -> None:
    actual = set(value)
    if actual != expected:
        raise SemanticValueError(
            "Normalized value fields do not match the semantic contract: "
            f"expected {sorted(expected)}, received {sorted(actual)}"
        )


def _canonical_date(value: dict[str, Any]) -> dict[str, Any]:
    _require_exact_keys(value, {"type", "value"})
    if value.get("type") != "date" or not isinstance(value.get("value"), str):
        raise SemanticValueError("Date assertions require type=date and an ISO date value")
    raw = value["value"]
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise SemanticValueError("Date assertion value must be a real ISO calendar date") from exc
    if raw != parsed.isoformat():
        raise SemanticValueError("Date assertion value must use canonical YYYY-MM-DD form")
    return {"type": "date", "value": parsed.isoformat()}


def _canonical_money(semantic_key: str, value: dict[str, Any]) -> dict[str, Any]:
    _require_exact_keys(value, {"type", "amount_minor", "currency", "cadence"})
    amount = value.get("amount_minor")
    currency = value.get("currency")
    cadence = value.get("cadence")
    if value.get("type") != "money":
        raise SemanticValueError("Fee assertions require type=money")
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise SemanticValueError("Money amount_minor must be an exact integer")
    if not 0 <= amount <= MAX_MINOR_UNITS:
        raise SemanticValueError("Money amount_minor is outside the supported exact range")
    if not isinstance(currency, str):
        raise SemanticValueError("Money currency must be a supported three-letter code")
    normalized_currency = currency.upper()
    if currency != normalized_currency or normalized_currency not in SUPPORTED_CURRENCIES:
        raise SemanticValueError("Money currency must be an uppercase supported code")
    allowed_cadences = {"annual", "monthly"} if semantic_key == "fee.subscription" else {"one_time"}
    if cadence not in allowed_cadences:
        expected = "annual or monthly" if semantic_key == "fee.subscription" else "one_time"
        raise SemanticValueError(f"{semantic_key} cadence must be {expected}")
    return {
        "type": "money",
        "amount_minor": amount,
        "currency": normalized_currency,
        "cadence": cadence,
    }


def _canonical_boolean(value: dict[str, Any]) -> dict[str, Any]:
    _require_exact_keys(value, {"type", "value"})
    boolean = value.get("value")
    if value.get("type") != "boolean" or not isinstance(boolean, bool):
        raise SemanticValueError("Auto-renewal assertions require type=boolean and a boolean value")
    return {"type": "boolean", "value": boolean}


def _canonical_duration(value: dict[str, Any]) -> dict[str, Any]:
    _require_exact_keys(value, {"type", "days"})
    days = value.get("days")
    if value.get("type") != "duration_days":
        raise SemanticValueError("Notice duration assertions require type=duration_days")
    if not isinstance(days, int) or isinstance(days, bool) or not 0 <= days <= 9_999:
        raise SemanticValueError("Notice duration days must be an integer from 0 through 9999")
    return {"type": "duration_days", "days": days}


def canonical_assertion_value(semantic_key: str, value: dict[str, Any]) -> dict[str, Any]:
    """Validate and return the one deterministic shape allowed for a semantic key."""

    if semantic_key in DATE_SEMANTIC_KEYS:
        return _canonical_date(value)
    if semantic_key in MONEY_SEMANTIC_KEYS:
        return _canonical_money(semantic_key, value)
    if semantic_key in BOOLEAN_SEMANTIC_KEYS:
        return _canonical_boolean(value)
    if semantic_key in DURATION_SEMANTIC_KEYS:
        return _canonical_duration(value)
    raise SemanticValueError(f"Unsupported assertion semantic key: {semantic_key}")


def canonical_assertion_display(semantic_key: str, value: dict[str, Any]) -> str:
    """Produce display text from a validated normalized value, never from free text."""

    canonical = canonical_assertion_value(semantic_key, value)
    value_type = canonical["type"]
    if value_type == "date":
        return str(canonical["value"])
    if value_type == "boolean":
        return "Yes" if canonical["value"] is True else "No"
    if value_type == "duration_days":
        return f"{canonical['days']} days"
    amount = int(canonical["amount_minor"])
    currency = str(canonical["currency"])
    exponent = currency_exponent(currency)
    major = Decimal(amount).scaleb(-exponent)
    return f"{currency} {major:,.{exponent}f}"


def semantic_value_is_valid(semantic_key: str, value: dict[str, Any]) -> bool:
    try:
        canonical_assertion_value(semantic_key, value)
    except SemanticValueError:
        return False
    return True


def correction_requires_assumption(
    semantic_key: str,
    proposed_value: dict[str, Any],
    corrected_value: dict[str, Any],
) -> bool:
    """Return whether a correction changes the source-backed canonical fact.

    A malformed/conflicting proposal has no single canonical value, so replacing
    it is necessarily an explicit human assumption.  A same-value correction is
    allowed without that flag because it only canonicalizes representation.
    """

    corrected = canonical_assertion_value(semantic_key, corrected_value)
    try:
        proposed = canonical_assertion_value(semantic_key, proposed_value)
    except SemanticValueError:
        return True
    return proposed != corrected


def fee_money_evidence_matches(
    *,
    semantic_key: str,
    normalized_value: dict[str, Any],
    category: FeeCategory,
    amount_minor: int,
    currency: str,
    billing_cadence: str | None,
) -> bool:
    """Return whether one assertion proves the fee's typed monetary fields."""

    if FEE_ASSERTION_KEYS.get(category) != semantic_key:
        return False
    try:
        canonical = canonical_assertion_value(semantic_key, normalized_value)
    except SemanticValueError:
        return False
    return bool(
        canonical["amount_minor"] == amount_minor
        and canonical["currency"] == currency
        and canonical["cadence"] == billing_cadence
    )


def assertion_date_value(semantic_key: str, normalized_value: dict[str, Any]) -> date | None:
    """Return a linked date assertion's value, or None for a non-date assertion."""

    if semantic_key not in DATE_SEMANTIC_KEYS:
        return None
    try:
        canonical = canonical_assertion_value(semantic_key, normalized_value)
    except SemanticValueError:
        return None
    return date.fromisoformat(str(canonical["value"]))
