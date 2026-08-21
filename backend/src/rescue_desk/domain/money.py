"""Shared money-domain limits for exact minor-unit storage."""

from types import MappingProxyType
from typing import Final

# Keep JSON transport exact in JavaScript while leaving ample enterprise-scale headroom.
MAX_MINOR_UNITS: Final = 9_000_000_000_000_000

# RescueDesk deliberately supports this explicit, versioned subset of ISO 4217 currencies.
# Expanding the set requires adding the exponent here and in the TypeScript registry.
_CURRENCY_EXPONENTS = {
    "AUD": 2,
    "BHD": 3,
    "CAD": 2,
    "CHF": 2,
    "CLP": 0,
    "EUR": 2,
    "GBP": 2,
    "INR": 2,
    "JOD": 3,
    "JPY": 0,
    "KRW": 0,
    "KWD": 3,
    "OMR": 3,
    "TND": 3,
    "USD": 2,
    "VND": 0,
}
CURRENCY_EXPONENTS: Final = MappingProxyType(_CURRENCY_EXPONENTS)
SUPPORTED_CURRENCIES: Final = frozenset(CURRENCY_EXPONENTS)


def currency_exponent(currency: str) -> int:
    """Return the locked minor-unit exponent or reject unsupported input."""

    normalized = currency.upper()
    try:
        return CURRENCY_EXPONENTS[normalized]
    except KeyError as exc:
        supported = ", ".join(sorted(SUPPORTED_CURRENCIES))
        raise ValueError(
            f"Unsupported currency {normalized}; supported currencies: {supported}"
        ) from exc
