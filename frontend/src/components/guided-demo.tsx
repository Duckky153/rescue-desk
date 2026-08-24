"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import type { WorkbenchTab } from "@/components/workbench-api";
import type { CaseStatus } from "@/types/api";
import styles from "./guided-demo.module.css";

interface TourStep {
  eyebrow: string;
  title: string;
  body: string;
  instruction: string;
  tab?: WorkbenchTab;
}

const SECTION_LABELS: Array<{ tab: WorkbenchTab; label: string }> = [
  { tab: "evidence", label: "Evidence" },
  { tab: "economics", label: "Economics" },
  { tab: "readiness", label: "Readiness" },
  { tab: "audit", label: "Audit" },
];

function statusSummary(
  status: CaseStatus,
  blockerCount: number,
  isSeededCompletedExample: boolean,
): string {
  if (isSeededCompletedExample) {
    return "Seeded comparison: role-separated approver history and four exports";
  }
  if (status === "exported") return "Completed workflow: recorded approval and exports";
  if (blockerCount === 1) return "Guided example: one source decision before internal readiness";
  if (blockerCount === 0) return "Evidence complete: ready for the next controlled workflow gate";
  return `${blockerCount} evidence decisions still block internal review`;
}

export function GuidedDemo({
  status,
  blockerCount,
  isSeededCompletedExample = false,
  activeTab,
  onSelectTab,
  onReviewDecision,
}: {
  status: CaseStatus;
  blockerCount: number;
  isSeededCompletedExample?: boolean;
  activeTab: WorkbenchTab;
  onSelectTab: (tab: WorkbenchTab) => void;
  onReviewDecision: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [stepIndex, setStepIndex] = useState(0);
  const guideRef = useRef<HTMLElement>(null);
  const startRef = useRef<HTMLButtonElement>(null);
  const returnFocusPending = useRef(false);
  const steps = useMemo<TourStep[]>(
    () => [
      {
        eyebrow: "0:00 - 0:25 · The story",
        title: "A contract exit should not depend on a spreadsheet guess.",
        body:
          "Northstar wants to leave a legacy ERP. An implementation analyst must prove what the contract says, calculate the remaining subscription consistently, and preserve an accountable approval trail.",
        instruction:
          "In three minutes, follow one source fact through evidence, math, readiness, and audit history.",
        tab: "evidence",
      },
      {
        eyebrow: "0:25 - 1:05 · Evidence",
        title: "Check the exact words before accepting a fact.",
        body:
          "The proposal called Renewal · notice days is the only unfinished decision in the guided case. Its page, quotation, offsets, and hashes stay attached to the proposed 90-day value.",
        instruction:
          "Select the proposal, compare it with the highlighted quote, and record Accept only if the source supports it.",
        tab: "evidence",
      },
      {
        eyebrow: "1:05 - 1:40 · Economics",
        title: "The model proposes facts; deterministic code does the math.",
        body:
          "The reviewed USD 120,000 subscription becomes a typed obligation. A versioned formula calculates USD 48,657.53 remaining as of August 20, 2026 and exposes its inputs and hashes.",
        instruction: "Inspect the obligation ledger, formula identifier, scenario range, and result hash.",
        tab: "economics",
      },
      {
        eyebrow: "1:40 - 2:15 · Readiness",
        title: "Uncertainty blocks the workflow instead of disappearing.",
        body:
          "Readiness is recomputed from current evidence, reviewed obligations, and calculation integrity. An analyst cannot bypass the separate approver-role gate.",
        instruction:
          isSeededCompletedExample
            ? "Inspect the zero-blocker controls and four downloadable formats generated from the seeded approver-role checkpoint."
            : status === "exported"
              ? "Inspect the zero-blocker controls and the four downloadable packet formats."
            : "See the single named blocker. After its evidence decision, this case becomes ready for internal review.",
        tab: "readiness",
      },
      {
        eyebrow: "2:15 - 2:45 · Audit",
        title: "Every consequential action leaves an inspectable record.",
        body:
          "Uploads, extraction, reviews, calculations, transitions, approvals, and exports record actor, reason, correlation ID, before/after data, and forward-linked hashes.",
        instruction: "Expand an event and inspect its exact payload and hash lineage.",
        tab: "audit",
      },
      {
        eyebrow: "2:45 - 3:00 · Outcome",
        title:
          isSeededCompletedExample
            ? "This seeded example records an exported workflow outcome."
            : status === "exported"
              ? "This matter reached a recorded, exported outcome."
            : "One meaningful evidence decision completes the guided checkpoint.",
        body:
          isSeededCompletedExample
            ? "The completed comparison exercises cited facts, deterministic calculation, seeded synthetic approver-role history, four immutable packet formats, and an audit trail. It is not represented as a human approval."
            : status === "exported"
              ? "The recorded workflow connects cited facts, deterministic calculation, an approver-role checkpoint, immutable packet formats, and an audit trail."
              : "The guided case stays honest: it does not pretend an undecided fact is complete. The Matters page also contains a seeded role-separated comparison case so the finished workflow is inspectable.",
        instruction: "Return to Matters to compare the one-action case with the completed exit packet.",
        tab: "readiness",
      },
    ],
    [isSeededCompletedExample, status],
  );
  const step = steps[stepIndex];

  useEffect(() => {
    if (open) {
      guideRef.current?.focus();
    } else if (returnFocusPending.current) {
      returnFocusPending.current = false;
      startRef.current?.focus();
    }
  }, [open]);

  function showStep(nextIndex: number) {
    const bounded = Math.max(0, Math.min(steps.length - 1, nextIndex));
    setStepIndex(bounded);
    const targetTab = steps[bounded].tab;
    if (targetTab) onSelectTab(targetTab);
    queueMicrotask(() => guideRef.current?.focus());
  }

  function start() {
    setOpen(true);
    showStep(0);
  }

  function dismiss() {
    returnFocusPending.current = true;
    setOpen(false);
  }

  function handleKeyboard(event: KeyboardEvent<HTMLElement>) {
    const target = event.target;
    if (
      target instanceof HTMLInputElement ||
      target instanceof HTMLTextAreaElement ||
      target instanceof HTMLSelectElement
    ) {
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      dismiss();
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      showStep(stepIndex + 1);
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      showStep(stepIndex - 1);
    }
  }

  if (!open) {
    return (
      <section className={styles.storyCard} aria-labelledby="demo-story-title">
        <div className={styles.storyLead}>
          <span className={styles.kicker}>Recommended three-minute demo</span>
          <h2 id="demo-story-title">Understand the product before clicking through it.</h2>
          <p>{statusSummary(status, blockerCount, isSeededCompletedExample)}</p>
        </div>
        <dl className={styles.storyFacts}>
          <div>
            <dt>Problem</dt>
            <dd>A company cannot safely leave its ERP until contract obligations are proved.</dd>
          </div>
          <div>
            <dt>User</dt>
            <dd>An implementation analyst coordinating evidence, economics, and approval.</dd>
          </div>
          <div>
            <dt>Outcome</dt>
            <dd>A cited, reproducible, approver-controlled contract-exit packet.</dd>
          </div>
        </dl>
        <div className={styles.storyActions}>
          <button type="button" className="button" onClick={onReviewDecision}>
            Review final decision
            <span aria-hidden="true">→</span>
          </button>
          <button ref={startRef} type="button" className="button secondary" onClick={start}>
            Start full tour
          </button>
        </div>
      </section>
    );
  }

  return (
    <section
      ref={guideRef}
      className={styles.guide}
      aria-labelledby="guided-demo-title"
      aria-describedby="guided-demo-body"
      aria-keyshortcuts="ArrowRight ArrowLeft Escape"
      onKeyDown={handleKeyboard}
      tabIndex={-1}
    >
      <div className={styles.guideRail} aria-label={`Guided demo step ${stepIndex + 1} of ${steps.length}`}>
        <div className={styles.guideTopline}>
          <span>Three-minute product tour</span>
          <span>{stepIndex + 1} / {steps.length}</span>
        </div>
        <ol className={styles.progress} aria-label="Tour progress">
          {steps.map((item, index) => (
            <li key={item.title} data-current={index === stepIndex} data-complete={index < stepIndex}>
              <button type="button" onClick={() => showStep(index)} aria-current={index === stepIndex ? "step" : undefined}>
                <span>{index < stepIndex ? "✓" : index + 1}</span>
                <span className="sr-only">{item.title}</span>
              </button>
            </li>
          ))}
        </ol>
        <p className={styles.keyboardHint}>Keyboard: ← previous · → next · Esc dismiss</p>
      </div>

      <div className={styles.guideBody} aria-live="polite">
        <span className={styles.kicker}>{step.eyebrow}</span>
        <h2 id="guided-demo-title">{step.title}</h2>
        <p id="guided-demo-body">{step.body}</p>
        <div className={styles.instruction}>
          <span aria-hidden="true">→</span>
          <strong>{step.instruction}</strong>
        </div>
        <nav className={styles.sectionJumps} aria-label="Open a workbench section">
          {SECTION_LABELS.map(({ tab, label }) => (
            <button
              key={tab}
              type="button"
              aria-pressed={activeTab === tab}
              onClick={() => {
                onSelectTab(tab);
                const matchingStep = steps.findIndex((item, index) => index > 0 && item.tab === tab);
                if (matchingStep >= 0) setStepIndex(matchingStep);
              }}
            >
              {label}
            </button>
          ))}
        </nav>
      </div>

      <div className={styles.guideActions}>
        <button type="button" className="button ghost small" onClick={() => showStep(0)}>
          Restart
        </button>
        <button type="button" className="button secondary small" onClick={dismiss}>
          Dismiss tour
        </button>
        {stepIndex > 0 ? (
          <button type="button" className="button secondary small" onClick={() => showStep(stepIndex - 1)}>
            Previous
          </button>
        ) : null}
        {stepIndex < steps.length - 1 ? (
          <button type="button" className="button small" onClick={() => showStep(stepIndex + 1)}>
            Next
          </button>
        ) : (
          <Link href="/" className="button small">
            Compare both matters
          </Link>
        )}
      </div>
    </section>
  );
}
