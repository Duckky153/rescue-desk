"use client";

import { useMemo, useState, type FormEvent } from "react";
import { useAuth } from "@/components/auth-provider";
import {
  createExport,
  downloadExport,
  resolveFinding,
  transitionCase,
  type ExportArtifact,
  type ExportKind,
} from "@/components/workbench-api";
import {
  errorMessage,
  formatDateTime,
  humanize,
  saveBlob,
} from "@/components/workbench-utils";
import { CaseStatusPill } from "@/components/status-pill";
import { DEMO_COMPLETED_CASE_NAME } from "@/lib/demo";
import type {
  Calculation,
  CaseDetail,
  CaseStatus,
  DocumentSummary,
  Readiness,
} from "@/types/api";
import styles from "./workbench.module.css";

const EXPORTS: Array<{
  kind: ExportKind;
  title: string;
  description: string;
  extension: string;
}> = [
  {
    kind: "internal_review_pdf",
    title: "Internal review packet",
    description: "Reviewed facts, calculations, blockers, formulas, citations, and hashes.",
    extension: "pdf",
  },
  {
    kind: "customer_explanation_pdf",
    title: "Switching evidence brief",
    description: "Customer-readable evidence and scenario explanation without an eligibility claim.",
    extension: "pdf",
  },
  {
    kind: "evidence_csv",
    title: "Evidence ledger",
    description: "Formula-safe source spans and review metadata for independent inspection.",
    extension: "csv",
  },
  {
    kind: "machine_readable_json",
    title: "Canonical snapshot",
    description: "Machine-readable packet used to reproduce and verify the export snapshot.",
    extension: "json",
  },
];

interface TransitionAction {
  target: CaseStatus;
  label: string;
  explanation: string;
}

function transitionAction(status: CaseStatus): TransitionAction | null {
  switch (status) {
    case "draft":
      return {
        target: "evidence_review",
        label: "Begin evidence review",
        explanation: "Requires a processed contract document.",
      };
    case "evidence_review":
      return {
        target: "ready_for_internal_review",
        label: "Send to internal review",
        explanation: "Requires reviewed mandatory facts, a reproducible calculation, and no blockers.",
      };
    case "ready_for_internal_review":
      return {
        target: "internal_packet_approved",
        label: "Approve internal packet",
        explanation: "Only an approver or administrator can cross this human gate.",
      };
    case "internal_packet_approved":
      return {
        target: "exported",
        label: "Mark packet exported",
        explanation: "Requires at least one current approver-recorded export artifact.",
      };
    case "exported":
      return {
        target: "evidence_review",
        label: "Reopen evidence review",
        explanation: "Reopening makes the existing approved snapshot historical.",
      };
    default:
      return null;
  }
}

