# Architecture

## Design principle

RescueDesk is a modular monolith. Contract evidence, review state, deterministic rules, exports,
and the HTTP layer are separated by explicit contracts, but they deploy together. This keeps the
system understandable and transactional while leaving clear seams for future extraction adapters.

```text
PDF upload
   |
   v
file safety -> immutable document/pages -> extraction proposal
                                         |
                                         v
                                  human evidence review
                                         |
                                         v
                              deterministic calculation engine
                                         |
                            blockers + explicit approval gate
                                         |
                                         v
                                  immutable ExportPacket
                         /            /          \             \
                internal PDF  customer PDF  evidence CSV  canonical JSON
```

## Technology boundary

| Area | Company-confirmed public signal | RescueDesk choice |
|---|---|---|
| Frontend | React, Next.js, JavaScript/TypeScript, responsiveness, accessibility | Implemented Next.js + React + strict TypeScript Evidence Workbench |
| Backend | Python, robust APIs, accounting edge cases, testing and monitoring | Python 3.13 + FastAPI |
| Data | PostgreSQL, SQL, schema design and migrations | PostgreSQL + SQLAlchemy + Alembic |
| Infrastructure | AWS appears in a public engineering posting | AWS-compatible container design; no external deployment yet |
| AI | AI-native product and AI-tooling emphasis | Optional local structured extraction adapter; never arithmetic or approval |

FastAPI, SQLAlchemy, Alembic, the data model, formulas, visual system, and all product behavior are
portfolio implementation choices. They are not claims about DualEntry's exact production stack.

## Module responsibilities

- `extraction/`: rejects unsafe PDFs, extracts immutable pages, proposes deterministic facts, and
  optionally validates structured output from a local Ollama adapter. Money normalization uses the
  same explicit currency/exponent registry as the domain and rejects excess fractional precision.
- `domain/evidence.py`: proves a quote exactly matches page text and produces its quote hash.
- `domain/calculations.py`: category handling, date rules, proration, currency separation, and
  reproducible input/result hashes.
- `domain/workflow.py`: legal state transitions and approval role gates.
- `domain/audit.py`: forward-linked, tamper-evident event hashes.
- `exports/contracts.py`: frozen DTOs, referential integrity, canonical payload, snapshot hash.
- `exports/renderers.py`: deterministic bytes for the four export types.
- `models.py`: persistence only; business rules must not be hidden in ORM callbacks.
- `services/`: organization-scoped orchestration, document integrity checks, revision snapshots,
  readiness, atomic idempotency, audit appends, and runtime artifact persistence.
- `routers/`: authentication, tenancy, request validation, and HTTP transaction boundary.
- `frontend/src/components/workbench-*`: evidence, PDF, economics, readiness/export, and audit
  surfaces backed only by the authenticated API.

## Export isolation

The export package imports no ORM models. A service must create `ExportPacket` from one consistent
database transaction or database snapshot. This prevents lazy-loading, later database changes, or
renderer-specific queries from causing the PDF, CSV, and JSON to disagree.

`ExportPacket.snapshot_hash` is SHA-256 over canonical UTF-8 JSON with sorted keys and compact
separators. Tuple members with stable identifiers are sorted before hashing. The hash excludes the
derived hash itself and artifact-specific metadata.

## Consistency and concurrency

- Case updates use explicit revisions and optimistic version checks.
- The workbench reads case detail, evidence, economics, readiness, audit, and export metadata from
  one organization-scoped aggregate endpoint. It acquires the case lifecycle lock before reading,
  so the browser never presents fields assembled from different concurrent revisions as one
  authoritative snapshot.
- Same-case writes acquire the case lifecycle lock before child records, preventing review,
  processing, transition, and export races from taking locks in conflicting orders.
- Mutating requests reserve an organization/endpoint/idempotency key inside the same transaction as
  their side effects and stored response; failures roll back the reservation and staged files.
- An export is generated from one revision, never from a live mix of revisions.
- Uploads use content hashes and organization/revision uniqueness constraints.
- Repeated jobs must be idempotent by document hash, revision, and operation identifier.
- Approval is invalidated when evidence or calculation inputs change.
- Any material input mutation retires the revision's current artifacts. A workflow-only move from
  approved to exported preserves the unchanged approved artifact that authorizes that transition.
- At most one unsuperseded artifact exists for a revision and format, and export creation is
  serialized against reopen and other same-case exports.
- Re-exporting the same packet yields identical logical content and byte-identical PDF/JSON/CSV
  output for the current renderer version.

## Failure behavior

- Failed extraction rolls back partial pages, runs, assertions, and its idempotency reservation;
  it then records one clean failed state and can be retried from the UI.
- Missing OCR becomes `needs_ocr`, not empty evidence.
- Missing or ambiguous facts remain blockers.
- A calculation that cannot justify proration returns `None` for the result and a blocker.
- A calculation whose input hash no longer matches the current reviewed fee ledger is stale,
  non-reproducible, and cannot support approval until rerun.
- Unknown payment status is not silently interpreted as unpaid; it remains unclassified and blocks
  readiness until a reviewed scenario input is supplied.
- No conversion is performed across currencies.
- Artifact persistence must happen after successful rendering and hash calculation; partial files
  must not become approved artifacts.
- Approved or exported revisions cannot be edited in place. Reopening clones the revision, retains
  the frozen approval snapshot, and supersedes earlier artifacts.

## Security boundary

The browser and uploaded document are untrusted. Extracted document text is data, never a prompt or
instruction with authority. The API enforces organization membership and roles server-side. See
[PRIVACY-THREAT-MODEL.md](PRIVACY-THREAT-MODEL.md).
