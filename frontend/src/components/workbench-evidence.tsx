"use client";

import dynamic from "next/dynamic";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useAuth } from "@/components/auth-provider";
import {
  getDocumentPages,
  processContract,
  reviewAssertion,
  supersedeContract,
  uploadAndProcessContract,
  type DocumentPage,
  type WorkbenchAssertion,
} from "@/components/workbench-api";
import {
  errorMessage,
  evidenceHasActiveDocument,
  evidenceUsesOnlySupersededDocuments,
  humanize,
} from "@/components/workbench-utils";
import { ReviewStatusPill } from "@/components/status-pill";
import type { DocumentSummary } from "@/types/api";
import type { CaseStatus } from "@/types/api";
import { stableIntentFingerprint } from "@/lib/api";
import { canReviewEvidence, canUploadDocuments, isEvidenceMutable } from "@/lib/permissions";
import styles from "./workbench.module.css";

const WorkbenchPdfViewer = dynamic(() => import("@/components/workbench-pdf-viewer"), {
  ssr: false,
  loading: () => <div className={styles.documentLoading}>Preparing document viewer…</div>,
});

type ReviewFilter = "all" | WorkbenchAssertion["review_state"];

interface WorkbenchEvidenceProps {
  caseId: string;
  caseStatus: CaseStatus;
  caseVersion: number;
  documents: DocumentSummary[];
  assertions: WorkbenchAssertion[];
  mutationsBlocked?: boolean;
  onRefresh: () => Promise<void>;
  onRecoveryRefresh?: () => Promise<void>;
}

export function filterAssertions(
  assertions: WorkbenchAssertion[],
  query: string,
  filter: ReviewFilter,
): WorkbenchAssertion[] {
  const normalizedQuery = query.trim().toLowerCase();
  return assertions.filter((assertion) => {
    if (!assertion.is_current) return false;
    if (filter !== "all" && assertion.review_state !== filter) return false;
    if (!normalizedQuery) return true;
    return [assertion.semantic_key, assertion.display_value, assertion.raw_value]
      .join(" ")
      .toLowerCase()
      .includes(normalizedQuery);
  });
}

function confidenceLabel(confidence: string): string {
  const value = Number(confidence);
  if (!Number.isFinite(value)) return confidence;
  return `${Math.round(value * 100)}%`;
}

type EvidenceBasis = WorkbenchAssertion["evidence_basis"] | "inactive_evidence";

function effectiveEvidenceBasis(
  assertion: WorkbenchAssertion,
  documents: DocumentSummary[] = [],
): EvidenceBasis {
  // Check the explicit flag independently so malformed provenance can never make a
  // human-supplied assumption look like an ordinary source-confirmed fact.
  if (assertion.assumption) return "explicit_assumption";
  if (evidenceUsesOnlySupersededDocuments(assertion.evidence, documents)) {
    return "inactive_evidence";
  }
  return assertion.evidence_basis;
}

function evidenceBasisLabel(basis: EvidenceBasis): string {
  switch (basis) {
    case "source_evidence":
      return "Source-confirmed";
    case "explicit_assumption":
      return "Explicit assumption";
    case "rejected_source":
      return "Rejected source";
    case "invalid_review":
      return "Invalid review";
    case "inactive_evidence":
      return "Superseded source";
    case "pending_review":
      return "Pending review";
  }
}

function evidenceBasisTone(basis: EvidenceBasis): "success" | "warning" | "danger" {
  if (basis === "source_evidence") return "success";
  if (basis === "pending_review" || basis === "explicit_assumption") return "warning";
  return "danger";
}

function assertionProvenanceSummary(
  assertion: WorkbenchAssertion,
  documents: DocumentSummary[] = [],
): string {
  const basis = effectiveEvidenceBasis(assertion, documents);
  const citations = `${assertion.evidence.length} citation${assertion.evidence.length === 1 ? "" : "s"}`;
  if (basis === "pending_review") {
    return `${humanize(assertion.source)} proposal · ${confidenceLabel(assertion.confidence)} extraction confidence · ${citations}`;
  }
  const reviewer = assertion.reviewed_by ?? "Reviewer not recorded";
  if (basis === "explicit_assumption") return `Human-supplied · recorded by ${reviewer}`;
  if (basis === "source_evidence") return `${citations} · confirmed by ${reviewer}`;
  if (basis === "inactive_evidence") return `Do not use · all cited sources are superseded`;
  if (basis === "rejected_source") return `Do not use · reviewed by ${reviewer}`;
  return "Do not rely on this value · valid review provenance is missing";
}

