"use client";

import { useMemo, useState, type FormEvent } from "react";
import { useAuth } from "@/components/auth-provider";
import {
  addFee,
  runCalculation,
  supersedeFee,
  type WorkbenchAssertion,
} from "@/components/workbench-api";
import {
  CURRENCY_EXPONENTS,
  errorMessage,
  evidenceHasActiveDocument,
  evidenceUsesOnlySupersededDocuments,
  formatDate,
  formatMinor,
  humanize,
  localDateInputValue,
  minorUnitsBigInt,
  parseMoneyToMinor,
  type SupportedCurrency,
} from "@/components/workbench-utils";
import type { Calculation, DocumentSummary, Fee } from "@/types/api";
import type { CaseStatus } from "@/types/api";
import { canManageEconomics, isEvidenceMutable } from "@/lib/permissions";
import styles from "./workbench.module.css";

const FEE_CATEGORIES: Fee["category"][] = [
  "subscription",
  "implementation",
  "professional_service",
  "termination",
  "penalty",
  "tax",
  "other",
  "unclassified",
];
const SUPPORTED_CURRENCIES = Object.keys(CURRENCY_EXPONENTS) as SupportedCurrency[];
const PRIMARY_MONEY_SEMANTIC_BY_CATEGORY: Partial<Record<Fee["category"], string>> = {
  subscription: "fee.subscription",
  implementation: "fee.implementation",
  termination: "fee.termination",
};

function hasUsableTypedMoneyValue(assertion: WorkbenchAssertion): boolean {
  const value = assertion.normalized_value;
  const keys = Object.keys(value).sort();
  if (keys.join("|") !== "amount_minor|cadence|currency|type") return false;
  if (value.type !== "money") return false;
  if (typeof value.amount_minor !== "number" || !Number.isSafeInteger(value.amount_minor)) {
    return false;
  }
  if (value.amount_minor < 0) return false;
  if (typeof value.currency !== "string" || !SUPPORTED_CURRENCIES.includes(value.currency as SupportedCurrency)) {
    return false;
  }
  const allowedCadences =
    assertion.semantic_key === "fee.subscription" ? ["annual", "monthly"] : ["one_time"];
  return typeof value.cadence === "string" && allowedCadences.includes(value.cadence);
}

function scenarioInputWarning(fee: Fee): string | null {
  if (fee.superseded || fee.category !== "subscription" || fee.payment_status === "paid") {
    return null;
  }
  if (fee.payment_status === "unknown") {
    return "Scenario input incomplete: resolve payment status.";
  }
  const missing: string[] = [];
  if (!fee.service_start) missing.push("service start");
  if (!fee.service_end) missing.push("service end");
  if (!fee.proration_rule) {
    missing.push("proration rule");
  } else if (fee.proration_rule !== "contract_daily") {
    missing.push("supported proration rule");
  }
  return missing.length
    ? `Scenario input incomplete: resolve ${missing.join(", ")}.`
    : null;
}

interface WorkbenchEconomicsProps {
  caseId: string;
  caseStatus: CaseStatus;
  caseVersion: number;
  fees: Fee[];
  assertions: WorkbenchAssertion[];
  calculation: Calculation | null;
  calculationIsCurrent: boolean;
  documents?: DocumentSummary[];
  mutationsBlocked?: boolean;
  onRefresh: () => Promise<void>;
}

