"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { GuidedDemo } from "@/components/guided-demo";
import { WorkbenchAudit } from "@/components/workbench-audit";
import { getWorkbenchSnapshot, type WorkbenchSnapshot, type WorkbenchTab } from "@/components/workbench-api";
import { WorkbenchEconomics } from "@/components/workbench-economics";
import { WorkbenchEvidence } from "@/components/workbench-evidence";
import { WorkbenchReadiness } from "@/components/workbench-readiness";
import { errorMessage, formatDateTime, workflowPosition, WORKFLOW } from "@/components/workbench-utils";
import { CaseStatusPill } from "@/components/status-pill";
import { DEMO_COMPLETED_CASE_NAME } from "@/lib/demo";
import styles from "./workbench.module.css";

const TABS: Array<{ id: WorkbenchTab; label: string; description: string }> = [
  { id: "evidence", label: "Evidence", description: "Documents and human fact review" },
  { id: "economics", label: "Economics", description: "Obligations and deterministic calculation" },
  { id: "readiness", label: "Readiness & packet", description: "Blockers, approval, and exports" },
  { id: "audit", label: "Audit", description: "Recorded event and hash history" },
];

export function Workbench({ caseId }: { caseId: string }) {
  const [snapshot, setSnapshot] = useState<WorkbenchSnapshot | null>(null);
  const [activeTab, setActiveTab] = useState<WorkbenchTab>("evidence");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [snapshotIsStale, setSnapshotIsStale] = useState(false);
  const refreshGeneration = useRef(0);

  const refresh = useCallback(async (context: "manual" | "mutation" | "recovery" = "manual") => {
    const generation = refreshGeneration.current + 1;
    refreshGeneration.current = generation;
    setRefreshing(true);
    setError(null);
    try {
      const result = await getWorkbenchSnapshot(caseId);
      if (generation !== refreshGeneration.current) return;
      setSnapshot(result);
      setSnapshotIsStale(false);
    } catch (requestError) {
      if (generation !== refreshGeneration.current) return;
      const message = errorMessage(requestError);
      setSnapshotIsStale(true);
      setError(
        context === "mutation"
          ? `The change was saved, but the workspace could not refresh (${message}). Reload before retrying; editing is locked to prevent a duplicate action.`
          : context === "recovery"
            ? `The recovery refresh could not load a fresh workspace (${message}). The result of the attempted action is not assumed; reload before retrying.`
          : `The workspace could not refresh (${message}). Editing is locked until a fresh snapshot loads.`,
      );
      throw requestError;
    } finally {
      if (generation === refreshGeneration.current) {
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, [caseId]);

  const refreshAfterMutation = useCallback(() => refresh("mutation"), [refresh]);
  const refreshAfterRecovery = useCallback(() => refresh("recovery"), [refresh]);

  useEffect(() => {
    let active = true;
    getWorkbenchSnapshot(caseId)
      .then((result) => {
        if (active) setSnapshot(result);
      })
      .catch((requestError: unknown) => {
        if (active) setError(errorMessage(requestError));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [caseId]);

  const counts = useMemo(() => {
    if (!snapshot) return { pending: 0, reviewed: 0 };
    return snapshot.assertions.reduce(
      (total, item) => {
        if (!item.is_current) return total;
        if (["accepted", "corrected", "rejected"].includes(item.review_state)) total.reviewed += 1;
        else total.pending += 1;
        return total;
      },
      { pending: 0, reviewed: 0 },
    );
  }, [snapshot]);

  if (loading && !snapshot) {
    return (
      <div className={styles.workbenchLoading} aria-live="polite">
        <div className="spinner" aria-label="Loading case workbench" />
        <strong>Loading evidence workbench…</strong>
      </div>
    );
  }

  if (!snapshot) {
    return (
      <div className={styles.workbenchFailure}>
        <div className="alert danger" role="alert">
          <strong>Case workbench could not be loaded.</strong> {error}
        </div>
        <button
          type="button"
          className="button"
          onClick={() => void refresh().catch(() => undefined)}
        >
          Try again
        </button>
        <Link href="/" className="button secondary">
          Back to matters
        </Link>
      </div>
    );
  }

  const position = workflowPosition(snapshot.caseDetail.status);
  const calculationStale = Boolean(
    snapshot.calculation &&
      snapshot.readiness.findings.some(
        (finding) => finding.code === "calculation.stale" && finding.status === "open",
      ),
  );

  return (
    <div className={styles.workbench}>
      <header className={styles.caseHeader}>
        <div className={styles.breadcrumbRow}>
          <Link href="/" className={styles.backLink}>
            <span aria-hidden="true">←</span> Matters
          </Link>
          <span>Updated {formatDateTime(snapshot.caseDetail.updated_at)}</span>
          <button
            type="button"
            className="button secondary small"
            onClick={() => void refresh().catch(() => undefined)}
            disabled={refreshing}
          >
            {refreshing ? "Refreshing…" : "Refresh"}
          </button>
        </div>
        <div className={styles.caseIdentity}>
          <div>
            <span className={styles.eyebrow}>ERP contract exit review</span>
            <h1>{snapshot.caseDetail.display_name}</h1>
            <p>
              {snapshot.caseDetail.applicant_company} <span aria-hidden="true">·</span>{" "}
              {snapshot.caseDetail.erp_provider} <span aria-hidden="true">·</span> Revision{" "}
              {snapshot.caseDetail.current_revision_number}
            </p>
          </div>
          <CaseStatusPill status={snapshot.caseDetail.status} />
        </div>
        <div className={styles.workflowTrack} aria-label="Case workflow" tabIndex={0}>
          {WORKFLOW.map((step, index) => (
            <div
              key={step.status}
              className={
                index < position
                  ? styles.workflowComplete
                  : index === position
                    ? styles.workflowCurrent
                    : ""
              }
              aria-current={index === position ? "step" : undefined}
            >
              <span>{index < position ? "✓" : index + 1}</span>
              <strong>{step.label}</strong>
            </div>
          ))}
        </div>
        <div className={styles.caseMetrics}>
          <article>
            <span>Source package</span>
            <strong>{snapshot.documents.length}</strong>
            <small>document{snapshot.documents.length === 1 ? "" : "s"}</small>
          </article>
          <article>
            <span>Evidence decisions</span>
            <strong>{counts.pending}</strong>
            <small>{counts.reviewed} decided</small>
          </article>
          <article data-alert={snapshot.readiness.open_blocking_findings > 0}>
            <span>Open blockers</span>
            <strong>{snapshot.readiness.open_blocking_findings}</strong>
            <small>{snapshot.readiness.ready_for_internal_review ? "Ready" : "Not ready"}</small>
          </article>
          <article>
            <span>Latest analysis</span>
            <strong>
              {snapshot.readiness.reproducible_calculation_exists
                ? "Ready"
                : calculationStale
                  ? "Stale"
                  : "—"}
            </strong>
            <small>
              {calculationStale
                ? "Rerun required"
                : snapshot.calculation
                  ? `engine ${snapshot.calculation.engine_version}`
                  : "Not run"}
            </small>
          </article>
        </div>
      </header>

      <GuidedDemo
        status={snapshot.caseDetail.status}
        blockerCount={snapshot.readiness.open_blocking_findings}
        isSeededCompletedExample={
          snapshot.caseDetail.display_name === DEMO_COMPLETED_CASE_NAME
        }
        activeTab={activeTab}
        onSelectTab={setActiveTab}
      />

      {error ? (
        <div className={`${styles.inlineError} alert danger`} role="alert">
          <span>{error}</span>
          {snapshotIsStale ? (
            <button
              type="button"
              className="button danger small"
              onClick={() => void refresh().catch(() => undefined)}
              disabled={refreshing}
            >
              {refreshing ? "Reloading…" : "Reload fresh snapshot"}
            </button>
          ) : (
            <button type="button" className="button danger small" onClick={() => setError(null)}>
              Dismiss
            </button>
          )}
        </div>
      ) : null}

      <nav className={styles.workbenchTabs} aria-label="Case workbench sections">
        {TABS.map((tab) => (
          <button
            type="button"
            key={tab.id}
            className={activeTab === tab.id ? styles.activeTab : ""}
            aria-current={activeTab === tab.id ? "page" : undefined}
            onClick={() => setActiveTab(tab.id)}
          >
            <strong>{tab.label}</strong>
            <span>{tab.description}</span>
            {tab.id === "evidence" && counts.pending ? (
              <small className={styles.tabCount}>{counts.pending}</small>
            ) : null}
            {tab.id === "readiness" && snapshot.readiness.open_blocking_findings ? (
              <small className={styles.tabCount}>{snapshot.readiness.open_blocking_findings}</small>
            ) : null}
          </button>
        ))}
      </nav>

      <div className={styles.workbenchContent}>
        {activeTab === "evidence" ? (
          <WorkbenchEvidence
            caseId={caseId}
            caseStatus={snapshot.caseDetail.status}
            caseVersion={snapshot.caseDetail.version}
            documents={snapshot.documents}
            assertions={snapshot.assertions}
            mutationsBlocked={snapshotIsStale}
            onRefresh={refreshAfterMutation}
            onRecoveryRefresh={refreshAfterRecovery}
          />
        ) : null}
        {activeTab === "economics" ? (
          <WorkbenchEconomics
            caseId={caseId}
            caseStatus={snapshot.caseDetail.status}
            caseVersion={snapshot.caseDetail.version}
            fees={snapshot.fees}
            assertions={snapshot.assertions}
            documents={snapshot.documents}
            calculation={snapshot.calculation}
            calculationIsCurrent={snapshot.readiness.reproducible_calculation_exists}
            mutationsBlocked={snapshotIsStale}
            onRefresh={refreshAfterMutation}
          />
        ) : null}
        {activeTab === "readiness" ? (
          <WorkbenchReadiness
            caseDetail={snapshot.caseDetail}
            readiness={snapshot.readiness}
            calculation={snapshot.calculation}
            exports={snapshot.exports}
            documents={snapshot.documents}
            hasExplicitAssumptions={snapshot.assertions.some(
              (assertion) =>
                assertion.is_current &&
                ["accepted", "corrected"].includes(assertion.review_state) &&
                (assertion.assumption || assertion.evidence_basis === "explicit_assumption"),
            )}
            mutationsBlocked={snapshotIsStale}
            onRefresh={refreshAfterMutation}
          />
        ) : null}
        {activeTab === "audit" ? <WorkbenchAudit events={snapshot.auditEvents} /> : null}
      </div>
    </div>
  );
}
