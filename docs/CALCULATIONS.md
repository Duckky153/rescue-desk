# Deterministic calculations

## Governing rule

AI does not calculate. It may propose candidate fields, but the calculation engine consumes only
typed, reviewed inputs and produces a versioned result with input and result hashes.

All money uses integer minor units. USD 1,200.34 is stored as `120034`; floating point is forbidden.
The export contract also carries the currency exponent, so zero-decimal and three-decimal currencies
are formatted correctly rather than being assumed to have two decimal places.
Intermediate division uses `Decimal`, and conversion to minor units uses round-half-up.

## Current engine

Engine identifier: `remaining-subscription-v2`.

For a reviewed subscription with a supported daily-proration clause:

```text
total_days     = service_end - service_start
remaining_days = service_end - as_of_date
remaining_minor = round_half_up(
    original_amount_minor * remaining_days / total_days
)
```

The service interval is half-open: `[service_start, service_end)`.

- If `as_of_date <= service_start`, the full reviewed amount remains.
- If `as_of_date >= service_end`, zero remains.
- If the fee is explicitly marked paid, zero remains.
- If the fee is explicitly marked unpaid, the selected category/date/proration rules apply.
- If payment status is unknown, no payable remainder is inferred; the line is unclassified and
  blocking.
- If either service date is missing, the amount remains unclassified.
- If the dates are invalid or the amount is negative, the input is rejected.
- If the proration rule is not explicitly reviewed as `contract_daily`, no amount is inferred.

## Fee treatment

A fee marked reviewed must cite a current accepted or corrected fee assertion for the same
category. Its typed amount, currency, and supplied cadence must match exactly; a date or unrelated
sentence cannot stand in for money evidence. Any linked date assertion must equal one of the
supplied fee dates. Service-period dates remain explicit hashed scenario inputs/assumptions unless
the matching date citations are also linked.

Each current money assertion can be the primary evidence for at most one active fee in a revision.
An entry mistake is recovered by superseding the fee with a reason; the historical row remains
auditable but is excluded from current calculations and releases its assertion for corrected use.

| Category | Treatment |
|---|---|
| Reviewed subscription | Included if the remaining amount is determinable |
| Unreviewed subscription | Unclassified and blocking |
| Tax, penalty, implementation, termination, or professional services | Reported separately as excluded from the subscription baseline |
| Unclassified | Reported separately and blocking |

“Excluded” means excluded from this demonstration's subscription baseline. It does not mean a fee
is invalid, avoidable, or legally unenforceable.

## Coverage scenario

For each currency:

```text
potential_coverage_min_minor = 0
potential_coverage_max_minor = documented_remaining_subscription_minor
```

This is deliberately a range, not an eligibility decision or promise. No private program rule is
encoded. Unclassified and non-subscription amounts are shown separately.

## Multiple currencies

Currencies are never summed or converted. Each currency receives an independent scenario, and the
packet carries a blocker explaining that no aggregate is available. This avoids silently choosing
an exchange-rate source, timestamp, or accounting treatment.

The extraction boundary recognizes the product's explicit supported ISO codes and only
unambiguous symbols. It converts quoted decimal values with the registered currency exponent—for
example JPY uses zero decimal places and KWD uses three—and rejects a quote with more fractional
digits than that currency permits.

## Notice dates

Day-based notice subtracts an exact number of calendar days from the contract end. Month-based
notice subtracts calendar months and clamps the day to the last valid day of the target month.
Exactly one unit must be supplied, and negative notice periods are rejected.

## Reproducibility

The engine records:

- engine version;
- normalized typed inputs;
- as-of date;
- per-line formula identifier and explanation;
- assumptions and blockers;
- SHA-256 of canonical inputs; and
- SHA-256 of the canonical result.

Every export includes both calculation hashes and the enclosing export snapshot hash. A changed
input, assumption, evidence reference, status, or formula creates a different snapshot.
A previously stored calculation whose input hash no longer matches the current reviewed fee ledger
is stale: readiness reports it as non-reproducible and requires a new run before approval.

## Worked synthetic example

```text
amount:         USD 120,000.00 = 12,000,000 minor units
service start:  2026-01-01
service end:    2027-01-01
as of:          2026-09-01
total days:     365
remaining days: 122

round_half_up(12,000,000 * 122 / 365) = 4,010,959 minor units
result: USD 40,109.59
```

This example illustrates software behavior only. It is not an interpretation of an actual
contract or a prediction of any credit.
