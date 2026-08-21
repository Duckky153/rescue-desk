# RescueDesk

Independent demonstration project.

## Product

An ERP contract exit control room: upload a public, synthetic, or redacted contract; extract obligations with page evidence; calculate switching scenarios deterministically; review ambiguities; approve manually; export a review packet.

## Locked rules

- This is an independent project and must not imply affiliation with any company.
- Do not use third-party logos, private data, proprietary screenshots, or customer claims.
- Do not provide legal, accounting, or financial advice.
- AI can extract and explain but cannot calculate, approve, or silently overwrite evidence.
- Money uses integer cents or `Decimal`; dates and state transitions are explicit.
- Every extracted fact retains document, page, quote, extractor, confidence, and review history.
- Synthetic/public/redacted fixtures only; no secrets or user credentials.
- Backend: Python 3.13, FastAPI, PostgreSQL, SQLAlchemy, Alembic.
- Frontend: the owner-selected Evidence Workbench implemented in Next.js, React, and strict TypeScript.
- Ship tests, documentation, deterministic fixtures, accessibility, and browser proof.
- No publishing or external deployment without explicit approval.