function citationHeading(basis: EvidenceBasis): string {
  switch (basis) {
    case "source_evidence":
      return "Exact source evidence";
    case "explicit_assumption":
      return "Referenced source context — not proof of this assumption";
    case "rejected_source":
      return "Rejected source evidence";
    case "invalid_review":
      return "Attached source context — review is invalid";
    case "pending_review":
      return "Proposed source evidence";
    case "inactive_evidence":
      return "Historical source evidence — inactive";
  }
}

function missingEvidenceMessage(basis: EvidenceBasis): string {
  switch (basis) {
    case "source_evidence":
      return "No exact source span is attached. This source-confirmed record has incomplete provenance.";
    case "explicit_assumption":
      return "No source span is attached. This remains an explicit assumption, not a source-confirmed fact.";
    case "rejected_source":
      return "No source span is attached to the rejected proposal.";
    case "invalid_review":
      return "No source span is attached and valid review provenance is missing.";
    case "pending_review":
      return "No exact source span is attached. This proposal cannot be accepted as source-confirmed.";
    case "inactive_evidence":
      return "All source spans are historical because their documents were superseded.";
  }
}

function EvidenceBasisNotice({
  assertion,
  documents,
}: {
  assertion: WorkbenchAssertion;
  documents: DocumentSummary[];
}) {
  const basis = effectiveEvidenceBasis(assertion, documents);
  const descriptions: Record<EvidenceBasis, string> = {
    pending_review: "This extracted value has not yet been confirmed by a reviewer.",
    source_evidence: "A reviewer confirmed this value against the attached source evidence.",
    explicit_assumption:
      "This is a human-supplied scenario assumption, not a source-confirmed fact.",
    rejected_source: "A reviewer rejected this extracted value. Do not use it as a fact.",
    invalid_review: "Valid review provenance is missing. Do not rely on this value.",
    inactive_evidence:
      "This reviewed value cites only superseded documents. It is historical and cannot support a current decision.",
  };
  const reviewed = basis !== "pending_review";

  return (
    <div
      className={`alert ${evidenceBasisTone(basis)} ${styles.evidenceBasisNotice}`}
      data-evidence-basis={basis}
    >
      <strong>{evidenceBasisLabel(basis)}</strong>
      <p>{descriptions[basis]}</p>
      {reviewed ? (
        <dl>
          <div>
            <dt>Reviewed by</dt>
            <dd>{assertion.reviewed_by ?? "Reviewer not recorded"}</dd>
          </div>
          <div>
            <dt>Review reason</dt>
            <dd>{assertion.review_reason ?? "Review reason not recorded"}</dd>
          </div>
        </dl>
      ) : null}
    </div>
  );
}

