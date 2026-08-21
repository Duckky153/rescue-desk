from datetime import date

import pytest

from rescue_desk.domain.calculations import (
    CalculationInputError,
    FeeInput,
    analyze_fees,
    calculate_notice_deadline,
    round_minor,
)
from rescue_desk.models import FeeCategory


def test_calculates_half_open_daily_subscription_and_excludes_other_fees() -> None:
    result = analyze_fees(
        as_of=date(2026, 7, 2),
        fees=[
            FeeInput(
                identifier="subscription",
                category=FeeCategory.SUBSCRIPTION,
                amount_minor=36_500_00,
                currency="USD",
                service_start=date(2026, 1, 1),
                service_end=date(2027, 1, 1),
                payment_status="unpaid",
                proration_rule="contract_daily",
            ),
            FeeInput(
                identifier="implementation",
                category=FeeCategory.IMPLEMENTATION,
                amount_minor=5_000_00,
                currency="USD",
            ),
        ],
    )
    usd = result.currencies[0]
    assert usd.documented_remaining_subscription_minor == 18_300_00
    assert usd.excluded_non_subscription_minor == 5_000_00
    assert usd.potential_coverage_min_minor == 0
    assert usd.potential_coverage_max_minor == 18_300_00
    assert not result.blocking_findings


def test_unknown_proration_remains_unclassified_and_blocks_readiness() -> None:
    result = analyze_fees(
        as_of=date(2026, 6, 1),
        fees=[
            FeeInput(
                identifier="unclear",
                category=FeeCategory.SUBSCRIPTION,
                amount_minor=12_000_00,
                currency="USD",
                service_start=date(2026, 1, 1),
                service_end=date(2027, 1, 1),
                payment_status="unpaid",
            )
        ],
    )
    assert result.currencies[0].documented_remaining_subscription_minor == 0
    assert result.currencies[0].unclassified_minor == 12_000_00
    assert "proration is unsupported or unclear" in result.blocking_findings[0]


def test_different_currencies_are_never_aggregated() -> None:
    result = analyze_fees(
        as_of=date(2026, 1, 1),
        fees=[
            FeeInput(
                "usd",
                FeeCategory.SUBSCRIPTION,
                100,
                "USD",
                date(2026, 2, 1),
                date(2026, 3, 1),
                "unpaid",
            ),
            FeeInput(
                "eur",
                FeeCategory.SUBSCRIPTION,
                200,
                "EUR",
                date(2026, 2, 1),
                date(2026, 3, 1),
                "unpaid",
            ),
        ],
    )
    assert [item.currency for item in result.currencies] == ["EUR", "USD"]
    assert "cannot be aggregated" in result.blocking_findings[-1]


def test_unknown_payment_status_cannot_be_treated_as_unpaid() -> None:
    result = analyze_fees(
        as_of=date(2026, 1, 1),
        fees=[
            FeeInput(
                identifier="unknown-payment",
                category=FeeCategory.SUBSCRIPTION,
                amount_minor=12_000_00,
                currency="USD",
                service_start=date(2026, 1, 1),
                service_end=date(2027, 1, 1),
                payment_status="unknown",
                proration_rule="contract_daily",
            )
        ],
    )

    line = result.line_items[0]
    assert line.treatment == "unclassified"
    assert line.remaining_amount_minor is None
    assert line.formula_identifier == "payment-status-unknown"
    assert result.currencies[0].documented_remaining_subscription_minor == 0
    assert result.currencies[0].unclassified_minor == 12_000_00
    assert result.blocking_findings == (
        "unknown-payment: payment status is unknown and cannot be treated as unpaid",
    )


def test_domain_engine_rejects_unregistered_three_letter_currency() -> None:
    with pytest.raises(CalculationInputError, match=r"Unsupported currency.*ZZZ"):
        analyze_fees(
            as_of=date(2026, 1, 1),
            fees=[FeeInput("unsupported", FeeCategory.OTHER, 100, "ZZZ")],
        )


@pytest.mark.parametrize(
    ("contract_end", "months", "expected"),
    [
        (date(2027, 5, 31), 3, date(2027, 2, 28)),
        (date(2028, 5, 31), 3, date(2028, 2, 29)),
        (date(2027, 1, 31), 1, date(2026, 12, 31)),
    ],
)
def test_month_notice_deadline_clamps_calendar_day(
    contract_end: date, months: int, expected: date
) -> None:
    assert calculate_notice_deadline(contract_end=contract_end, notice_months=months) == expected


def test_notice_requires_exactly_one_unit() -> None:
    with pytest.raises(CalculationInputError):
        calculate_notice_deadline(contract_end=date(2027, 1, 1))


def test_rounds_minor_units_half_up() -> None:
    from decimal import Decimal

    assert round_minor(Decimal("10.5")) == 11
    assert round_minor(Decimal("10.49")) == 10
