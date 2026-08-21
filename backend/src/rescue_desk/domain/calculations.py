import calendar
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from rescue_desk.domain.hashing import hash_payload
from rescue_desk.domain.money import MAX_MINOR_UNITS, SUPPORTED_CURRENCIES
from rescue_desk.models import FeeCategory

ENGINE_VERSION = "remaining-subscription-v2"


class CalculationInputError(ValueError):
    pass


@dataclass(frozen=True)
class FeeInput:
    identifier: str
    category: FeeCategory
    amount_minor: int
    currency: str
    service_start: date | None = None
    service_end: date | None = None
    payment_status: str = "unknown"
    proration_rule: str | None = None
    reviewed: bool = True


@dataclass(frozen=True)
class LineResult:
    identifier: str
    treatment: str
    original_amount_minor: int
    remaining_amount_minor: int | None
    currency: str
    formula_identifier: str
    explanation: str


@dataclass(frozen=True)
class CurrencyResult:
    currency: str
    documented_remaining_subscription_minor: int
    excluded_non_subscription_minor: int
    unclassified_minor: int
    potential_coverage_min_minor: int
    potential_coverage_max_minor: int


@dataclass(frozen=True)
class AnalysisResult:
    as_of_date: date
    engine_version: str
    currencies: tuple[CurrencyResult, ...]
    line_items: tuple[LineResult, ...]
    blocking_findings: tuple[str, ...]
    assumptions: tuple[str, ...]
    input_hash: str
    result_hash: str


