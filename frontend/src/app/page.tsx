"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { AppShell } from "@/components/app-shell";
import { RequireAuth, useAuth } from "@/components/auth-provider";
import { CaseStatusPill } from "@/components/status-pill";
import { ApiError, apiRequest, stableIntentFingerprint } from "@/lib/api";
import { DEMO_COMPLETED_CASE_NAME, DEMO_GUIDED_CASE_NAME } from "@/lib/demo";
import type { CaseDetail, CaseSummary } from "@/types/api";
import { canCreateMatter } from "@/lib/permissions";
import styles from "./page.module.css";

function formatDate(value: string): string {
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric" }).format(
    new Date(value),
  );
}

export default function MattersPage() {
  const { user } = useAuth();
  const [cases, setCases] = useState<CaseSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [query, setQuery] = useState("");
  const [listIsStale, setListIsStale] = useState(false);
  const canCreate = canCreateMatter(user?.role);

  const loadCases = useCallback(async () => {
    setLoading(true);
    try {
      const nextCases = await apiRequest<CaseSummary[]>("/v1/cases");
      setCases(nextCases);
      setError("");
      setListIsStale(false);
      return true;
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Unable to load matters");
      setListIsStale(true);
      return false;
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let active = true;
    queueMicrotask(() => {
      if (active) void loadCases();
    });
    return () => {
      active = false;
    };
  }, [loadCases]);

  const filtered = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    if (!normalized) return cases;
    return cases.filter((item) =>
      [item.display_name, item.applicant_company, item.erp_provider, item.status]
        .join(" ")
        .toLowerCase()
        .includes(normalized),
    );
  }, [cases, query]);
  const guidedDemoCase = cases.find((item) => item.display_name === DEMO_GUIDED_CASE_NAME);
  const completedDemoCase = cases.find((item) => item.display_name === DEMO_COMPLETED_CASE_NAME);

  async function createMatter(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    setCreating(true);
    setError("");
    const payload = {
      display_name: String(form.get("display_name") ?? ""),
      applicant_company: String(form.get("applicant_company") ?? ""),
      erp_provider: String(form.get("erp_provider") ?? ""),
    };
    let created: CaseDetail;
    try {
      created = await apiRequest<CaseDetail>(
        "/v1/cases",
        {
          method: "POST",
          body: JSON.stringify(payload),
        },
        {
          intent: {
            scope: "create-case",
            fingerprint: stableIntentFingerprint(payload),
          },
        },
      );
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Unable to create matter");
      setCreating(false);
      return;
    }

    formElement.reset();
    setShowCreate(false);
    setCases((current) => [created, ...current.filter((item) => item.id !== created.id)]);
    const refreshed = await loadCases();
    if (!refreshed) {
      setError(
        "The matter was created and is shown from its save receipt, but the matter list could not refresh. Reload matters before creating another.",
      );
    }
    setCreating(false);
  }

  return (
    <RequireAuth>
      <AppShell>
        <div className={styles.page}>
          <header className={styles.header}>
            <div>
              <p className={styles.eyebrow}>Contract portfolio</p>
              <h1>Exit-review matters</h1>
              <p>Trace every number to the contract before anyone makes a decision.</p>
            </div>
            {canCreate ? (
              <button
                className="button"
                type="button"
                onClick={() => setShowCreate((value) => !value)}
                disabled={listIsStale || loading}
              >
                {showCreate ? "Close new matter" : "New matter"}
              </button>
            ) : null}
          </header>

          {error ? (
            <div className="alert danger" role="alert">
              <span>{error}</span>
              {listIsStale ? (
                <button
                  className="button danger small"
                  type="button"
                  onClick={() => void loadCases()}
                  disabled={loading}
                >
                  {loading ? "Reloading…" : "Reload matters"}
                </button>
              ) : null}
            </div>
          ) : null}

          {showCreate && canCreate ? (
            <section className={`${styles.createPanel} panel`} aria-labelledby="new-matter-title">
              <div>
                <p className={styles.eyebrow}>New review</p>
                <h2 id="new-matter-title">Open an evidence workspace</h2>
                <p className="muted">
                  Start with the customer and legacy platform. The contract comes next.
                </p>
              </div>
              <form onSubmit={createMatter} className={styles.createForm}>
                <div className="field">
                  <label htmlFor="display_name">Matter name</label>
                  <input className="input" id="display_name" name="display_name" required minLength={2} />
                </div>
                <div className="field">
                  <label htmlFor="applicant_company">Company</label>
                  <input
                    className="input"
                    id="applicant_company"
                    name="applicant_company"
                    required
                    minLength={2}
                  />
                </div>
                <div className="field">
                  <label htmlFor="erp_provider">Legacy ERP</label>
                  <input className="input" id="erp_provider" name="erp_provider" required minLength={2} />
                </div>
                <button className="button" type="submit" disabled={creating || listIsStale}>
                  {creating ? "Opening…" : "Open matter"}
                </button>
              </form>
            </section>
          ) : null}

          {guidedDemoCase && completedDemoCase ? (
            <section className={styles.demoRoute} aria-labelledby="recommended-demo-title">
              <div className={styles.demoRouteIntro}>
                <span className={styles.eyebrow}>Recommended three-minute route</span>
                <h2 id="recommended-demo-title">
                  Make one live decision against synthetic evidence, then inspect the finished result.
                </h2>
                <p>
                  Northstar is leaving a legacy ERP. RescueDesk keeps every number tied to source
                  language, deterministic math, and a named review record.
                </p>
                <span className={styles.syntheticNote}>Synthetic data only · no external action</span>
              </div>
              <ol className={styles.demoRouteSteps}>
                <li>
                  <span>1</span>
                  <div>
                    <strong>Make the final evidence decision</strong>
                    <p>One 90-day cancellation-notice fact remains proposed.</p>
                    <Link className="button" href={`/cases/${guidedDemoCase.id}`}>
                      Start guided case
                    </Link>
                  </div>
                </li>
                <li>
                  <span>2</span>
                  <div>
                    <strong>Inspect the completed packet</strong>
                    <p>
                      Zero blockers, seeded synthetic approver-role history, and four export
                      formats.
                    </p>
                    <Link className="button secondary" href={`/cases/${completedDemoCase.id}`}>
                      Open completed example
                    </Link>
                  </div>
                </li>
              </ol>
            </section>
          ) : null}

          <section className={`${styles.tablePanel} panel`} aria-labelledby="matters-heading">
            <div className={styles.tableToolbar}>
              <div>
                <h2 id="matters-heading">Matters</h2>
                <span className="muted">{cases.length} total</span>
              </div>
              <label className={styles.search}>
                <span className="sr-only">Search matters</span>
                <input
                  className="input"
                  type="search"
                  placeholder="Search company, ERP, or status"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                />
              </label>
            </div>
            {loading ? (
              <div className={styles.empty}>Loading matters…</div>
            ) : filtered.length === 0 ? (
              <div className={styles.empty}>
                <strong>{cases.length ? "No matching matters" : "No matters yet"}</strong>
                <span>
                  {cases.length
                    ? "Try a different search."
                    : "Open the first evidence workspace to begin."}
                </span>
              </div>
            ) : (
              <div
                className={styles.tableScroll}
                role="region"
                aria-label="Matter list"
                tabIndex={0}
              >
                <table className={styles.table}>
                  <thead>
                    <tr>
                      <th>Matter</th>
                      <th>Legacy system</th>
                      <th>Review state</th>
                      <th>Revision</th>
                      <th>Updated</th>
                      <th>
                        <span className="sr-only">Open</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {filtered.map((item) => (
                      <tr key={item.id}>
                        <td>
                          <Link href={`/cases/${item.id}`} className={styles.matterLink}>
                            <strong>{item.display_name}</strong>
                            <span>{item.applicant_company}</span>
                          </Link>
                        </td>
                        <td>{item.erp_provider}</td>
                        <td>
                          <CaseStatusPill status={item.status} />
                        </td>
                        <td>R{item.current_revision_number}</td>
                        <td>{formatDate(item.updated_at)}</td>
                        <td>
                          <Link className="button secondary small" href={`/cases/${item.id}`}>
                            Review
                          </Link>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </div>
      </AppShell>
    </RequireAuth>
  );
}