export function AssertionReviewForm({
  assertion,
  documents = [],
  canReview,
  unavailableMessage = "Your role can inspect evidence but cannot record review decisions.",
  onReviewed,
}: {
  assertion: WorkbenchAssertion;
  documents?: DocumentSummary[];
  canReview: boolean;
  unavailableMessage?: string;
  onReviewed: () => Promise<void>;
}) {
  const assertionHasFinalDecision = ["accepted", "corrected", "rejected"].includes(
    assertion.review_state,
  );
  const lineageRequiresAssumption =
    assertion.assumption || assertion.evidence_basis === "explicit_assumption";
  const hasActiveExactEvidence = evidenceHasActiveDocument(assertion.evidence, documents);
  const acceptIsSupported =
    hasActiveExactEvidence && assertion.review_state !== "conflicting";
  const correctionMustBeAssumption =
    lineageRequiresAssumption || !hasActiveExactEvidence;
  const [decision, setDecision] = useState<"accept" | "correct" | "reject">(
    assertionHasFinalDecision || !acceptIsSupported ? "correct" : "accept",
  );
  const [reason, setReason] = useState("");
  const [correctedJson, setCorrectedJson] = useState(
    JSON.stringify(assertion.normalized_value, null, 2),
  );
  const [assumption, setAssumption] = useState(correctionMustBeAssumption);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (reason.trim().length < 3) {
      setError("Record a review reason of at least three characters.");
      return;
    }
    let correctedValue: Record<string, unknown> | undefined;
    const submittedAssumption = decision === "correct" ? assumption : false;
    if (decision === "correct") {
      try {
        const parsed: unknown = JSON.parse(correctedJson);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
          throw new Error("Corrected normalized value must be a JSON object.");
        }
        correctedValue = parsed as Record<string, unknown>;
      } catch (parseError) {
        setError(errorMessage(parseError));
        return;
      }
      const changesTypedValue =
        stableIntentFingerprint(correctedValue) !==
        stableIntentFingerprint(assertion.normalized_value);
      if ((changesTypedValue || correctionMustBeAssumption) && !submittedAssumption) {
        setError("A changed normalized value must be recorded as an explicit assumption.");
        return;
      }
    }
    setBusy(true);
    setError(null);
    try {
      await reviewAssertion(assertion, {
        decision,
        reason: reason.trim(),
        correctedValue,
        assumption: submittedAssumption,
      });
    } catch (reviewError) {
      setError(errorMessage(reviewError));
      setBusy(false);
      return;
    }
    try {
      await onReviewed();
    } catch {
      setError(
        "The review decision was saved, but the workspace could not refresh. Reload before retrying.",
      );
    } finally {
      setBusy(false);
    }
  }

  if (!canReview) {
    return (
      <div className="alert warning">{unavailableMessage}</div>
    );
  }

  return (
    <form className={styles.reviewForm} onSubmit={submit}>
      {assertionHasFinalDecision ? (
        <div className="alert warning">
          A final {humanize(assertion.review_state).toLowerCase()} decision is recorded. Accept and
          reject cannot be repeated; record a correction if later evidence changes the value.
        </div>
      ) : null}
      <div className={styles.segmented} role="group" aria-label="Review decision">
        {(assertionHasFinalDecision
          ? (["correct"] as const)
          : acceptIsSupported
            ? (["accept", "correct", "reject"] as const)
            : (["correct", "reject"] as const)
        ).map((option) => (
          <button
            type="button"
            key={option}
            className={decision === option ? styles.segmentActive : ""}
            aria-pressed={decision === option}
            onClick={() => {
              setDecision(option);
              setAssumption(option === "correct" && correctionMustBeAssumption);
            }}
          >
            {humanize(option)}
          </button>
        ))}
      </div>
      {decision === "correct" ? (
        <div className={styles.correctionFields}>
          <div className="field">
            <label htmlFor={`correct-json-${assertion.id}`}>Normalized value (JSON)</label>
            <textarea
              id={`correct-json-${assertion.id}`}
              className={`${styles.jsonEditor} mono`}
              value={correctedJson}
              onChange={(event) => setCorrectedJson(event.target.value)}
              spellCheck={false}
            />
            <p className="subtle">
              Display text is derived from this typed JSON after the correction is recorded.
            </p>
          </div>
        </div>
      ) : null}
      <div className="field">
        <label htmlFor={`review-reason-${assertion.id}`}>Reason for this decision</label>
        <textarea
          id={`review-reason-${assertion.id}`}
          className="textarea"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder={
            decision === "accept"
              ? "Explain what you verified against the source…"
              : decision === "correct"
                ? "Explain why the original proposal was inaccurate…"
                : "Explain why this proposal cannot be used…"
          }
          required
        />
      </div>
      {decision === "correct" ? (
        <>
          <label className={styles.checkboxRow}>
            <input
              type="checkbox"
              checked={assumption}
              onChange={(event) => setAssumption(event.target.checked)}
              disabled={correctionMustBeAssumption}
            />
            Record this correction as an explicit assumption
          </label>
          <p className={styles.formNote}>
            {lineageRequiresAssumption
              ? "This correction descends from an explicit assumption, so that label must remain."
              : !hasActiveExactEvidence
                ? "No active exact source span supports this proposal, so a correction must remain an explicit assumption."
              : "Required when the typed JSON changes the extracted value."}
          </p>
        </>
      ) : null}
      {error ? (
        <div className="alert danger" role="alert">
          {error}
        </div>
      ) : null}
      <button type="submit" className="button" disabled={busy}>
        {busy ? "Recording decision…" : `Record ${decision}`}
      </button>
    </form>
  );
}