def round_minor(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def subtract_calendar_months_clamped(value: date, months: int) -> date:
    if months < 0:
        raise CalculationInputError("Notice months cannot be negative")
    month_index = value.year * 12 + value.month - 1 - months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def calculate_notice_deadline(
    *, contract_end: date, notice_days: int | None = None, notice_months: int | None = None
) -> date:
    if (notice_days is None) == (notice_months is None):
        raise CalculationInputError("Provide exactly one notice-period unit")
    if notice_days is not None:
        if notice_days < 0:
            raise CalculationInputError("Notice days cannot be negative")
        return contract_end - timedelta(days=notice_days)
    assert notice_months is not None
    return subtract_calendar_months_clamped(contract_end, notice_months)


def _remaining_subscription_amount(fee: FeeInput, as_of: date) -> tuple[int | None, str]:
    if fee.payment_status == "paid":
        return 0, "paid-no-remaining-obligation-v1"
    if fee.payment_status == "unknown":
        return None, "payment-status-unknown"
    if fee.payment_status != "unpaid":
        raise CalculationInputError(f"Invalid payment status for {fee.identifier}")
    if fee.service_start is None or fee.service_end is None:
        return None, "missing-service-period"
    if fee.service_end <= fee.service_start:
        raise CalculationInputError(f"Invalid service period for {fee.identifier}")
    if as_of >= fee.service_end:
        return 0, "expired-service-period-v1"
    if as_of <= fee.service_start:
        return fee.amount_minor, "future-full-obligation-v1"
    if fee.proration_rule != "contract_daily":
        return None, "proration-unsupported"
    total_days = (fee.service_end - fee.service_start).days
    remaining_days = (fee.service_end - as_of).days
    remaining = round_minor(
        Decimal(fee.amount_minor) * Decimal(remaining_days) / Decimal(total_days)
    )
    return remaining, "contract-daily-half-open-v1"


def analyze_fees(*, fees: list[FeeInput], as_of: date) -> AnalysisResult:
    if not fees:
        raise CalculationInputError("At least one fee obligation is required")
    input_payload = {
        "as_of_date": as_of.isoformat(),
        "engine_version": ENGINE_VERSION,
        "fees": [
            {
                **asdict(fee),
                "category": fee.category.value,
                "service_start": fee.service_start.isoformat() if fee.service_start else None,
                "service_end": fee.service_end.isoformat() if fee.service_end else None,
            }
            for fee in fees
        ],
    }
    input_hash = hash_payload(input_payload)
    currency_totals: dict[str, dict[str, int]] = {}
    aggregate_inputs: dict[str, int] = {}
    line_items: list[LineResult] = []
    blockers: list[str] = []
    assumptions: set[str] = set()

    for fee in fees:
        if fee.amount_minor < 0:
            raise CalculationInputError(f"Negative fee amount for {fee.identifier}")
        currency = fee.currency.upper()
        if currency not in SUPPORTED_CURRENCIES:
            raise CalculationInputError(f"Unsupported currency for {fee.identifier}: {currency}")
        current_aggregate = aggregate_inputs.get(currency, 0)
        if fee.amount_minor > MAX_MINOR_UNITS - current_aggregate:
            raise CalculationInputError(
                f"Aggregate {currency} obligations exceed the exact supported minor-unit range"
            )
        aggregate_inputs[currency] = current_aggregate + fee.amount_minor
        totals = currency_totals.setdefault(currency, {"remaining": 0, "excluded": 0, "unknown": 0})
        if not fee.reviewed:
            totals["unknown"] += fee.amount_minor
            blockers.append(f"{fee.identifier}: fee has not been reviewed")
            line_items.append(
                LineResult(
                    identifier=fee.identifier,
                    treatment="unclassified",
                    original_amount_minor=fee.amount_minor,
                    remaining_amount_minor=None,
                    currency=currency,
                    formula_identifier="unreviewed-v1",
                    explanation="Unreviewed fee is kept out of the subscription baseline.",
                )
            )
            continue
        if fee.category == FeeCategory.SUBSCRIPTION:
            if fee.payment_status in {"paid", "unpaid"}:
                assumptions.add(
                    f"Payment status '{fee.payment_status}' is a human-supplied scenario input; "
                    "the fee quote proves amount, currency, category, and cadence only"
                )
            if fee.service_start is not None or fee.service_end is not None:
                assumptions.add(
                    "Fee evidence proves amount, currency, category, and cadence; service-period "
                    "dates are explicit scenario inputs hashed with this calculation"
                )
            remaining, formula = _remaining_subscription_amount(fee, as_of)
            if remaining is None:
                totals["unknown"] += fee.amount_minor
                if formula == "payment-status-unknown":
                    blockers.append(
                        f"{fee.identifier}: payment status is unknown and cannot be treated "
                        "as unpaid"
                    )
                else:
                    blockers.append(
                        f"{fee.identifier}: subscription proration is unsupported or unclear"
                    )
                treatment = "unclassified"
                explanation = (
                    "Subscription amount cannot be treated as remaining until payment status "
                    "and any required proration inputs are explicit."
                )
            else:
                totals["remaining"] += remaining
                treatment = "included"
                explanation = "Reviewed subscription obligation included in documented remainder."
                if formula == "contract-daily-half-open-v1":
                    assumptions.add(
                        "Reviewed contract-daily proration using a half-open service interval"
                    )
            line_items.append(
                LineResult(
                    identifier=fee.identifier,
                    treatment=treatment,
                    original_amount_minor=fee.amount_minor,
                    remaining_amount_minor=remaining,
                    currency=currency,
                    formula_identifier=formula,
                    explanation=explanation,
                )
            )
        elif fee.category == FeeCategory.UNCLASSIFIED:
            totals["unknown"] += fee.amount_minor
            blockers.append(f"{fee.identifier}: fee category is unclassified")
            line_items.append(
                LineResult(
                    identifier=fee.identifier,
                    treatment="unclassified",
                    original_amount_minor=fee.amount_minor,
                    remaining_amount_minor=None,
                    currency=currency,
                    formula_identifier="unclassified-v1",
                    explanation="Unclassified charge remains separate and blocks readiness.",
                )
            )
        else:
            totals["excluded"] += fee.amount_minor
            line_items.append(
                LineResult(
                    identifier=fee.identifier,
                    treatment="excluded",
                    original_amount_minor=fee.amount_minor,
                    remaining_amount_minor=0,
                    currency=currency,
                    formula_identifier="non-subscription-exclusion-v1",
                    explanation=(
                        f"{fee.category.value} is not included in the subscription baseline."
                    ),
                )
            )

    if len(currency_totals) > 1:
        blockers.append("Multiple currencies are reported separately and cannot be aggregated")

    currencies = tuple(
        CurrencyResult(
            currency=currency,
            documented_remaining_subscription_minor=totals["remaining"],
            excluded_non_subscription_minor=totals["excluded"],
            unclassified_minor=totals["unknown"],
            potential_coverage_min_minor=0,
            potential_coverage_max_minor=totals["remaining"],
        )
        for currency, totals in sorted(currency_totals.items())
    )
    result_payload = {
        "as_of_date": as_of.isoformat(),
        "engine_version": ENGINE_VERSION,
        "currencies": [asdict(item) for item in currencies],
        "line_items": [asdict(item) for item in line_items],
        "blocking_findings": sorted(set(blockers)),
        "assumptions": sorted(assumptions),
        "input_hash": input_hash,
    }
    result_hash = hash_payload(result_payload)
    return AnalysisResult(
        as_of_date=as_of,
        engine_version=ENGINE_VERSION,
        currencies=currencies,
        line_items=tuple(line_items),
        blocking_findings=tuple(sorted(set(blockers))),
        assumptions=tuple(sorted(assumptions)),
        input_hash=input_hash,
        result_hash=result_hash,
    )
