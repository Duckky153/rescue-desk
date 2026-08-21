# Data dictionary

## Case and revision

| Field | Type | Meaning |
|---|---|---|
| `case_id` | stable string ID | Organization-scoped review case |
| `case_status` | enum | Workflow state, never inferred from UI text |
| `revision_id` | stable string ID | Immutable evidence/calculation revision |
| `revision_number` | positive integer | Human-readable sequence within a case |
| `packet_superseded` | boolean | A newer packet exists; this packet must not be relied upon |
| `as_of_date` | ISO date | Date at which remaining obligations are calculated |
| `prepared_at` | timezone-aware timestamp | Snapshot preparation time, canonicalized to UTC for hashing |
| `approval_status` | enum | `not_approved` or `approved`; not derived from artifact existence |
| `approved_by` | optional actor label | Human approver represented in the snapshot |

## Source citation

| Field | Type | Meaning |
|---|---|---|
| `citation_id` | stable string ID | Reference used by facts, lines, and blockers |
| `document_id` | stable string ID | Immutable uploaded document record |
| `document_name` | string | Display name; treated as untrusted input |
| `document_sha256` | 64 lowercase hex characters | Hash of original document bytes |
| `page_number` | positive integer | One-based PDF page |
| `quote` | nonblank string | Exact page-text slice |
| `quote_sha256` | SHA-256 | Hash of UTF-8 quote text |
| `char_start`, `char_end` | integers | Half-open offsets into stored page text |
| `superseded` | boolean | Source replaced by a later reviewed document |

The contract verifies that `char_end - char_start == len(quote)` and that the quote hash matches.
The evidence domain also verifies the quote against the stored page text.

## Reviewed fact

| Field | Meaning |
|---|---|
| `fact_id` | Stable assertion ID |
| `label`, `value` | Human-readable claim |
| `normalized_value` | Semantic-key-specific typed object: canonical date, money, boolean, or day duration; unknown or extra fields fail closed |
| `review_state` | `proposed`, `accepted`, `corrected`, `rejected`, or `conflicting` |
| `citation_ids` | One or more source citations |
| `source` | Machine or human origin of this assertion row |
| `reviewed_by`, `review_reason` | Human actor display name and recorded decision rationale |
| `assumption` | Whether the current canonical value is human-supplied rather than source-confirmed |
| `evidence_basis` | `pending_review`, `source_evidence`, `explicit_assumption`, `rejected_source`, or `invalid_review` |
| `extractor`, `model_identifier`, `prompt_hash`, `code_version`, `structured_output_hash` | Frozen proposal provenance when extraction supplied the source row |
| `customer_visible` | Whether it may appear in the customer brief |
| `superseded` | Retained history that must not be relied upon |

Human corrections are validated against the fact's semantic key and their display text is derived
from the canonical typed value. A reviewed fee must include a compatible fee citation whose typed
category, amount, currency, and supplied cadence match the ledger input exactly.

## Fee obligation

| Field | Meaning |
|---|---|
| `amount_minor`, `currency` | Exact active scenario input and supported ISO currency; `reviewed` and evidence fields state whether the contract supports it |
| `category`, `billing_cadence` | Governed treatment and, when supplied, evidence-matched cadence |
| `payment_status` | Explicit `paid`, `unpaid`, or `unknown`; unknown never means unpaid |
| `assertion_ids` | All reviewed source facts linked to the input |
| `primary_money_assertion_id` | The single category-compatible money assertion consumed by this active fee |
| `reviewed` | Whether the row passed current evidence-governance checks |
| `superseded`, `superseded_at`, `superseded_by`, `supersede_reason` | Soft-retirement history; superseded rows are excluded from current analysis |

## Calculation line

| Field | Meaning |
|---|---|
| `original_amount_minor` | Documented input in integer minor units |
| `result_amount_minor` | Deterministic result, or `null` when unsupported |
| `currency` | Uppercase three-letter ISO 4217 code |
| `currency_exponent` | Registry-derived decimal places for the supported currency's minor unit |
| `treatment` | Included, excluded, or unclassified |
| `formula_id` | Versioned machine-stable formula identifier |
| `formula` | Human-auditable formula text |
| `explanation` | Why the treatment was used |
| `citation_ids` | Evidence supporting the input |
| `superseded` | Line belongs to replaced evidence or logic |

## Currency scenario

Amounts and the currency exponent are maintained separately for documented remaining subscription, excluded
non-subscription fees, unclassified fees, and minimum/maximum coverage scenarios. All are
nonnegative integer minor units.

The exact-money registry currently supports AUD, BHD, CAD, CHF, CLP, EUR, GBP, INR, JOD, JPY, KRW,
KWD, OMR, TND, USD, and VND. Inputs outside that explicit set fail validation; the system never
guesses a currency exponent. Minor-unit amounts are bounded to 9,000,000,000,000,000 so PostgreSQL,
JSON, and JavaScript round trips remain exact.

## Blocker

`open` prevents readiness. `resolved` preserves the issue and records that it no longer blocks.
`accepted_risk` preserves an explicit human decision; it must not be presented as if the issue
never existed. A blocker may cite zero sources when the missing source is itself the problem.

## Hashes

- Document hash proves the input file identity.
- Quote hash proves the citation text identity.
- Calculation input hash proves normalized engine inputs.
- Calculation result hash proves the engine output.
- Export snapshot hash proves all canonical packet content shared across artifacts.
- Audit-event hashes prove ordered event integrity.
