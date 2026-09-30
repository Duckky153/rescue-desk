# RescueDesk

RescueDesk helps an implementation analyst work out what a company still owes on its current ERP
software contract before it switches systems. It reads the contract PDF, shows every proposed fact
next to the exact page it came from, has a person confirm it, and turns the confirmed facts into a
review packet that an approver must sign off on.

![RescueDesk guided evidence workbench](docs/assets/rescuedesk-guided-workbench.png)

- **Page-cited extraction:** every proposed fact keeps its document, page number, exact quote with
  character offsets, and a hash of that quote.
- **A person decides each fact:** a reviewer accepts, corrects, or rejects it with a reason. A
  corrected value stays labelled as an assumption, not as source-confirmed.
- **Fixed-formula costs:** money is calculated with exact decimals and named, versioned formulas.
  In the demo contract, USD 48,657.53 of a USD 120,000.00 annual subscription remains in the
  current service year as of August 20, 2026.
- **Approval stays blocked until the case is clean:** any open blocking finding stops readiness,
  and only an approver-role account can approve.
- **Four exports from one snapshot:** an internal-review PDF, a customer explanation PDF, an
  evidence CSV, and a JSON file, all carrying the same snapshot hash.

By default the demo pulls facts out of the PDF with fixed text rules. An optional local AI model
(Ollama) can also propose facts, but it is switched off by default and never does the math or
approves anything. The demo runs on your own machine with Docker; there is no hosted version. All
contract data in the repository is synthetic, and it is not legal, accounting, or financial advice.

Built with AI assistance (Claude Code and Codex).

## Three-minute demo route

Make one live decision against synthetic evidence in the guided case, then compare it with a
completed example whose approver history and four export formats are seeded synthetic data. The
full walkthrough is in [docs/DEMO.md](docs/DEMO.md).

## What is working

- Safe PDF intake with byte hashing, active-content rejection, page extraction, and exact quote
  offsets.
- Deterministic and optional local-Ollama extraction; model output can propose facts but cannot
  calculate, approve, or change evidence.
- Analyst accept/correct/reject decisions with optimistic concurrency, retained review history,
  and lineage-aware labels that keep a changed human value visible as an explicit assumption.
- Exact-money obligation entry for supported zero-, two-, and three-decimal currencies, with
  each active reviewed fee bound to one compatible, unconsumed typed money assertion.
- Versioned `Decimal` calculations with explicit formula IDs, assumptions, blockers, and hashes.
- A role-gated workflow from draft through evidence review, internal approval, and export.
- Controlled finding disposition, recoverable fee/source supersession, retryable document
  processing, and revision cloning after an approved packet is reopened.
- A forward-linked audit ledger covering cases, documents, reviews, fees, calculations, findings,
  transitions, and exports.
- Internal-review PDF, customer explanation PDF, formula-safe evidence CSV, and canonical JSON,
  all derived from one immutable snapshot.
- A responsive Next.js Evidence Workbench for PDF-to-citation review, economics, readiness,
  packet generation, revision history, and audit inspection. Each refresh comes from one
  case-locked aggregate snapshot rather than a mixture of independent reads.
- An accessible in-product tour that explains the problem, user, workflow, and outcome; navigates
  directly among Evidence, Economics, Readiness, and Audit; and supports keyboard, touch, dismiss,
  restart, desktop, and mobile use.

## One-command demonstration

Prerequisite: Docker Desktop.

```bash
docker compose up --build --detach --wait
```

Open [http://127.0.0.1:3000](http://127.0.0.1:3000). The bootstrap migrates PostgreSQL, loads only
the checked-in synthetic Northstar fixture, and creates two replay-safe matters:

- **Northstar guided review - 1 fact left:** exactly one cited 90-day cancellation-notice decision
  remains before internal readiness.
- **Northstar completed exit packet:** zero blockers, seeded synthetic approver-role history, and
  four current formats sharing one packet snapshot hash.

| Role | Email | Password |
|---|---|---|
| Analyst | `analyst@rescuedesk.local` | `DemoPassword!2026` |
| Approver | `approver@rescuedesk.local` | `DemoPassword!2026` |
| Read-only auditor | `auditor@rescuedesk.local` | `DemoPassword!2026` |

Preloaded history is visibly attributed to `Synthetic Demo Analyst` and
`Synthetic Demo Approver`. `Avery Analyst` and `Morgan Approver` are reserved for actions a person
records through the interactive demo accounts above.

The API and interactive documentation are at
[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs). All services bind to loopback.

```bash
docker compose ps
docker compose logs api
docker compose down
```

For this synthetic demo only, `docker compose down --volumes` resets the database and runtime
artifacts. Do not use that command for an environment containing data that must be retained.

## Verification

```bash
cd backend
uv sync --all-groups --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy src/rescue_desk
uv run alembic upgrade head
uv run pytest --cov=rescue_desk --cov-report=term-missing --cov-fail-under=90
uv run alembic check

cd ../frontend
npm ci
npm run lint
npm run typecheck
npm test
npm run build
npm run test:e2e
```

The Playwright suite drives the real Docker API and UI on desktop Chromium and a Pixel 7 viewport.
It verifies authenticated PDF rendering, evidence synchronization, exact scenario figures,
the guided tour, one-action readiness, the seeded completed export inventory, audit details,
keyboard-accessible controls, serious/critical axe findings, and page-level horizontal overflow.
Its stateful path also drives a unique synthetic
matter through upload, processing, review, fee correction, calculation, approval, all four exports,
download, exported state, and explicit reopen. Controlled mutation cases use unique synthetic names
so the two seeded Northstar matters stay unchanged.

## Architecture

```text
untrusted PDF
    -> safety and byte integrity
    -> immutable pages and evidence spans
    -> machine proposals
    -> human review
    -> deterministic exact-money calculation
    -> readiness and approver gate
    -> immutable export snapshot
       -> internal PDF / customer PDF / evidence CSV / canonical JSON
```

The FastAPI/SQLAlchemy/Alembic backend and Next.js/React/TypeScript frontend deploy as three
non-root local containers with PostgreSQL. Runtime documents and exports share one persistent
volume so an API-container recreation cannot strand artifact metadata from its bytes.

## Repository map

```text
backend/                 API, domain rules, extraction, migrations, exports, tests
frontend/                Evidence Workbench, component tests, Playwright browser tests
fixtures/contracts/      deterministic synthetic PDFs and ground truth
scripts/                 deterministic fixture generator
docs/                    product, architecture, calculations, data, security, demo, recovery
docker-compose.yml       loopback-only PostgreSQL, API, and web demonstration
.github/workflows/ci.yml backend, frontend, migration, coverage, and container gates
```

Start with [docs/DEMO.md](docs/DEMO.md) for the walkthrough,
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for system boundaries, and
[docs/PRIVACY-THREAT-MODEL.md](docs/PRIVACY-THREAT-MODEL.md) before using any document.

## Truth boundary

- A parsed value is not a reviewed fact.
- A human-supplied correction that changes the cited value is an explicit assumption, not
  source-confirmed evidence.
- A scenario is not a promise of credit, savings, eligibility, enforceability, or action.
- An exported file is not approved unless its immutable snapshot contains an approver-role event.
  The prebuilt completed case uses seeded synthetic approver-role history; only an actual operator
  action should be described as human approval.
- A superseded source or fact remains visible and is marked as superseded.
- Only synthetic, public, or properly redacted PDFs belong in this demonstration.
- No email, CRM, contract, external program, or third-party account is changed by RescueDesk.
- No customers, adoption, production deployment, or external outcomes are claimed.
