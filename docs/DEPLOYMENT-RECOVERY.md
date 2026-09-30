# Deployment and recovery runbook

## Current boundary

RescueDesk is a loopback-only local demonstration with no public deployment. The checked-in Compose
stack contains synthetic data only and exposes PostgreSQL, the API, and the web UI only on
`127.0.0.1`.

## Start and stop the complete demonstration

```bash
docker compose up --build --detach --wait
docker compose ps
```

Expected healthy endpoints:

- UI: `http://127.0.0.1:3000`
- OpenAPI: `http://127.0.0.1:8000/docs`
- liveness: `http://127.0.0.1:8000/health/live`
- database, migration, and runtime-storage readiness: `http://127.0.0.1:8000/health/ready`
- PostgreSQL: `127.0.0.1:55432`

The API bootstrap performs `alembic upgrade head`, runs the replay-safe synthetic seed when
`RESCUEDESK_SEED_DEMO=true`, and then starts Uvicorn. A second API-container start does not create a
second case, user, document, fee, calculation, or export.

```bash
docker compose logs --tail=200 api
docker compose down
```

`docker compose down` preserves both named volumes. Documents and exports live together under the
`rescuedesk-runtime` volume, so recreating the API container does not leave database artifact rows
without their bytes.

## Synthetic reset

Only when every byte is known to be disposable demonstration data:

```bash
docker compose down --volumes
docker compose up --build --detach --wait
```

This destroys the local Compose database and runtime artifacts and reconstructs them from the
current migration and checked-in fixture. Never use this reset against data that must be retained.

## Developer-mode verification

```bash
cd backend
uv sync --all-groups --locked
uv run alembic upgrade head
uv run rescuedesk-seed
uv run rescuedesk-serve
```

The `.env.example` file names every RescueDesk setting. Keep `.env`, database files, uploaded PDFs,
exports, tokens, and secrets outside git. The optional local-AI adapter is off by default and is not
required for the deterministic demo.

## Runtime integrity behavior

- Health readiness fails if PostgreSQL is unreachable, the database is not at the exact Alembic
  head, or either runtime directory cannot create and remove a `0600` probe file.
- Upload, process, calculation, review, transition, and export writes are transactional. A failed
  request rolls back both its database changes and newly staged runtime files.
- Document bytes are rehashed before processing, download, revision freeze, and export preparation.
- Export download rehashes the artifact bytes against both its row and creation-audit snapshot.
- Missing or modified bytes fail closed; stored hashes are never rewritten to hide the mismatch.

## Backup and restore scope

A consistent recovery point must include:

1. PostgreSQL;
2. immutable original documents; and
3. export artifact bytes.

A database-only restore is incomplete. To validate a restored environment:

1. restore into an isolated location;
2. apply only migrations appropriate for that database version;
3. verify every document byte hash;
4. verify page/quote hashes and audit chains;
5. download sampled exports and compare artifact SHA-256 values;
6. reconstruct sampled packets and compare snapshot hashes; and
7. run the backend, frontend, migration, and browser gates.

Never repair a mismatch by changing the stored hash. Treat it as corruption or an inconsistent
recovery point.

## Before any external deployment

External deployment would be a separate project. At minimum it requires managed encrypted
storage, TLS, secure HttpOnly session handling, a secret manager, login throttling, resource-isolated
PDF parsing, malware scanning, structured telemetry without document content, rate limits, backup
and restore proof, tenant-isolation penetration testing, dependency review, retention/deletion
policy, and legal/privacy review for the actual data controller. The local demonstration makes none
of those production-certification claims.
