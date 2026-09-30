# Three-minute demonstration

## Start cleanly

```bash
docker compose up --build --detach --wait
```

Open `http://127.0.0.1:3000` and sign in as
`analyst@rescuedesk.local` / `DemoPassword!2026`.

The replay-safe seed uses only `fixtures/contracts/clean_standard.pdf`, an invented contract. It
creates two complementary Northstar matters:

| Matter | State | Why it exists |
|---|---|---|
| `Northstar guided review - 1 fact left` | Evidence review; exactly one blocker | A short, honest success path. The analyst must decide the cited 90-day cancellation-notice proposal. |
| `Northstar completed exit packet` | Exported; zero blockers | An inspectable finished outcome with seeded synthetic approver-role history and four packet formats. |

Both matters contain the same reviewed USD 120,000 subscription obligation and deterministic
USD 48,657.53 remainder as of August 20, 2026. No real company, customer, or contract data is used.
Preloaded events are attributed to `Synthetic Demo Analyst` and `Synthetic Demo Approver`; the
interactive analyst `Avery Analyst` appears only after you record the remaining live decision.

If this checkout previously ran the older one-case seed, reset only its disposable local demo
volumes once, then rebuild:

```bash
docker compose down --volumes
docker compose up --build --detach --wait
```

Do not remove volumes in an environment containing data that must be retained.

## 0:00-0:25 — Understand the story

The Matters page presents the route before the data table:

- **Problem:** a company cannot safely leave its ERP until contract obligations are proved.
- **User:** an implementation analyst coordinating evidence, economics, and approval.
- **Outcome:** a cited, reproducible, approver-controlled contract-exit packet.

Open **Northstar guided review - 1 fact left**, then select **Start guided demo**. The guide supports
mouse, touch, Tab navigation, direct section jumps, Left/Right Arrow progression, Escape dismissal,
and an always-available restart. It stacks into one column on a narrow viewport.

## 0:25-1:05 — Make one live decision against synthetic evidence

The guided case opens the only unfinished proposal first: **Renewal · notice days**.

1. Read the proposed `90 days` value.
2. Open the page-1 citation and compare the exact cancellation-notice sentence in the protected
   source PDF.
3. Inspect the quote offsets plus document, page-text, and quote hashes.
4. Record **Accept** with a short reason only if the source supports the proposal.

That is one meaningful decision, not a bypass. The API still requires exact active evidence,
optimistic assertion versioning, a named reviewer, and a reason. A changed value must be recorded as
an explicit assumption; it cannot be relabelled as source-confirmed.

After the decision, evidence pending changes from one to zero and readiness recomputes from one
blocker to zero. The case is ready for the next controlled workflow transition; it is not silently
approved or exported.

## 1:05-1:40 — Inspect deterministic economics

Open **Economics**. Confirm:

- the reviewed USD 120,000.00 subscription input and exact money citation;
- the January 15, 2026 to January 15, 2027 service interval;
- USD 48,657.53 remaining as of August 20, 2026;
- the `remaining-subscription-v2` engine and `contract-daily-half-open-v1` formula;
- the explicit USD 0.00-48,657.53 scenario range; and
- versioned input and result hashes.

Extraction may propose facts. It never performs this calculation.

## 1:40-2:15 — See the approver gate stay real

Open **Readiness & packet**. Before the final evidence decision, the only blocker is named and the
internal-review transition stays disabled. After the decision, the evidence and calculation controls
turn complete, but an analyst still cannot cross the separate approver-role gate.

Nothing is emailed, submitted, or sent to a vendor. Packet generation is local and remains locked
until the approver records a reasoned decision.

## 2:15-2:45 — Inspect the history

Open **Audit**. Expand an event and inspect its actor, correlation ID, timestamp, before/after
payload, previous hash, and event hash. The filtered case ledger exposes the recorded
organization-chain pointers for inspection; it does not claim that the browser view independently
recomputes the complete organization chain.

## 2:45-3:00 — Compare the completed outcome

Return to **Matters** and open **Northstar completed exit packet**. In **Readiness & packet** verify:

- zero open blockers and an `Exported` workflow state;
- a frozen revision snapshot produced by the seeded approver-role transition;
- Internal review PDF, switching-evidence PDF, evidence CSV, and canonical JSON;
- `Seeded synthetic approver-role snapshot` on all four artifacts; and
- the same packet snapshot hash across every format.

Open **Audit** to see the seeded role-separated analyst and approver history. This completed example
was produced programmatically through the real service layer and gates; it is not a front-end-only
success label and is not presented as an operator-performed approval.

## Full mutable lifecycle

The seeded guide intentionally stops at internal readiness. To exercise the remaining lifecycle on
synthetic data:

1. Transition the guided case to **Ready for internal review** with an analyst reason.
2. Sign out and sign in as `approver@rescuedesk.local` with the same demo password.
3. Inspect evidence, calculation, findings, and audit history, then approve with a reason.
4. Generate all four formats and verify their shared packet snapshot hash.
5. Mark the case exported only after an approved artifact exists.
6. Reopen evidence review to create a new revision, freeze the prior approved revision, retire its
   artifacts from the current view, and preserve explicit-assumption lineage.

For destructive or edge-case lifecycle checks, use a newly created synthetic matter. The browser
test suite does this so the two seeded demo cases stay unchanged.

## What the demo shows

- The local synthetic fixture completes the implemented intake, evidence, calculation, review,
  approval, revision, audit, and export workflow.
- The guided case has exactly one live operator decision against synthetic evidence before internal
  readiness.
- The completed case contains four formats generated from seeded synthetic approver-role history,
  sharing one verified packet hash.
- The named deterministic scenario reproduces its checked-in expected result.

It does not show customers, revenue, adoption, legal accuracy, eligibility, promised savings, or a
production deployment.
