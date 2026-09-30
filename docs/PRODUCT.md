# Product specification

## Problem

ERP switching conversations can begin with a deceptively simple question: how much contractual
commitment remains? The answer is commonly scattered across order forms, amendments, renewal
language, service periods, fee schedules, and termination clauses. A spreadsheet total without
source evidence cannot show whether the right documents or assumptions were used.

RescueDesk makes the review process inspectable. It preserves exact evidence, separates machine
proposals from human decisions, calculates only reviewed inputs, and packages the result without
pretending to make a legal or commercial determination.

## Users and jobs

| User | Job to be done | RescueDesk responsibility |
|---|---|---|
| Uploader | Supply a safe contract package | Validate type, size, hash, and processing status |
| Analyst | Establish reviewed facts | Compare proposals with page evidence; accept, correct, or reject |
| Approver | Decide whether the internal packet is ready | Resolve blockers and record an explicit approval action |
| Auditor | Reproduce what was known at export time | Inspect revision, source hashes, formulas, decisions, and audit chain |
| Customer-facing operator | Explain the scenario without overstating it | Use the customer brief and point to exact source notes |

## End-to-end workflow

1. Create a case and revision.
2. Explicitly classify and upload a public, synthetic, or properly redacted PDF; there is no silent
   default source classification.
3. Perform file-safety checks and extract page text.
4. Produce structured fact proposals with document, page, quote, offsets, extractor, and confidence.
5. Verify every quote against the immutable page text.
6. Require a human to accept, correct, or reject each mandatory fact; a changed value remains an
   explicit human assumption throughout its correction lineage.
7. Bind each active reviewed fee to one exact, category-compatible money fact, recover mistakes by
   superseding rather than deleting them, and run deterministic calculations.
8. Keep unclear fees, unsupported proration, and mixed currencies visible as blockers.
9. Require an approver to authorize the internal packet.
10. Render four artifacts from one immutable export snapshot.
11. Preserve hashes and audit events so the packet can be reproduced.

## Scope

The complete demonstration includes contract intake, evidence-preserving extraction, review,
calculation, blockers, approval, revisioning, audit history, and exports. It should remain useful
when the document is incomplete or contradictory; uncertainty is a product state, not an error to
hide.

## Non-goals

- Legal interpretation or advice
- Accounting or financial advice
- Contract enforceability or termination determinations
- Eligibility or credit decisions for any external program
- Currency conversion
- Electronic signatures, email sending, CRM mutation, or contract cancellation
- A replacement for an ERP or document-management platform

## Product invariants

- Evidence is immutable within a revision.
- Corrections create review history; they do not rewrite the original proposal.
- Accepted and corrected facts must satisfy the semantic key's canonical typed-value contract.
- A corrected value that differs from its source proposal remains an explicit assumption in the
  UI, readiness result, approval snapshot, export provenance, and every reopened revision.
- A reviewed fee's category, amount, currency, and supplied cadence must match compatible money
  evidence exactly, and one current money assertion cannot substantiate two active fees.
- Superseded fee rows remain in history but do not affect current totals, readiness, or calculations.
- `paid` and `unpaid` are explicit scenario inputs; an unknown payment status stays unclassified
  and blocking rather than being treated as unpaid.
- Every exported fact and calculation line references one or more citations.
- AI cannot calculate, approve, or move a case past a human gate.
- Money is stored in integer minor units and calculated with `Decimal`.
- Multiple currencies are reported independently.
- Open blocking findings prevent readiness.
- Superseded material is visible and unmistakably labeled.
- All export formats share the same canonical snapshot hash.
- Approval seals the case identity, revision, deterministic calculation, and evidence snapshot;
  an older approval event cannot authorize a newer or changed packet.
- One workbench refresh represents one case-locked database view; the UI does not combine
  independently timed evidence, calculation, readiness, or export responses.

## Completion criteria

Completion must be demonstrated rather than inferred. The full project is complete only when:

- The documented workflow works through the real API and UI.
- Synthetic and public fixtures exercise clean, missing, conflicting, amended, scanned, and
  spreadsheet-injection cases.
- Source spans and hashes are verified, not merely displayed.
- Calculation fixtures have independent expected results and boundary tests.
- Unauthorized approval and cross-organization access are rejected.
- Exported PDFs parse, CSVs remain formula-safe, and JSON hashes reproduce.
- Desktop and mobile browser checks pass.
- Documentation matches the actual commands and runtime behavior.