export function WorkbenchEvidence({
  caseId,
  caseStatus,
  caseVersion,
  documents,
  assertions,
  mutationsBlocked = false,
  onRefresh,
  onRecoveryRefresh = onRefresh,
}: WorkbenchEvidenceProps) {
  const { user } = useAuth();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [requestedDocumentId, setRequestedDocumentId] = useState<string | null>(documents[0]?.id ?? null);
  const [requestedAssertionId, setRequestedAssertionId] = useState<string | null>(
    assertions.find(
      (item) => item.is_current && ["proposed", "conflicting"].includes(item.review_state),
    )?.id ?? assertions.find((item) => item.is_current)?.id ?? null,
  );
  const [loadedCatalog, setLoadedCatalog] = useState<{
    documentKey: string;
    pagesByDocument: Record<string, DocumentPage[]>;
  } | null>(null);
  const [catalogAttempt, setCatalogAttempt] = useState(0);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [selectedQuote, setSelectedQuote] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<ReviewFilter>("all");
  const [sourceType, setSourceType] = useState<"" | "synthetic" | "public" | "redacted">("");
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [processingDocumentId, setProcessingDocumentId] = useState<string | null>(null);
  const [processError, setProcessError] = useState<string | null>(null);
  const [supersedeReason, setSupersedeReason] = useState("");
  const [superseding, setSuperseding] = useState(false);
  const [supersedeError, setSupersedeError] = useState<string | null>(null);

  const selectedDocumentId = documents.some((item) => item.id === requestedDocumentId)
    ? requestedDocumentId
    : (documents[0]?.id ?? null);
  const documentKey = documents.map((item) => item.id).sort().join("|");

  useEffect(() => {
    let active = true;
    const documentIds = documentKey ? documentKey.split("|") : [];
    if (!documentIds.length) return () => undefined;
    Promise.all(
      documentIds.map(async (documentId) => [documentId, await getDocumentPages(documentId)] as const),
    )
      .then((entries) => {
        if (active) {
          setLoadedCatalog({ documentKey, pagesByDocument: Object.fromEntries(entries) });
          setCatalogError(null);
        }
      })
      .catch((loadError: unknown) => {
        if (active) {
          setLoadedCatalog({ documentKey, pagesByDocument: {} });
          setCatalogError(errorMessage(loadError));
        }
      });
    return () => {
      active = false;
    };
  }, [catalogAttempt, documentKey]);

  const pageCatalog = loadedCatalog?.documentKey === documentKey ? loadedCatalog.pagesByDocument : {};
  const pages = selectedDocumentId ? (pageCatalog[selectedDocumentId] ?? []) : [];
  const selectedAssertionId = assertions.some((item) => item.id === requestedAssertionId)
    ? requestedAssertionId
    : (assertions.find((item) => item.is_current)?.id ?? null);

  const filtered = useMemo(
    () => filterAssertions(assertions, query, filter),
    [assertions, filter, query],
  );
  const selectedAssertion =
    filtered.find((item) => item.id === selectedAssertionId) ?? filtered[0] ?? null;
  const selectedDocument = documents.find((item) => item.id === selectedDocumentId) ?? null;
  const evidenceMutable = isEvidenceMutable(caseStatus);
  const canReview = !mutationsBlocked && canReviewEvidence(user?.role, caseStatus);
  const canUpload = !mutationsBlocked && canUploadDocuments(user?.role, caseStatus);
  const canSupersede =
    canUpload &&
    Boolean(selectedDocument && !selectedDocument.superseded) &&
    documents.some(
      (item) =>
        item.id !== selectedDocument?.id &&
        !item.superseded &&
        item.processing_status === "processed",
    );
  const selectedDocumentNeedsProcessing =
    Boolean(selectedDocument && !selectedDocument.superseded) &&
    (selectedDocument?.processing_status === "uploaded" ||
      selectedDocument?.processing_status === "failed");
  const pendingCount = assertions.filter(
    (item) => item.is_current && ["proposed", "conflicting"].includes(item.review_state),
  ).length;

  function selectCitation(assertion: WorkbenchAssertion, evidenceIndex = 0) {
    setRequestedAssertionId(assertion.id);
    const evidence = assertion.evidence[evidenceIndex];
    if (!evidence) {
      setSelectedQuote(null);
      return;
    }
    if (evidence.document_id && documents.some((item) => item.id === evidence.document_id)) {
      setRequestedDocumentId(evidence.document_id);
    }
    const catalogMatch = Object.entries(pageCatalog).find(([, documentPages]) =>
      documentPages.some((item) => item.id === evidence.page_id),
    );
    if (catalogMatch) setRequestedDocumentId(catalogMatch[0]);
    const mappedPage =
      evidence.page_number ??
      catalogMatch?.[1].find((item) => item.id === evidence.page_id)?.page_number;
    if (mappedPage) setPage(mappedPage);
    setSelectedQuote(evidence.quote);
  }

  async function handleUpload(file: File | undefined) {
    if (!file) return;
    if (!sourceType) {
      setUploadError("Choose whether this document is synthetic, public, or properly redacted.");
      return;
    }
    if (file.type !== "application/pdf" && !file.name.toLowerCase().endsWith(".pdf")) {
      setUploadError("Only PDF files are accepted.");
      return;
    }
    setUploading(true);
    setUploadError(null);
    try {
      const processed = await uploadAndProcessContract(caseId, file, sourceType);
      setRequestedDocumentId(processed.id);
      setSourceType("");
      try {
        await onRefresh();
      } catch {
        setUploadError(
          "The document processed successfully, but the workspace could not refresh. Reload to inspect it.",
        );
      }
    } catch (error) {
      const message = errorMessage(error);
      setUploadError(message);
      try {
        await onRecoveryRefresh();
      } catch {
        // Preserve the upload/process error; refresh failure must not hide the recovery cause.
      }
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  async function handleProcessDocument() {
    if (!selectedDocument || !selectedDocumentNeedsProcessing || !canUpload) return;
    setProcessingDocumentId(selectedDocument.id);
    setProcessError(null);
    try {
      await processContract(selectedDocument.id);
    } catch (error) {
      const message = errorMessage(error);
      setProcessError(message);
      try {
        await onRecoveryRefresh();
      } catch {
        // Preserve the processing error; a failed refresh must not replace it.
      }
      setProcessingDocumentId(null);
      return;
    }
    try {
      await onRefresh();
    } catch {
      setProcessError(
        "Processing completed, but the workspace could not refresh. Reload before retrying.",
      );
    } finally {
      setProcessingDocumentId(null);
    }
  }

  async function handleSupersede(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedDocument || supersedeReason.trim().length < 3) {
      setSupersedeError("Record why this source is being replaced.");
      return;
    }
    setSuperseding(true);
    setSupersedeError(null);
    try {
      await supersedeContract(selectedDocument.id, caseVersion, supersedeReason.trim());
      setSupersedeReason("");
    } catch (error) {
      setSupersedeError(errorMessage(error));
      setSuperseding(false);
      return;
    }
    try {
      await onRefresh();
    } catch {
      setSupersedeError(
        "The source was superseded, but the workspace could not refresh. Reload before retrying.",
      );
    } finally {
      setSuperseding(false);
    }
  }

  return (
    <div className={styles.evidenceWorkspace}>
      <aside className={styles.documentRail} aria-label="Case documents">
        <div className={styles.railHeader}>
          <div>
            <span className={styles.eyebrow}>Source package</span>
            <h2>Documents</h2>
          </div>
          <span className={styles.countBadge}>{documents.length}</span>
        </div>
        <div className={styles.documentList}>
          {documents.length ? (
            documents.map((document) => (
              <button
                type="button"
                key={document.id}
                aria-pressed={selectedDocumentId === document.id}
                className={`${styles.documentCard} ${
                  selectedDocumentId === document.id ? styles.documentCardSelected : ""
                }`}
                onClick={() => {
                  setRequestedDocumentId(document.id);
                  setPage(1);
                  setSelectedQuote(null);
                  setProcessError(null);
                }}
              >
                <span className={styles.fileMark} aria-hidden="true">
                  PDF
                </span>
                <span className={styles.documentMeta}>
                  <strong>{document.original_filename}</strong>
                  <small>
                    {document.page_count || "—"} pages · {(document.size_bytes / 1024).toFixed(0)} KB
                  </small>
                  <small>Source classification: {humanize(document.source_type)}</small>
                  <span className={styles.documentStates}>
                    <span data-state={document.safety_status}>{humanize(document.safety_status)}</span>
                    <span data-state={document.processing_status}>
                      {humanize(document.processing_status)}
                    </span>
                    {document.superseded ? <span data-state="superseded">Superseded</span> : null}
                  </span>
                </span>
              </button>
            ))
          ) : (
            <p className={styles.emptyRail}>No source documents have been added.</p>
          )}
        </div>
        {selectedDocumentNeedsProcessing && selectedDocument ? (
          <div className={styles.documentProcessingRecovery}>
            <strong>
              {selectedDocument.processing_status === "failed"
                ? "Processing failed"
                : "Document is waiting for processing"}
            </strong>
            <p>
              {selectedDocument.processing_error ??
                (selectedDocument.processing_status === "failed"
                  ? "The last processing attempt failed safely. Retry the persisted document; do not upload the same file again."
                  : "The PDF was stored successfully but has not been processed yet.")}
            </p>
            {processError ? (
              <div className="alert danger" role="alert">
                {processError}
              </div>
            ) : null}
            {canUpload ? (
              <button
                className="button secondary small"
                type="button"
                disabled={processingDocumentId === selectedDocument.id}
                onClick={() => void handleProcessDocument()}
              >
                {processingDocumentId === selectedDocument.id
                  ? "Processing…"
                  : selectedDocument.processing_status === "failed"
                    ? "Retry processing"
                    : "Process document"}
              </button>
            ) : (
              <p>
                {caseStatus === "archived"
                  ? "Archived cases are permanently read-only and have no recovery transition."
                  : evidenceMutable
                  ? "Your role can inspect this document but cannot process it."
                  : "This revision is locked. Reopen evidence review before processing the document."}
              </p>
            )}
          </div>
        ) : null}
        {selectedDocument &&
        !selectedDocument.superseded &&
        selectedDocument.processing_status === "needs_ocr" ? (
          <div className={styles.documentOcrNotice}>
            <strong>OCR required</strong>
            <p>
              This PDF has pages without usable text. Retrying processing will not add OCR; replace it
              with a searchable PDF produced through an approved OCR workflow.
            </p>
          </div>
        ) : null}
        {canSupersede ? (
          <form className={styles.documentRecovery} onSubmit={handleSupersede}>
            <div className="field">
              <label htmlFor="supersede-reason">Replacement reason</label>
              <textarea
                id="supersede-reason"
                className="textarea"
                value={supersedeReason}
                onChange={(event) => setSupersedeReason(event.target.value)}
                placeholder="Explain why the selected source is replaced…"
                required
              />
            </div>
            {supersedeError ? (
              <div className="alert danger" role="alert">
                {supersedeError}
              </div>
            ) : null}
            <button className="button secondary small" type="submit" disabled={superseding}>
              {superseding ? "Superseding…" : "Supersede selected source"}
            </button>
          </form>
        ) : null}
        <div className={styles.uploadBox}>
          <div className="field">
            <label htmlFor="source-type">Document source</label>
            <select
              id="source-type"
              className="select"
              value={sourceType}
              onChange={(event) => setSourceType(event.target.value as typeof sourceType)}
              disabled={!canUpload}
            >
              <option value="">Choose source type</option>
              <option value="synthetic">Synthetic</option>
              <option value="public">Public</option>
              <option value="redacted">Properly redacted</option>
            </select>
            <p className={styles.formNote}>
              This is an operator-supplied classification. “Redacted” does not certify that all
              sensitive data was removed.
            </p>
          </div>
          <input
            ref={fileInputRef}
            aria-label="Contract PDF file"
            className="sr-only"
            id="contract-upload"
            type="file"
            accept="application/pdf,.pdf"
            disabled={!canUpload}
            onChange={(event) => void handleUpload(event.target.files?.[0])}
          />
          <button
            type="button"
            className="button secondary"
            onClick={() => fileInputRef.current?.click()}
            disabled={uploading || !canUpload || !sourceType}
          >
            {uploading
              ? "Validating PDF…"
              : canUpload
                ? "Upload contract"
                : evidenceMutable
                  ? "Upload unavailable for this role"
                  : "Evidence locked at this workflow state"}
          </button>
          <p>PDF only. Safety validation runs before extraction; rejected files remain rejected.</p>
          {uploadError ? (
            <div className="alert danger" role="alert">
              {uploadError}
            </div>
          ) : null}
        </div>
      </aside>

      <WorkbenchPdfViewer
        documentId={selectedDocumentId}
        documentName={selectedDocument?.original_filename ?? null}
        requestedPage={page}
        selectedQuote={selectedQuote}
        onPageChange={setPage}
      />

      <aside className={styles.assertionRail} aria-label="Extracted assertions">
        <div className={styles.assertionHeader}>
          <div>
            <span className={styles.eyebrow}>Human review queue</span>
            <h2>Contract facts</h2>
          </div>
          <span className={pendingCount ? styles.pendingBadge : styles.completeBadge}>
            {pendingCount ? `${pendingCount} pending` : "Queue clear"}
          </span>
        </div>
        {catalogError ? (
          <div className="alert danger" role="alert">
            <strong>Page catalog unavailable.</strong> {catalogError}{" "}
            <button
              type="button"
              className="button secondary small"
              onClick={() => {
                setCatalogError(null);
                setCatalogAttempt((attempt) => attempt + 1);
              }}
            >
              Retry page catalog
            </button>
          </div>
        ) : null}
        {pendingCount === 1 ? (
          <div className={styles.demoCheckpoint} role="status">
            <strong>One evidence decision left</strong>
            <span>
              Compare the selected proposal with its exact quote, then record Accept, Correct, or
              Reject. That single decision unlocks the readiness checkpoint.
            </span>
          </div>
        ) : pendingCount === 0 ? (
          <div className={styles.demoCheckpoint} data-complete="true" role="status">
            <strong>Evidence review complete</strong>
            <span>Open Readiness &amp; packet to inspect the next human workflow gate.</span>
          </div>
        ) : null}
        <div className={styles.assertionFilters}>
          <label>
            <span className="sr-only">Search contract facts</span>
            <input
              className="input"
              type="search"
              placeholder="Search facts or values"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          <label>
            <span className="sr-only">Filter review status</span>
            <select
              className="select"
              value={filter}
              onChange={(event) => setFilter(event.target.value as ReviewFilter)}
            >
              <option value="all">All review states</option>
              <option value="proposed">Proposed</option>
              <option value="conflicting">Conflicting</option>
              <option value="accepted">Accepted</option>
              <option value="corrected">Corrected</option>
              <option value="rejected">Rejected</option>
            </select>
          </label>
        </div>
        <div className={styles.assertionList}>
          {filtered.map((assertion) => (
            <button
              type="button"
              key={assertion.id}
              aria-pressed={selectedAssertion?.id === assertion.id}
              className={`${styles.assertionCard} ${
                selectedAssertion?.id === assertion.id ? styles.assertionCardSelected : ""
              } ${
                effectiveEvidenceBasis(assertion, documents) === "explicit_assumption"
                  ? styles.assertionCardAssumption
                  : ""
              }`}
              data-evidence-basis={effectiveEvidenceBasis(assertion, documents)}
              onClick={() => selectCitation(assertion)}
            >
              <span className={styles.assertionCardTop}>
                <strong>{humanize(assertion.semantic_key)}</strong>
                <span className={styles.assertionBadges}>
                  <span
                    className={`status-pill ${evidenceBasisTone(effectiveEvidenceBasis(assertion, documents))}`}
                  >
                    {evidenceBasisLabel(effectiveEvidenceBasis(assertion, documents))}
                  </span>
                  <ReviewStatusPill status={assertion.review_state} />
                </span>
              </span>
              <span className={styles.assertionValue}>{assertion.display_value || "No display value"}</span>
              <span className={styles.assertionProvenance}>
                {assertionProvenanceSummary(assertion, documents)}
              </span>
            </button>
          ))}
          {!filtered.length ? (
            <div className={styles.noResults}>No current assertions match these filters.</div>
          ) : null}
        </div>
        {selectedAssertion ? (
          <div className={styles.assertionDetail}>
            <div className={styles.detailHeading}>
              <div>
                <span className={styles.eyebrow}>Selected fact</span>
                <h3>{humanize(selectedAssertion.semantic_key)}</h3>
              </div>
              <ReviewStatusPill status={selectedAssertion.review_state} />
            </div>
            <EvidenceBasisNotice assertion={selectedAssertion} documents={documents} />
            <dl className={styles.factGrid}>
              <div>
                <dt>Current value</dt>
                <dd>{selectedAssertion.display_value}</dd>
              </div>
              <div>
                <dt>Original extractor</dt>
                <dd>{humanize(selectedAssertion.source)}</dd>
              </div>
              <div>
                <dt>Extraction confidence</dt>
                <dd>{confidenceLabel(selectedAssertion.confidence)}</dd>
              </div>
              <div>
                <dt>Version</dt>
                <dd>{selectedAssertion.version}</dd>
              </div>
            </dl>
            <div className={styles.citationStack}>
              <span className="field-label">
                {citationHeading(effectiveEvidenceBasis(selectedAssertion, documents))}
              </span>
              {selectedAssertion.evidence.length ? (
                selectedAssertion.evidence.map((evidence, index) => {
                  const pageNumber =
                    evidence.page_number ??
                    pages.find((item) => item.id === evidence.page_id)?.page_number;
                  return (
                    <button
                      type="button"
                      key={evidence.id}
                      className={styles.citationCard}
                      onClick={() => selectCitation(selectedAssertion, index)}
                    >
                      <span>
                        Page {pageNumber ?? "unknown"} · chars {evidence.char_start}–{evidence.char_end}
                      </span>
                      <q>{evidence.quote}</q>
                      <small className="mono">quote sha256 {evidence.quote_sha256.slice(0, 14)}…</small>
                    </button>
                  );
                })
              ) : (
                <div
                  className={`alert ${
                    effectiveEvidenceBasis(selectedAssertion, documents) === "explicit_assumption"
                      ? "warning"
                      : "danger"
                  }`}
                >
                  {missingEvidenceMessage(effectiveEvidenceBasis(selectedAssertion, documents))}
                </div>
              )}
            </div>
            <details className={styles.normalizedValue}>
              <summary>Inspect normalized value</summary>
              <pre>{JSON.stringify(selectedAssertion.normalized_value, null, 2)}</pre>
            </details>
            <AssertionReviewForm
              key={`${selectedAssertion.id}:${selectedAssertion.version}`}
              assertion={selectedAssertion}
              documents={documents}
              canReview={canReview}
              unavailableMessage={
                caseStatus === "archived"
                  ? "Archived cases are permanently read-only and have no further transitions."
                  : evidenceMutable
                  ? "Your role can inspect evidence but cannot record review decisions."
                  : "This revision is locked at the current workflow state. Reopen evidence review before recording a new decision."
              }
              onReviewed={onRefresh}
            />
          </div>
        ) : null}
      </aside>
    </div>
  );
}