export function FeeEntryForm({
  caseId,
  assertions,
  documents = [],
  consumedAssertionIds = new Set<string>(),
  consumedSemanticKeys = new Set<string>(),
  onAdded,
  canEdit = true,
  unavailableMessage = "Your role can inspect obligation inputs but cannot add or recalculate them.",
}: {
  caseId: string;
  assertions: WorkbenchAssertion[];
  documents?: DocumentSummary[];
  consumedAssertionIds?: ReadonlySet<string>;
  consumedSemanticKeys?: ReadonlySet<string>;
  onAdded: () => Promise<void>;
  canEdit?: boolean;
  unavailableMessage?: string;
}) {
  const [category, setCategory] = useState<Fee["category"]>("subscription");
  const [amount, setAmount] = useState("");
  const [currency, setCurrency] = useState<SupportedCurrency>("USD");
  const [serviceStart, setServiceStart] = useState("");
  const [serviceEnd, setServiceEnd] = useState("");
  const [obligationDate, setObligationDate] = useState("");
  const [paymentStatus, setPaymentStatus] = useState<Fee["payment_status"]>("unknown");
  const [billingCadence, setBillingCadence] = useState("");
  const [prorationRule, setProrationRule] = useState("");
  const [assertionId, setAssertionId] = useState("");
  const [reviewed, setReviewed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const expectedMoneySemanticKey = PRIMARY_MONEY_SEMANTIC_BY_CATEGORY[category];

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    let amountMinor: number;
    try {
      amountMinor = parseMoneyToMinor(amount, currency);
    } catch (parseError) {
      setError(errorMessage(parseError));
      return;
    }
    if (reviewed && !assertionId) {
      setError("A reviewed obligation must link to a supporting reviewed fact.");
      return;
    }
    if (reviewed && !billingCadence) {
      setError("A reviewed obligation must record the billing cadence shown by its money fact.");
      return;
    }
    setBusy(true);
    try {
      await addFee(caseId, {
        category,
        amount_minor: amountMinor,
        currency: currency.trim().toUpperCase(),
        service_start: serviceStart || null,
        service_end: serviceEnd || null,
        obligation_date: obligationDate || null,
        payment_status: paymentStatus,
        billing_cadence: billingCadence || null,
        proration_rule: prorationRule || null,
        assertion_ids: assertionId ? [assertionId] : [],
        reviewed,
      });
      setAmount("");
      setServiceStart("");
      setServiceEnd("");
      setObligationDate("");
      setAssertionId("");
      setReviewed(false);
      setPaymentStatus("unknown");
      setBillingCadence("");
      setProrationRule("");
    } catch (requestError) {
      setError(errorMessage(requestError));
      setBusy(false);
      return;
    }
    try {
      await onAdded();
    } catch {
      setError(
        "The obligation was saved, but the workspace could not refresh. Reload before retrying.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className={styles.feeForm} onSubmit={submit}>
      <div className={styles.sectionHeading}>
        <div>
          <span className={styles.eyebrow}>Reviewed input</span>
          <h2>Add a contract obligation</h2>
        </div>
        <span className={styles.inputRule}>Stored in integer minor units</span>
      </div>
      <div className={styles.formGrid}>
        <div className="field">
          <label htmlFor="fee-category">Category</label>
          <select
            id="fee-category"
            className="select"
            value={category}
            onChange={(event) => {
              setCategory(event.target.value as Fee["category"]);
              setServiceStart("");
              setServiceEnd("");
              setObligationDate("");
              setPaymentStatus("unknown");
              setBillingCadence("");
              setProrationRule("");
              setAssertionId("");
              setReviewed(false);
              setError(null);
            }}
            disabled={!canEdit}
          >
            {FEE_CATEGORIES.map((item) => (
              <option value={item} key={item}>
                {humanize(item)}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="fee-amount">Active input amount</label>
          <div className={styles.moneyInput}>
            <input
              id="fee-amount"
              className="input"
              inputMode="decimal"
              placeholder="12000.00"
              value={amount}
              onChange={(event) => setAmount(event.target.value)}
              required
              disabled={!canEdit}
            />
            <select
              aria-label="Currency"
              className="select"
              value={currency}
              onChange={(event) => setCurrency(event.target.value as SupportedCurrency)}
              disabled={!canEdit}
            >
              {SUPPORTED_CURRENCIES.map((item) => (
                <option value={item} key={item}>
                  {item}
                </option>
              ))}
            </select>
          </div>
        </div>
        <div className="field">
          <label htmlFor="service-start">Service start</label>
          <input
            id="service-start"
            className="input"
            type="date"
            value={serviceStart}
            onChange={(event) => setServiceStart(event.target.value)}
            disabled={!canEdit}
          />
        </div>
        <div className="field">
          <label htmlFor="service-end">Service end</label>
          <input
            id="service-end"
            className="input"
            type="date"
            value={serviceEnd}
            onChange={(event) => setServiceEnd(event.target.value)}
            disabled={!canEdit}
          />
        </div>
        <div className="field">
          <label htmlFor="obligation-date">Obligation date</label>
          <input
            id="obligation-date"
            className="input"
            type="date"
            value={obligationDate}
            onChange={(event) => setObligationDate(event.target.value)}
            disabled={!canEdit}
          />
        </div>
        <div className="field">
          <label htmlFor="payment-status">Payment status</label>
          <select
            id="payment-status"
            className="select"
            value={paymentStatus}
            onChange={(event) =>
              setPaymentStatus(event.target.value as Fee["payment_status"])
            }
            disabled={!canEdit}
          >
            <option value="unknown">Unknown — requires review</option>
            <option value="unpaid">Unpaid</option>
            <option value="paid">Paid</option>
          </select>
        </div>
        <div className="field">
          <label htmlFor="billing-cadence">Billing cadence</label>
          <select
            id="billing-cadence"
            className="select"
            value={billingCadence}
            onChange={(event) => setBillingCadence(event.target.value)}
            disabled={!canEdit}
          >
            <option value="annual">Annual</option>
            <option value="quarterly">Quarterly</option>
            <option value="monthly">Monthly</option>
            <option value="one_time">One time</option>
            <option value="">Not documented</option>
          </select>
        </div>
        <div className="field">
          <label htmlFor="proration-rule">Proration rule</label>
          <select
            id="proration-rule"
            className="select"
            value={prorationRule}
            onChange={(event) => setProrationRule(event.target.value)}
            disabled={!canEdit}
          >
            <option value="contract_daily">Contract daily</option>
            <option value="none">No proration</option>
            <option value="">Not documented</option>
          </select>
        </div>
        <div className={`${styles.fullField} field`}>
          <label htmlFor="fee-assertion">Supporting reviewed fact</label>
          <select
            id="fee-assertion"
            className="select"
            value={assertionId}
            onChange={(event) => setAssertionId(event.target.value)}
            disabled={!canEdit || !expectedMoneySemanticKey}
          >
            <option value="">No assertion link</option>
            {assertions
              .filter(
                (item) =>
                  item.semantic_key === expectedMoneySemanticKey &&
                  item.is_current &&
                  ["accepted", "corrected"].includes(item.review_state) &&
                  ["explicit_assumption", "source_evidence"].includes(item.evidence_basis) &&
                  evidenceHasActiveDocument(item.evidence, documents) &&
                  hasUsableTypedMoneyValue(item) &&
                  !consumedAssertionIds.has(item.id) &&
                  !consumedSemanticKeys.has(item.semantic_key),
              )
              .map((assertion) => (
                <option key={assertion.id} value={assertion.id}>
                  {humanize(assertion.semantic_key)} — {assertion.display_value} —{" "}
                  {assertion.assumption || assertion.evidence_basis === "explicit_assumption"
                    ? "Explicit assumption"
                    : "Source-confirmed"}
                </option>
              ))}
          </select>
        </div>
      </div>
      {!canEdit ? (
        <div className="alert warning">{unavailableMessage}</div>
      ) : null}
      <label className={styles.checkboxRow}>
        <input
          type="checkbox"
          checked={reviewed}
          onChange={(event) => setReviewed(event.target.checked)}
          disabled={!canEdit || !expectedMoneySemanticKey}
        />
        I verified this obligation&apos;s category, amount, currency, and cadence against the
        selected money fact
      </label>
      <p className={styles.formNote}>
        Payment status, service dates, and proration are explicit scenario inputs. The selected
        money fact substantiates only category, amount, currency, and cadence.
      </p>
      {!expectedMoneySemanticKey ? (
        <p className={styles.formNote}>
          This category has no typed money-fact contract in the current demonstration and cannot
          be marked reviewed.
        </p>
      ) : null}
      {!reviewed ? (
        <p className={styles.formNote}>
          Unreviewed fees remain visible but cannot support a documented subscription remainder.
        </p>
      ) : null}
      {error ? (
        <div className="alert danger" role="alert">
          {error}
        </div>
      ) : null}
      <button type="submit" className="button" disabled={busy || !canEdit}>
        {busy ? "Adding obligation…" : "Add obligation"}
      </button>
    </form>
  );
}

export function WorkbenchEconomics({
  caseId,
  caseStatus,
  caseVersion,
  fees,
  assertions,
  calculation,
  calculationIsCurrent,
  documents = [],
  mutationsBlocked = false,
  onRefresh,
}: WorkbenchEconomicsProps) {
  const { user } = useAuth();
  const canEdit = !mutationsBlocked && canManageEconomics(user?.role, caseStatus);
  const evidenceMutable = isEvidenceMutable(caseStatus);
  const [asOfDate, setAsOfDate] = useState(() => localDateInputValue());
  const [calculating, setCalculating] = useState(false);
  const [calculationError, setCalculationError] = useState<string | null>(null);
  const [selectedFeeId, setSelectedFeeId] = useState<string | null>(null);
  const [supersedeReason, setSupersedeReason] = useState("");
  const [superseding, setSuperseding] = useState(false);
  const [supersedeError, setSupersedeError] = useState<string | null>(null);
  const activeFees = useMemo(() => fees.filter((fee) => !fee.superseded), [fees]);
  const consumedAssertionIds = useMemo(
    () => new Set(activeFees.flatMap((fee) => fee.assertion_ids)),
    [activeFees],
  );
  const consumedSemanticKeys = useMemo(
    () =>
      new Set(
        activeFees.flatMap((fee) => {
          if (!fee.primary_money_assertion_id) return [];
          const semanticKey = PRIMARY_MONEY_SEMANTIC_BY_CATEGORY[fee.category];
          return semanticKey ? [semanticKey] : [];
        }),
      ),
    [activeFees],
  );
  const selectedFee = fees.find((fee) => fee.id === selectedFeeId && !fee.superseded) ?? null;
  const totals = useMemo(() => {
    const grouped = new Map<string, bigint>();
    for (const fee of activeFees) {
      grouped.set(
        fee.currency,
        (grouped.get(fee.currency) ?? 0n) + minorUnitsBigInt(fee.amount_minor),
      );
    }
    return [...grouped.entries()];
  }, [activeFees]);

  async function calculate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setCalculating(true);
    setCalculationError(null);
    try {
      await runCalculation(caseId, asOfDate);
    } catch (error) {
      setCalculationError(errorMessage(error));
      setCalculating(false);
      return;
    }
    try {
      await onRefresh();
    } catch {
      setCalculationError(
        "The calculation was saved, but the workspace could not refresh. Reload before rerunning it.",
      );
    } finally {
      setCalculating(false);
    }
  }

  async function submitSupersede(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedFee) return;
    if (supersedeReason.trim().length < 3) {
      setSupersedeError("Record a recovery reason of at least three characters.");
      return;
    }
    setSuperseding(true);
    setSupersedeError(null);
    try {
      await supersedeFee(caseId, selectedFee.id, caseVersion, supersedeReason.trim());
      setSelectedFeeId(null);
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
        "The obligation was superseded, but the workspace could not refresh. Reload before retrying.",
      );
    } finally {
      setSuperseding(false);
    }
  }

  return (
    <div className={styles.economicsLayout}>
      <section className={`${styles.economicsInput} panel`}>
        <FeeEntryForm
          caseId={caseId}
          assertions={assertions}
          documents={documents}
          consumedAssertionIds={consumedAssertionIds}
          consumedSemanticKeys={consumedSemanticKeys}
          onAdded={onRefresh}
          canEdit={canEdit}
          unavailableMessage={
            caseStatus === "archived"
              ? "Archived cases are permanently read-only and have no recovery transition."
              : evidenceMutable
              ? "Your role can inspect obligation inputs but cannot add or recalculate them."
              : "Obligation inputs are locked at this workflow state. Reopen evidence review before changing them."
          }
        />
      </section>
      <section className={`${styles.obligationPanel} panel`}>
        <div className={styles.sectionHeading}>
          <div>
            <span className={styles.eyebrow}>Calculation inputs</span>
            <h2>Obligation ledger</h2>
          </div>
          <span className={styles.countBadge}>
            {activeFees.length} current · {fees.length} total
          </span>
        </div>
        {totals.length ? (
          <div className={styles.totalStrip}>
            {totals.map(([currency, total]) => (
              <div key={currency}>
                <span>Active input total · {currency} · current inputs</span>
                <strong>{formatMinor(total, currency)}</strong>
              </div>
            ))}
          </div>
        ) : null}
        <div
          className={styles.tableScroller}
          role="region"
          aria-label="Obligation ledger"
          tabIndex={0}
        >
          <table className={styles.dataTable}>
            <thead>
              <tr>
                <th>Category</th>
                <th>Amount</th>
                <th>Service period</th>
                <th>Cadence</th>
                <th>Payment</th>
                <th>Proration</th>
                <th>Evidence</th>
                <th>Status</th>
                <th>Recovery</th>
              </tr>
            </thead>
            <tbody>
              {fees.map((fee) => {
                const primaryAssertion = assertions.find(
                  (item) => item.id === fee.primary_money_assertion_id,
                );
                const isAssumption = Boolean(
                  primaryAssertion &&
                    (primaryAssertion.assumption ||
                      primaryAssertion.evidence_basis === "explicit_assumption"),
                );
                const evidenceChanged = Boolean(primaryAssertion && !primaryAssertion.is_current);
                const evidenceSuperseded = Boolean(
                  primaryAssertion &&
                    evidenceUsesOnlySupersededDocuments(primaryAssertion.evidence, documents),
                );
                const evidenceInvalid = evidenceChanged || evidenceSuperseded;
                const scenarioWarning = scenarioInputWarning(fee);
                return (
                <tr key={fee.id}>
                  <td>{humanize(fee.category)}</td>
                  <td className={styles.numeric}>{formatMinor(fee.amount_minor, fee.currency)}</td>
                  <td>
                    {formatDate(fee.service_start)} – {formatDate(fee.service_end)}
                  </td>
                  <td>{fee.billing_cadence ? humanize(fee.billing_cadence) : "Not documented"}</td>
                  <td>{humanize(fee.payment_status)}</td>
                  <td>{fee.proration_rule ? humanize(fee.proration_rule) : "Not documented"}</td>
                  <td>
                    {primaryAssertion ? (
                      <>
                        <span
                          className={`status-pill ${evidenceInvalid ? "danger" : isAssumption ? "warning" : "success"}`}
                        >
                          {evidenceChanged
                            ? "Evidence changed"
                            : evidenceSuperseded
                              ? "Superseded source"
                              : isAssumption
                                ? "Explicit assumption"
                                : "Source-confirmed"}
                        </span>
                        <small className={styles.cellDetail}>{primaryAssertion.display_value}</small>
                        {evidenceInvalid ? (
                          <small className={styles.cellDetail}>
                            Supersede and re-enter this fee against current active evidence.
                          </small>
                        ) : null}
                      </>
                    ) : fee.assertion_ids.length ? (
                      `${fee.assertion_ids.length} linked · no primary money fact`
                    ) : (
                      "None linked"
                    )}
                  </td>
                  <td>
                    <span
                      className={`status-pill ${fee.superseded ? "" : fee.reviewed && !evidenceInvalid ? "success" : "warning"}`}
                    >
                      {fee.superseded
                        ? "Superseded"
                        : fee.reviewed && !evidenceInvalid
                          ? "Money evidence reviewed"
                          : evidenceInvalid
                            ? "Evidence changed"
                            : "Money evidence not reviewed"}
                    </span>
                    {scenarioWarning ? (
                      <small className={styles.cellDetail}>
                        {scenarioWarning}
                      </small>
                    ) : null}
                    {fee.superseded && fee.supersede_reason ? (
                      <small className={styles.cellDetail}>{fee.supersede_reason}</small>
                    ) : null}
                  </td>
                  <td>
                    {fee.superseded ? (
                      <span className={styles.inputRule}>Historical only</span>
                    ) : canEdit ? (
                      <button
                        type="button"
                        className="button secondary small"
                        aria-label={`Supersede ${humanize(fee.category)} obligation ${formatMinor(fee.amount_minor, fee.currency)}`}
                        onClick={() => {
                          setSelectedFeeId(fee.id);
                          setSupersedeReason("");
                          setSupersedeError(null);
                        }}
                      >
                        Supersede
                      </button>
                    ) : (
                      <span className={styles.inputRule}>Locked</span>
                    )}
                  </td>
                </tr>
                );
              })}
              {!fees.length ? (
                <tr>
                  <td colSpan={9} className={styles.emptyTable}>
                    No obligations yet. Add the documented subscription commitment first.
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
        {selectedFee ? (
          <form className={styles.transitionForm} onSubmit={submitSupersede}>
            <div className="alert warning">
              <strong>Preserve and remove this input from future calculations.</strong> The original
              {" "}{formatMinor(selectedFee.amount_minor, selectedFee.currency)} row remains in the
              audit history as superseded.
            </div>
            <div className="field">
              <label htmlFor="fee-supersede-reason">Recovery reason</label>
              <textarea
                id="fee-supersede-reason"
                className="textarea"
                value={supersedeReason}
                onChange={(event) => setSupersedeReason(event.target.value)}
                placeholder="Explain why this input must no longer affect the calculation…"
                required
              />
            </div>
            {supersedeError ? (
              <div className="alert danger" role="alert">
                {supersedeError}
              </div>
            ) : null}
            <div className={styles.exportActions}>
              <button
                type="button"
                className="button secondary"
                disabled={superseding}
                onClick={() => {
                  setSelectedFeeId(null);
                  setSupersedeReason("");
                  setSupersedeError(null);
                }}
              >
                Cancel
              </button>
              <button type="submit" className="button danger" disabled={superseding}>
                {superseding ? "Superseding input…" : "Confirm supersede"}
              </button>
            </div>
          </form>
        ) : null}
      </section>

      <section className={`${styles.calculationPanel} panel`}>
        <div className={styles.sectionHeading}>
          <div>
            <span className={styles.eyebrow}>Deterministic engine</span>
            <h2>Switching scenario</h2>
          </div>
          {calculation ? (
            <span className={`status-pill ${calculationIsCurrent ? "success" : "warning"}`}>
              {calculationIsCurrent ? "Reproducible" : "Stale · rerun required"}
            </span>
          ) : null}
        </div>
        <form className={styles.calculationAction} onSubmit={calculate}>
          <div className="field">
            <label htmlFor="as-of-date">Calculate remaining obligations as of</label>
            <input
              id="as-of-date"
              className="input"
              type="date"
              value={asOfDate}
              onChange={(event) => setAsOfDate(event.target.value)}
              required
              disabled={!canEdit}
            />
          </div>
          <button
            type="submit"
            className="button"
            disabled={calculating || !activeFees.length || !canEdit}
          >
            {calculating
              ? "Calculating…"
              : calculationIsCurrent
                ? "Recalculate scenario"
                : calculation
                  ? "Rerun calculation"
                  : "Run calculation"}
          </button>
        </form>
        <p className={styles.calculationBoundary}>
          AI is not used here. Reviewed source facts and explicitly labelled human assumptions are
          evaluated by versioned, deterministic formulas; currencies remain separate and unclear
          proration becomes a blocker.
        </p>
        {!canEdit && !evidenceMutable ? (
          <div className="alert warning">
            {caseStatus === "archived"
              ? "Archived cases are permanently read-only and have no further transitions."
              : "Obligation inputs and calculations are locked at this workflow state. Reopen evidence review to create a new revision before changing them."}
          </div>
        ) : null}
        {calculation && !calculationIsCurrent ? (
          <div className="alert warning" role="status">
            <strong>Rerun required.</strong> Fee inputs changed after this result was created. The
            values below are historical and cannot satisfy readiness until recalculated.
          </div>
        ) : null}
        {calculationError ? (
          <div className="alert danger" role="alert">
            {calculationError}
          </div>
        ) : null}
        {calculation ? (
          <div className={styles.calculationResults}>
            <div className={styles.resultMeta}>
              <span>As of {formatDate(calculation.as_of_date)}</span>
              <span>Engine {calculation.engine_version}</span>
              <span className="mono">result {calculation.result_hash.slice(0, 12)}…</span>
            </div>
            <div className={styles.scenarioCards}>
              {calculation.currencies.map((item) => (
                <article key={item.currency}>
                  <span>{item.currency} documented remainder</span>
                  <strong>
                    {formatMinor(item.documented_remaining_subscription_minor, item.currency)}
                  </strong>
                  <dl>
                    <div>
                      <dt>Scenario range</dt>
                      <dd>
                        {formatMinor(item.potential_coverage_min_minor, item.currency)} –{" "}
                        {formatMinor(item.potential_coverage_max_minor, item.currency)}
                      </dd>
                    </div>
                    <div>
                      <dt>Excluded</dt>
                      <dd>{formatMinor(item.excluded_non_subscription_minor, item.currency)}</dd>
                    </div>
                    <div>
                      <dt>Unclassified</dt>
                      <dd>{formatMinor(item.unclassified_minor, item.currency)}</dd>
                    </div>
                  </dl>
                </article>
              ))}
            </div>
            {calculation.blocking_findings.length ? (
              <div className="alert danger">
                <strong>Calculation has {calculation.blocking_findings.length} blocker(s).</strong>
                <ul>
                  {calculation.blocking_findings.map((finding) => (
                    <li key={finding}>{finding}</li>
                  ))}
                </ul>
              </div>
            ) : (
              <div className="alert success">No deterministic calculation blockers remain.</div>
            )}
            <div
              className={styles.tableScroller}
              role="region"
              aria-label="Calculation line items"
              tabIndex={0}
            >
              <table className={styles.dataTable}>
                <thead>
                  <tr>
                    <th>Line</th>
                    <th>Treatment</th>
                    <th>Original</th>
                    <th>Remaining</th>
                    <th>Formula</th>
                  </tr>
                </thead>
                <tbody>
                  {calculation.line_items.map((line) => (
                    <tr key={line.identifier}>
                      <td>
                        <strong>{line.identifier}</strong>
                        <small className={styles.cellDetail}>{line.explanation}</small>
                      </td>
                      <td>{humanize(line.treatment)}</td>
                      <td className={styles.numeric}>
                        {formatMinor(line.original_amount_minor, line.currency)}
                      </td>
                      <td className={styles.numeric}>
                        {line.remaining_amount_minor === null
                          ? "Blocked"
                          : formatMinor(line.remaining_amount_minor, line.currency)}
                      </td>
                      <td className="mono">{line.formula_identifier}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {calculation.assumptions.length ? (
              <details className={styles.assumptionList}>
                <summary>{calculation.assumptions.length} recorded assumption(s)</summary>
                <ul>
                  {calculation.assumptions.map((assumption) => (
                    <li key={assumption}>{assumption}</li>
                  ))}
                </ul>
              </details>
            ) : null}
          </div>
        ) : (
          <div className={styles.emptyCalculation}>
            <strong>No calculation has been run.</strong>
            <span>Add and review at least one subscription obligation, then choose an as-of date.</span>
          </div>
        )}
      </section>
    </div>
  );
}
