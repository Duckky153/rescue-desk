# Privacy and threat model

## Data boundary

The demonstration accepts only synthetic, public, or properly redacted documents. Real customer
contracts, personal information, credentials, API keys, and confidential pricing must not be used.
The safest demonstration is the included synthetic fixture set. Upload requires an explicit source
classification; selecting `redacted` records the operator's classification and does not itself
perform or certify redaction.

## Protected assets

- Original document bytes and extracted page text
- Organization membership and role assignments
- Human review decisions and corrections
- Calculation inputs, formulas, and results
- Approval identity and state
- Audit chain and export artifacts
- Runtime secrets and database credentials

## Trust boundaries

1. Browser to API: all input is untrusted.
2. Uploaded PDF to parser: document structure and text are hostile.
3. Extracted text to AI adapter: document instructions have no authority.
4. AI proposal to reviewed fact: proposal is untrusted until human action.
5. Application to database: every query is organization-scoped.
6. Snapshot to export renderer: referential integrity and hashes are validated first.
7. Export to spreadsheet/PDF reader: malicious cell values and markup must be neutralized.

## Threats and controls

| Threat | Control |
|---|---|
| Cross-organization access | Server-side organization membership checks on reads and writes; tests with two tenants |
| Privilege escalation | Role-gated transitions; approver/admin only for approval |
| Malicious or oversized PDF | Size/type limits, safe filename replacement, page limits, fail-closed parsing, and rejection of JavaScript, Launch actions, embedded files, encryption, and active annotations |
| Duplicate or swapped document | SHA-256 identity tied to organization and revision |
| Prompt injection inside a contract | Treat text solely as quoted data; fixed extraction schema; no tools, network, arithmetic, or approval available to model |
| Hallucinated evidence | Exact page-span verification and quote hash; missing spans rejected |
| Silent AI overwrite | Append review decisions; retain proposal and correction history |
| Floating-point/accounting error | Integer minor units, `Decimal`, versioned deterministic formulas |
| Formula injection in CSV | Prefix risky leading characters in every exported cell |
| Markup injection in PDF | HTML-escape every value before ReportLab paragraph rendering |
| Tampered artifact | Store snapshot and artifact SHA-256; audit the export event |
| Swapped runtime bytes | Rehash documents before processing/download/freeze and artifacts before download; reject symlink targets |
| Stale evidence | Revision IDs and conspicuous superseded labels in all formats |
| Mixed-state browser view | One case-locked aggregate workbench snapshot; same-case writers use the same lifecycle lock |
| Internal notes exposed to a customer audience | Customer PDF uses an audience-safe evidence-basis summary and omits reviewer IDs/reasons and extraction-run/model/hash metadata |
| Partial write or retry race | Same-transaction idempotency reservation/response, atomic file rename, rollback cleanup, optimistic versions, and organization-scoped locking |
| Lost browser response | One durable idempotency key per user intent is retained across retryable network/server failures and rotated only after a definitive response |
| Clickjacking or MIME confusion | Frontend CSP `frame-ancestors`, frame denial, `nosniff`, and referrer-policy response headers |
| Secret leakage | Explicit environment variables, ignored `.env` and runtime directories, synthetic fixtures, no credentials in exports |
| Parser or dependency vulnerability | Pin bounded dependencies, run dependency/security review before external deployment |

## Data minimization and retention

- Do not log document text, quotes, tokens, passwords, or raw request bodies.
- Logs may contain case/revision IDs, operation names, durations, status codes, and correlation IDs.
- Keep uploads outside git and web-static directories.
- Delete demonstration runtime data through an explicit organization/case retention operation, not
  an unscoped filesystem command.
- Backups inherit the same sensitivity and deletion policy as the primary database.
- Export bundles should contain only the reviewed evidence necessary for their audience.

## Authentication and session guidance

- Passwords use Argon2 and are never logged.
- Production secrets must come from a secret manager, not `.env` files in the image.
- The local bearer token has a bounded expiry and is kept in browser session storage. Issuer/audience
  validation, rotation, revocation, and secure HttpOnly cookies remain external-deployment gates.
- TLS and secure cookies are mandatory before external access.
- Rate limits and login throttling are mandatory before internet exposure.

## Known demonstration limitations

The local demonstration build is not a production security certification and is not meant to
process real contracts. Before external deployment, perform dependency scanning, parser sandbox
review, backup/restore proof, penetration testing of tenancy and authorization, and privacy/legal
review appropriate to the actual data controller.
