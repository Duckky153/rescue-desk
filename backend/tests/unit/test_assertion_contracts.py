from __future__ import annotations

import pytest

from rescue_desk.domain.assertions import (
    SemanticValueError,
    canonical_assertion_display,
    canonical_assertion_value,
    fee_money_evidence_matches,
)
from rescue_desk.models import FeeCategory


@pytest.mark.parametrize(
    ("semantic_key", "value", "display"),
    [
        ("contract_end_date", {"type": "date", "value": "2029-01-14"}, "2029-01-14"),
        (
            "fee.subscription",
            {
                "type": "money",
                "amount_minor": 12_000_000,
                "currency": "USD",
                "cadence": "annual",
            },
            "USD 120,000.00",
        ),
        (
            "fee.implementation",
            {
                "type": "money",
                "amount_minor": 25_000,
                "currency": "JPY",
                "cadence": "one_time",
            },
            "JPY 25,000",
        ),
        ("auto_renewal", {"type": "boolean", "value": False}, "No"),
        ("renewal.notice_days", {"type": "duration_days", "days": 90}, "90 days"),
    ],
)
def test_known_assertion_semantics_have_one_canonical_shape(
    semantic_key: str, value: dict[str, object], display: str
) -> None:
    assert canonical_assertion_value(semantic_key, value) == value
    assert canonical_assertion_display(semantic_key, value) == display


@pytest.mark.parametrize(
    ("semantic_key", "value"),
    [
        ("contract_end_date", {"date": "2029-01-14"}),
        ("contract_end_date", {"type": "date", "value": "2029-02-29"}),
        ("contract_end_date", {"type": "text", "value": "eventually"}),
        (
            "fee.subscription",
            {"type": "money", "amount_minor": True, "currency": "USD", "cadence": "annual"},
        ),
        (
            "fee.subscription",
            {"type": "money", "amount_minor": 100, "currency": "usd", "cadence": "annual"},
        ),
        (
            "fee.subscription",
            {
                "type": "money",
                "amount_minor": 100,
                "currency": "USD",
                "cadence": "weekly",
            },
        ),
        ("auto_renewal", {"type": "boolean", "value": "yes"}),
        ("renewal.notice_days", {"type": "duration_days", "days": True}),
        ("contract.vendor_name", {"type": "text", "value": "Anything"}),
    ],
)
def test_malformed_or_unsupported_semantic_objects_are_rejected(
    semantic_key: str, value: dict[str, object]
) -> None:
    with pytest.raises(SemanticValueError):
        canonical_assertion_value(semantic_key, value)


def test_fee_evidence_requires_matching_key_amount_currency_and_cadence() -> None:
    evidence = {
        "type": "money",
        "amount_minor": 12_000_000,
        "currency": "USD",
        "cadence": "annual",
    }
    assert fee_money_evidence_matches(
        semantic_key="fee.subscription",
        normalized_value=evidence,
        category=FeeCategory.SUBSCRIPTION,
        amount_minor=12_000_000,
        currency="USD",
        billing_cadence="annual",
    )
    assert not fee_money_evidence_matches(
        semantic_key="fee.subscription",
        normalized_value=evidence,
        category=FeeCategory.SUBSCRIPTION,
        amount_minor=12_000_001,
        currency="USD",
        billing_cadence="annual",
    )
    assert not fee_money_evidence_matches(
        semantic_key="fee.subscription",
        normalized_value=evidence,
        category=FeeCategory.TERMINATION,
        amount_minor=12_000_000,
        currency="USD",
        billing_cadence="annual",
    )
