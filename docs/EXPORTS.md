# Export contract

## Stable public functions

```python
build_internal_review_pdf(packet: ExportPacket) -> bytes
build_customer_explanation_pdf(packet: ExportPacket) -> bytes
build_evidence_csv(packet: ExportPacket) -> bytes
build_machine_json(packet: ExportPacket) -> bytes
content_type_for(kind: ExportKind) -> str
extension_for(kind: ExportKind) -> str
```

These functions are credential-free and perform no database, network, filesystem, or clock reads.
Their only input is a frozen `ExportPacket` DTO. The export package does not import ORM models.

## Mapping rule

The application service must load one organization-authorized revision in a consistent database
transaction, materialize all reviewed facts, calculation lines, scenarios, blockers, and source
citations, then construct `ExportPacket`. It must not query separately for each renderer.

The service recomputes readiness while holding the case lifecycle lock and accepts only the latest
calculation whose input hash matches the current active fee ledger. A missing or stale calculation
cannot be exported. Reviewed facts include their source/human origin, reviewer and reason,
assumption/evidence basis, and extraction model/prompt/code/output provenance where applicable.

Construction fails when:

- required identifiers or text are blank;
- timestamps lack a timezone;
- hashes are malformed or a quote hash does not match;
- citation offsets do not span the quote length;
- IDs are duplicated;
- a fact or calculation references a missing citation;
- currency codes or amounts are invalid; or
- a currency exponent is invalid or disagrees between a calculation line and its scenario; or
- the packet has no citations or scenarios.

## Artifact semantics

### Internal review PDF

Includes every fact, full formula detail, calculation hashes, assumptions, all blockers, all source
quotes, superseded indicators, and the packet hash. It is still a demonstration artifact, not an
approval by itself.

### Customer explanation PDF

Uses plain language and customer-visible facts/blockers. It includes formula explanations and
source notes while explicitly refusing to determine enforceability, eligibility, credits, or a
recommended action. It labels source-confirmed evidence and explicit human assumptions, but omits
internal reviewer identifiers/reasons and extraction-run/model/hash details; those remain in the
internal and machine-oriented artifacts. Unreviewed proposals are explicitly labelled as pending
human review and are never described as source-confirmed.

### Evidence CSV

Contains one row per citation, the objects that reference it, linked formula detail, linked blocker
context, document and quote hashes, offsets, superseded status, disclaimer, and snapshot hash.
Every cell beginning with `=`, `+`, `-`, `@`, tab, or carriage return is prefixed with a single
quote to prevent spreadsheet formula execution.

### Machine-readable JSON

Contains the complete canonical snapshot, disclaimer, snapshot hash, calculation hashes, hashing
algorithm, and canonicalization rule. Keys are sorted and output uses compact UTF-8 JSON plus one
terminal newline.

## Integrity verification

1. Read `snapshot_sha256` from the machine JSON.
2. Canonicalize its `snapshot` value using UTF-8 JSON, sorted keys, compact separators, and Unicode
   characters unescaped.
3. SHA-256 the resulting bytes.
4. Confirm the digest equals `snapshot_sha256`.
5. Confirm the same digest appears in the PDFs and every CSV row.
6. Confirm calculation input/result hashes match the stored calculation record.

PDF metadata uses ReportLab invariant mode, standard fonts, fixed metadata, and page compression so
the same packet and renderer version produce byte-identical PDFs.

## Persistence behavior

The service persists artifact kind, revision and calculation IDs, packet and revision snapshot
hashes, artifact SHA-256, renderer version, creator/preparer, approval association, and timestamp.
It writes `0600` bytes to a sibling temporary path, fsyncs, atomically renames, and registers the
result for transaction-rollback cleanup. Document and export directories share one persistent
runtime volume. Listing and download refuse missing, symlinked, or hash-mismatched bytes.

Approval is copied only from an audit event whose case identity, revision ID, revision snapshot
hash, calculation, and case version match the frozen approval. Rendering an unapproved
packet never upgrades it, and the `exported` workflow transition accepts only a current approved
artifact whose revision snapshot and approval case version still match.

Material evidence, review, fee, calculation, finding, or reopen changes retire all prior current
artifacts for that revision. Retired rows remain visible as historical metadata but cannot be
downloaded or represented as current approval. The unchanged approved artifact remains current
when the case moves from internal approval to exported status.
