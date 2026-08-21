# Testing and verification

## Principle

A green test is evidence only for the behavior it actually exercises. RescueDesk keeps calculation,
evidence, workflow, tenancy, API, export, and browser checks separate so a passing renderer test
cannot be mistaken for full product completion.

## Required gates

```bash
cd backend
uv run ruff check src tests
uv run mypy src/rescue_desk
uv run pytest
```

Run with coverage when assessing a release:

```bash
uv run pytest --cov=rescue_desk --cov-report=term-missing
```

Coverage percentage is diagnostic; it does not replace boundary, mutation, or adversarial tests.

## Test layers

| Layer | Required evidence |
|---|---|
| Calculation unit tests | exact minor-unit results, round-half-up boundaries, date clamping, invalid inputs, category handling, mixed currency |
| Evidence unit tests | exact quote/offset match, blank/overflow rejection, tamper-sensitive quote hash |
| Workflow unit tests | every legal transition, every illegal transition, role gates, blocker gates, approval invalidation |
| Audit unit tests | valid chain, changed payload, missing/reordered event, incorrect previous hash |
| Export contract tests | timezone, unique IDs, reference integrity, hash formats, stable ordering, content sensitivity |
| Renderer tests | byte determinism, PDF parseability, disclaimers, citations, formulas, blockers, superseded status, shared hash |
| CSV adversarial tests | `=`, `+`, `-`, `@`, tab, carriage return, quote, comma, and newline handling |
| API integration tests | authentication, two-tenant isolation, optimistic concurrency, transaction rollback, idempotency |
| Fixture tests | clean, amendment, conflict, missing clause, scanned/needs-OCR, malformed, and multi-currency contracts |
| Browser tests | real desktop/mobile UI and API, authenticated PDF canvas, citation sync, exact economics, keyboard focus, contrast, status visibility, audit detail, and export inventory |

## Export-specific proof

`backend/tests/exports` verifies:

- the canonical hash ignores input tuple order but changes with packet content;
- missing citation references and duplicate identifiers fail closed;
- tampered quote hashes and mismatched offsets fail closed;
- repeated PDF/CSV/JSON generation is deterministic;
- both PDFs can be parsed and expose disclaimer, integrity, evidence, formulas, blockers, and
  superseded material;
- JSON preserves the complete canonical snapshot and hashes; and
- CSV cells neutralize spreadsheet-formula prefixes while preserving traceability.

## Manual release audit

1. Start from a clean runtime database.
2. Load only the named synthetic fixture.
3. Record exact git commit and dependency lock hash.
4. Run all automated gates with `set -o pipefail` and preserve output.
5. Complete the three-minute demo using the real application.
6. Download all four exports and independently compare snapshot hashes.
7. Confirm no private data, credentials, external messages, or network model calls occurred.
8. Run desktop and mobile browser checks after UI implementation.
9. Update documentation only with executed results and observed timings.

## Evidence boundary

No single gate proves the whole product. Unit/export tests establish deterministic behavior;
SQLite integration tests establish API and transaction behavior; Alembic and PostgreSQL runs
establish the declared database contract; container health and recreation establish packaging and
runtime persistence; and Playwright establishes rendered end-user behavior against the live API.
Completion claims require all of them.

## Executed release verification - 2026-08-21

The final guided-demo tree produced the following evidence:

| Gate | Observed result |
|---|---|
| Backend default suite | 265 passed, 6 PostgreSQL-only tests skipped |
| Backend coverage | 90.67%, above the enforced 90% release threshold |
| PostgreSQL concurrency suite | 6/6 passed against an isolated migrated PostgreSQL database |
| Backend static gates | Ruff lint clean; 76 files format-clean; strict mypy clean across 45 source files |
| Frontend component/unit suite | 85/85 passed across 13 files |
| Frontend static/build gates | ESLint zero warnings; strict TypeScript clean; Next.js production build passed |
| Full live browser suite | 15 passed; 7 intentional cross-viewport skips |
| Final clean-baseline browser check | 8/8 read-only desktop/mobile tests passed |
| Dependency and secret checks | gitleaks clean; npm audit zero vulnerabilities; pip-audit found no known third-party vulnerabilities |

The full live browser path used the rebuilt digest-pinned Compose stack. Its read-only desktop and
mobile paths covered the two-step recruiter route, guided-tour keyboard/direct navigation,
authenticated PDF rendering, citation synchronization, exact scenario figures, one-action
readiness, seeded completed export inventory, audit details, accessibility checks, and horizontal
overflow. Its isolated mutation paths covered source and fee supersession, stale-view retry
protection, approver gating, all four downloaded formats, independent artifact-byte hashes, one
shared packet hash, exported state, and explicit reopen. The seven skips are tests intentionally
assigned to one viewport, not failures.

The API and web containers ran as UID 10001. PostgreSQL, API, and web health checks passed on
loopback, and live Alembic check reported no pending operations. The container build resolved the
pinned Python, Node, and PostgreSQL base-image digests rather than mutable tags alone.

Desktop 1600x1050 and mobile 390x844 guided-workbench screenshots were inspected after the
automated run. The desktop artifact committed under `docs/assets/` clearly shows the problem flow,
one pending decision, one blocker, protected source, and seeded reviewer provenance. The mobile tour
stacks into one column without page-level overflow. Tested states had one main landmark, no browser
console/page errors, no serious or critical axe violations, and a readable keyboard/hover control
state. The completed internal and customer PDFs were also raster-inspected after the copy change;
their seeded synthetic approver-role banner fits without clipping and both identify
`Synthetic Demo Approver`.

The final cleanup removed only disposable synthetic Playwright data and recreated the named demo
volumes. The running baseline contains exactly two Northstar matters: the guided matter has one
pending evidence decision and one blocker; the completed matter is exported with zero blockers,
seeded synthetic approver-role history, and four current artifacts sharing one packet hash. All
preloaded review decisions are attributed to `Synthetic Demo Analyst`; `Avery Analyst` has no
programmatically seeded decision history.