export function WorkbenchReadiness({
  caseDetail,
  readiness,
  calculation,
  exports,
  documents = [],
  hasExplicitAssumptions = false,
  mutationsBlocked = false,
  onRefresh,
}: {
  caseDetail: CaseDetail;
  readiness: Readiness;
  calculation: Calculation | null;
  exports: ExportArtifact[];
  documents?: DocumentSummary[];
  hasExplicitAssumptions?: boolean;
  mutationsBlocked?: boolean;
  onRefresh: () => Promise<void>;
}) {
  const { user } = useAuth();
  const [reason, setReason] = useState("");
  const [transitioning, setTransitioning] = useState(false);
  const [transitionError, setTransitionError] = useState<string | null>(null);
  const [exporting, setExporting] = useState<ExportKind | null>(null);
  const [downloading, setDownloading] = useState<string | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const [selectedFindingId, setSelectedFindingId] = useState<string | null>(null);
  const [findingReason, setFindingReason] = useState("");
  const [findingDisposition, setFindingDisposition] = useState<"resolved" | "accepted_risk">(
    "resolved",
  );
  const [resolvingFinding, setResolvingFinding] = useState(false);
  const [findingError, setFindingError] = useState<string | null>(null);
  const [returnReason, setReturnReason] = useState("");
  const [returning, setReturning] = useState(false);
  const [returnError, setReturnError] = useState<string | null>(null);
  const action = transitionAction(caseDetail.status);
  const calculationStale = readiness.findings.some(
    (finding) => finding.code === "calculation.stale" && finding.status === "open",
  );
  const isApprover = Boolean(
    !mutationsBlocked && user && ["approver", "admin"].includes(user.role),
  );
  const canManage = Boolean(
    !mutationsBlocked && user && ["analyst", "approver", "admin"].includes(user.role),
  );
  const canApprovePacket = Boolean(
    isApprover &&
      (user?.role === "admin" ||
        caseDetail.assigned_approver_id === null ||
        caseDetail.assigned_approver_id === user?.id),
  );
  const hasProcessedDocument = documents.some(
    (document) => !document.superseded && document.processing_status === "processed",
  );
  const canReturnToEvidence = [
    "ready_for_internal_review",
    "internal_packet_approved",
  ].includes(caseDetail.status);
  const currentExports = useMemo(
    () => exports.filter((artifact) => !artifact.superseded),
    [exports],
  );
  const historicalExports = useMemo(
    () => exports.filter((artifact) => artifact.superseded),
    [exports],
  );
  const hasApprovedExport = currentExports.some(
    (artifact) => artifact.approval_status === "approved",
  );
  const isSeededCompletedExample = caseDetail.display_name === DEMO_COMPLETED_CASE_NAME;
  const exportCalculationReady = Boolean(
    calculation && readiness.reproducible_calculation_exists && !calculationStale,
  );
  const transitionBlocked =
    !action ||
    !canManage ||
    (caseDetail.status === "draft" &&
      action.target === "evidence_review" &&
      !hasProcessedDocument) ||
    (action.target === "ready_for_internal_review" && !readiness.ready_for_internal_review) ||
    (action.target === "internal_packet_approved" && !canApprovePacket) ||
    (action.target === "exported" && !hasApprovedExport);

  async function submitTransition(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!action) return;
    if (reason.trim().length < 3) {
      setTransitionError("Record a reason of at least three characters.");
      return;
    }
    setTransitioning(true);
    setTransitionError(null);
    try {
      await transitionCase(caseDetail.id, action.target, caseDetail.version, reason.trim());
      setReason("");
    } catch (error) {
      setTransitionError(errorMessage(error));
      setTransitioning(false);
      return;
    }
    try {
      await onRefresh();
    } catch {
      setTransitionError(
        "The workflow decision was saved, but the workspace could not refresh. Reload before retrying.",
      );
    } finally {
      setTransitioning(false);
    }
  }

  async function generate(kind: ExportKind) {
    setExporting(kind);
    setExportError(null);
    try {
      await createExport(caseDetail.id, kind, calculation?.id ?? null);
    } catch (error) {
      setExportError(errorMessage(error));
      setExporting(null);
      return;
    }
    try {
      await onRefresh();
    } catch {
      setExportError(
        "The export was generated, but the workspace could not refresh. Reload before generating again.",
      );
    } finally {
      setExporting(null);
    }
  }

  async function download(artifact: ExportArtifact) {
    setDownloading(artifact.id);
    setExportError(null);
    try {
      const blob = await downloadExport(caseDetail.id, artifact.id);
      saveBlob(blob, artifact.filename);
    } catch (error) {
      setExportError(errorMessage(error));
    } finally {
      setDownloading(null);
    }
  }

  async function submitFindingDisposition(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const finding = readiness.findings.find((item) => item.id === selectedFindingId);
    if (!finding || findingReason.trim().length < 3) {
      setFindingError("Record a reason of at least three characters.");
      return;
    }
    setResolvingFinding(true);
    setFindingError(null);
    try {
      await resolveFinding(
        caseDetail.id,
        finding,
        findingDisposition,
        findingReason.trim(),
      );
      setSelectedFindingId(null);
      setFindingReason("");
      setFindingDisposition("resolved");
    } catch (error) {
      setFindingError(errorMessage(error));
      setResolvingFinding(false);
      return;
    }
    try {
      await onRefresh();
    } catch {
      setFindingError(
        "The finding disposition was saved, but the workspace could not refresh. Reload before retrying.",
      );
    } finally {
      setResolvingFinding(false);
    }
  }

  async function submitReturnToEvidence(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (returnReason.trim().length < 3) {
      setReturnError("Record why the packet must return to evidence review.");
      return;
    }
    setReturning(true);
    setReturnError(null);
    try {
      await transitionCase(
        caseDetail.id,
        "evidence_review",
        caseDetail.version,
        returnReason.trim(),
      );
      setReturnReason("");
    } catch (error) {
      setReturnError(errorMessage(error));
      setReturning(false);
      return;
    }
    try {
      await onRefresh();
    } catch {
      setReturnError(
        "The case returned to evidence review, but the workspace could not refresh. Reload before retrying.",
      );
    } finally {
      setReturning(false);
    }
  }

  return (
    <div className={styles.readinessLayout}>
      <section className={`${styles.readinessOverview} panel`}>
        <div className={styles.sectionHeading}>
          <div>
            <span className={styles.eyebrow}>Preflight controls</span>
            <h2>Internal review readiness</h2>
          </div>
          <span
            className={`status-pill ${readiness.ready_for_internal_review ? "success" : "danger"}`}
          >
            {readiness.ready_for_internal_review
              ? "Ready"
              : `${readiness.open_blocking_findings} ${
                  readiness.open_blocking_findings === 1 ? "blocker" : "blockers"
                }`}
          </span>
        </div>
        <div className={styles.controlCards}>
          <article data-complete={readiness.mandatory_assertions_reviewed}>
            <span aria-hidden="true">{readiness.mandatory_assertions_reviewed ? "✓" : "!"}</span>
            <div>
              <strong>Mandatory facts</strong>
              <small>
                {readiness.mandatory_assertions_reviewed
                  ? hasExplicitAssumptions
                    ? "Reviewed; explicit assumptions remain labelled"
                    : "Reviewed against source evidence"
                  : "Review decisions remain"}
              </small>
            </div>
          </article>
          <article data-complete={readiness.reproducible_calculation_exists}>
            <span aria-hidden="true">{readiness.reproducible_calculation_exists ? "✓" : "!"}</span>
            <div>
              <strong>Calculation</strong>
              <small>
                {readiness.reproducible_calculation_exists
                  ? "Versioned result is available"
                  : calculationStale
                    ? "Latest result is stale · rerun required"
                    : "No reproducible result"}
              </small>
            </div>
          </article>
          <article data-complete={readiness.open_blocking_findings === 0}>
            <span aria-hidden="true">{readiness.open_blocking_findings === 0 ? "✓" : "!"}</span>
            <div>
              <strong>Open blockers</strong>
              <small>
                {readiness.open_blocking_findings === 0
                  ? "No blocking findings"
                  : readiness.open_blocking_findings === 1
                    ? "1 must be resolved"
                    : `${readiness.open_blocking_findings} must be resolved`}
              </small>
            </div>
          </article>
        </div>
        <div className={styles.findingsList}>
          <div className={styles.listTitle}>
            <h3>Findings</h3>
            <span>{readiness.findings.length} total</span>
          </div>
          {readiness.findings.map((finding) => (
            <article key={finding.id} data-severity={finding.severity}>
              <span className={styles.findingIcon} aria-hidden="true">
                {finding.severity === "blocking" ? "!" : finding.severity === "warning" ? "△" : "i"}
              </span>
              <div>
                <span className={styles.findingTopline}>
                  <strong>{finding.title}</strong>
                  <span>
                    {humanize(finding.severity)} · {humanize(finding.status)}
                  </span>
                </span>
                <p>{finding.detail}</p>
                <small className="mono">{finding.code}</small>
                {finding.resolution_reason ? (
                  <p className={styles.findingResolution}>
                    Disposition reason: {finding.resolution_reason}
                  </p>
                ) : null}
                {isApprover &&
                finding.status === "open" &&
                !finding.id.startsWith("computed:") &&
                ["draft", "evidence_review"].includes(caseDetail.status) ? (
                  <button
                    type="button"
                    className="button secondary small"
                    onClick={() => {
                      setSelectedFindingId(finding.id);
                      setFindingError(null);
                      setFindingDisposition("resolved");
                      setFindingReason("");
                    }}
                  >
                    Record disposition
                  </button>
                ) : null}
              </div>
            </article>
          ))}
          {selectedFindingId ? (
            <form className={styles.findingDisposition} onSubmit={submitFindingDisposition}>
              <div className="field">
                <label htmlFor="finding-disposition">Disposition</label>
                <select
                  id="finding-disposition"
                  className="select"
                  value={findingDisposition}
                  onChange={(event) =>
                    setFindingDisposition(event.target.value as typeof findingDisposition)
                  }
                >
                  <option value="resolved">Resolved</option>
                  <option value="accepted_risk">Accepted risk</option>
                </select>
              </div>
              <div className="field">
                <label htmlFor="finding-reason">Disposition reason</label>
                <textarea
                  id="finding-reason"
                  className="textarea"
                  value={findingReason}
                  onChange={(event) => setFindingReason(event.target.value)}
                  required
                />
              </div>
              {findingError ? (
                <div className="alert danger" role="alert">
                  {findingError}
                </div>
              ) : null}
              <div className={styles.findingActions}>
                <button className="button small" type="submit" disabled={resolvingFinding}>
                  {resolvingFinding ? "Recording…" : "Record disposition"}
                </button>
                <button
                  className="button secondary small"
                  type="button"
                  onClick={() => {
                    setSelectedFindingId(null);
                    setFindingDisposition("resolved");
                    setFindingReason("");
                    setFindingError(null);
                  }}
                >
                  Cancel
                </button>
              </div>
            </form>
          ) : null}
          {!readiness.findings.length ? (
            <div className={styles.emptyFindings}>No findings are recorded for this revision.</div>
          ) : null}
        </div>
        <p className={styles.disclaimerText}>{readiness.disclaimer}</p>
      </section>

      <aside className={styles.workflowColumn} aria-label="Workflow and revision controls">
        <section className={`${styles.workflowAction} panel`}>
          <div className={styles.sectionHeading}>
            <div>
              <span className={styles.eyebrow}>Approver gate</span>
              <h2>Workflow decision</h2>
            </div>
            <CaseStatusPill status={caseDetail.status} />
          </div>
          {caseDetail.status === "archived" ? (
            <div className="alert warning">This case is archived and has no further transitions.</div>
          ) : action ? (
            <form onSubmit={submitTransition} className={styles.transitionForm}>
              <div className={styles.nextState}>
                <span>Next controlled state</span>
                <strong>{humanize(action.target)}</strong>
                <small>{action.explanation}</small>
              </div>
              <div className="field">
                <label htmlFor="transition-reason">Decision reason</label>
                <textarea
                  id="transition-reason"
                  className="textarea"
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                  placeholder="Record why this state change is appropriate…"
                  required
                />
              </div>
              {action.target === "internal_packet_approved" && !canApprovePacket ? (
                <div className="alert warning">
                  {!isApprover
                    ? `Your ${user?.role} role cannot approve. An approver must sign in and record the decision.`
                    : "This matter is assigned to another approver. Only that approver or an administrator can approve it."}
                </div>
              ) : null}
              {!canManage ? (
                <div className="alert warning">
                  Your role can inspect the workflow but cannot record state changes.
                </div>
              ) : null}
              {transitionError ? (
                <div className="alert danger" role="alert">
                  {transitionError}
                </div>
              ) : null}
              <button
                type="submit"
                className="button"
                disabled={transitioning || transitionBlocked}
              >
                {transitioning ? "Recording transition…" : action.label}
              </button>
            </form>
          ) : null}
          {canReturnToEvidence ? (
            <form onSubmit={submitReturnToEvidence} className={styles.transitionForm}>
              <div className={styles.nextState}>
                <span>Correction path</span>
                <strong>Return to evidence review</strong>
                <small>
                  Use this when a fact, obligation, or calculation needs correction before export.
                </small>
              </div>
              <div className="field">
                <label htmlFor="return-reason">Return reason</label>
                <textarea
                  id="return-reason"
                  className="textarea"
                  value={returnReason}
                  onChange={(event) => setReturnReason(event.target.value)}
                  placeholder="Record what must be corrected…"
                  required
                />
              </div>
              {returnError ? (
                <div className="alert danger" role="alert">
                  {returnError}
                </div>
              ) : null}
              <button
                type="submit"
                className="button secondary"
                disabled={returning || !canManage}
              >
                {returning ? "Returning…" : "Return to evidence review"}
              </button>
            </form>
          ) : null}
          <div className={styles.revisionHistory}>
            <span className="field-label">Revision history</span>
            {caseDetail.revisions.map((revision) => (
              <div key={revision.id}>
                <span>Revision {revision.number}</span>
                <small>{formatDateTime(revision.created_at)}</small>
                <strong className="mono">
                  {revision.snapshot_hash ? `${revision.snapshot_hash.slice(0, 12)}…` : "Not frozen"}
                </strong>
              </div>
            ))}
          </div>
        </section>
      </aside>

      <section className={`${styles.exportPanel} panel`}>
        <div className={styles.sectionHeading}>
          <div>
            <span className={styles.eyebrow}>One immutable snapshot</span>
            <h2>Review packet</h2>
          </div>
          <span className={styles.countBadge}>
            {currentExports.length} current · {historicalExports.length} historical
          </span>
        </div>
        <p className={styles.sectionIntro}>
          All four formats must resolve to the same reviewed facts, citations, formulas, and snapshot
          hash. Generating a packet does not send it anywhere.
        </p>
        {caseDetail.status !== "internal_packet_approved" && caseDetail.status !== "exported" ? (
          <div className="alert warning">
            Approved packet formats remain locked until an approver records internal approval. Any
            pre-approval internal packet is explicitly marked not approved.
          </div>
        ) : null}
        {["internal_packet_approved", "exported"].includes(caseDetail.status) &&
        !exportCalculationReady ? (
          <div className="alert warning">
            A current reproducible calculation is required. Reopen evidence review and rerun the
            calculation before generating a replacement packet.
          </div>
        ) : null}
        <div className={styles.exportGrid}>
          {EXPORTS.map((config) => {
            const artifact = currentExports.find((item) => item.kind === config.kind);
            const historicalCount = historicalExports.filter(
              (item) => item.kind === config.kind,
            ).length;
            const needsApprovedReplacement = Boolean(
              artifact &&
                artifact.approval_status !== "approved" &&
                ["internal_packet_approved", "exported"].includes(caseDetail.status),
            );
            return (
              <article key={config.kind}>
                <div className={styles.exportIcon} aria-hidden="true">
                  {config.extension.toUpperCase()}
                </div>
                <div>
                  <strong>{config.title}</strong>
                  <p>{config.description}</p>
                  {artifact ? (
                    <>
                      <span
                        className={styles.artifactApproval}
                        data-approved={artifact.approval_status === "approved"}
                      >
                        {artifact.approval_status === "approved"
                          ? isSeededCompletedExample
                            ? "Seeded synthetic approver-role snapshot"
                            : "Approver-recorded snapshot"
                          : "Not approved"}
                      </span>
                      <span className={styles.artifactMeta}>
                        <span>{formatDateTime(artifact.created_at)}</span>
                        <span className="mono">file {artifact.sha256.slice(0, 12)}…</span>
                        <span className="mono">
                          packet {artifact.packet_snapshot_sha256.slice(0, 12)}…
                        </span>
                      </span>
                    </>
                  ) : historicalCount ? (
                    <span className={styles.artifactMissing}>
                      {historicalCount} prior artifact{historicalCount === 1 ? " is" : "s are"}{" "}
                      retired and cannot represent the current reviewed inputs.
                    </span>
                  ) : (
                    <span className={styles.artifactMissing}>No current artifact generated</span>
                  )}
                </div>
                <div className={styles.exportActions}>
                  {artifact ? (
                    <button
                      type="button"
                      className="button secondary small"
                      onClick={() => void download(artifact)}
                      disabled={downloading === artifact.id}
                    >
                      {downloading === artifact.id ? "Downloading…" : "Download"}
                    </button>
                  ) : null}
                  {!artifact || needsApprovedReplacement ? (
                    <button
                      type="button"
                      className="button secondary small"
                      onClick={() => void generate(config.kind)}
                      disabled={
                        exporting !== null ||
                        !canManage ||
                        !exportCalculationReady ||
                        (caseDetail.status !== "internal_packet_approved" &&
                          caseDetail.status !== "exported")
                      }
                    >
                      {exporting === config.kind
                        ? "Generating…"
                        : needsApprovedReplacement
                          ? "Regenerate approved"
                          : "Generate"}
                    </button>
                  ) : null}
                </div>
              </article>
            );
          })}
        </div>
        {exportError ? (
          <div className="alert danger" role="alert">
            {exportError}
          </div>
        ) : null}
      </section>
    </div>
  );
}
