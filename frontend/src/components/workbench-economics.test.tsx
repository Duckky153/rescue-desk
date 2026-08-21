import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FeeEntryForm, WorkbenchEconomics } from "@/components/workbench-economics";
import {
  addFee,
  supersedeFee,
  type WorkbenchAssertion,
} from "@/components/workbench-api";
import type { Calculation, DocumentSummary, Fee } from "@/types/api";

vi.mock("@/components/auth-provider", () => ({
  useAuth: () => ({
    user: {
      id: "analyst-1",
      email: "analyst@example.test",
      display_name: "Analyst",
      organization_id: "org-1",
      role: "analyst",
    },
  }),
}));

vi.mock("@/components/workbench-api", async () => {
  const actual = await vi.importActual<typeof import("@/components/workbench-api")>(
    "@/components/workbench-api",
  );
  return { ...actual, addFee: vi.fn(), supersedeFee: vi.fn() };
});

function feeFixture(overrides: Partial<Fee> = {}): Fee {
  return {
    id: "fee-1",
    case_revision_id: "revision-1",
    category: "subscription",
    amount_minor: 1_200_045,
    currency: "USD",
    service_start: "2026-01-01",
    service_end: "2027-01-01",
    obligation_date: null,
    payment_status: "unpaid",
    billing_cadence: "annual",
    proration_rule: "contract_daily",
    assertion_ids: ["assertion-1"],
    reviewed: true,
    primary_money_assertion_id: "assertion-1",
    superseded: false,
    superseded_at: null,
    superseded_by: null,
    supersede_reason: null,
    created_at: "2026-08-21T00:00:00Z",
    ...overrides,
  };
}

function reviewedAssertionFixture(): WorkbenchAssertion {
  return {
    id: "assertion-1",
    case_revision_id: "revision-1",
    semantic_key: "fee.subscription",
    raw_value: "USD 12,000.45",
    normalized_value: {
      type: "money",
      amount_minor: 1_200_045,
      currency: "USD",
      cadence: "annual",
    },
    display_value: "USD 12,000.45",
    source: "deterministic",
    confidence: "0.99",
    review_state: "accepted",
    version: 2,
    is_current: true,
    created_at: "2026-08-21T00:00:00Z",
    assumption: false,
    review_reason: "Matched the cited subscription clause.",
    reviewed_by: "Analyst",
    evidence_basis: "source_evidence",
    evidence: [
      {
        id: "evidence-1",
        document_id: "document-active",
        page_id: "page-1",
        page_number: 1,
        quote: "Annual subscription fee: USD 12,000.45",
        char_start: 0,
        char_end: 39,
        quote_sha256: "a".repeat(64),
      },
    ],
  };
}

const activeDocument: DocumentSummary = {
  id: "document-active",
  case_revision_id: "revision-1",
  original_filename: "active.pdf",
  sha256: "f".repeat(64),
  media_type: "application/pdf",
  source_type: "synthetic",
  size_bytes: 1_024,
  page_count: 1,
  safety_status: "passed",
  processing_status: "processed",
  processing_error: null,
  superseded: false,
  created_at: "2026-08-21T00:00:00Z",
};

const supersededDocument: DocumentSummary = {
  ...activeDocument,
  id: "document-superseded",
  original_filename: "historical.pdf",
  superseded: true,
};

describe("economics fee entry", () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(cleanup);

  it("submits an exact integer-cent reviewed obligation", async () => {
    const assertions = [
      {
        id: "assertion-1",
        case_revision_id: "revision-1",
        semantic_key: "fee.subscription",
        raw_value: "USD 12,000.45",
        normalized_value: {
          type: "money",
          amount_minor: 1_200_045,
          currency: "USD",
          cadence: "annual",
        },
        display_value: "USD 12,000.45",
        source: "deterministic" as const,
        confidence: "0.99",
        review_state: "accepted" as const,
        version: 2,
        is_current: true,
        created_at: "2026-08-21T00:00:00Z",
        assumption: false,
        review_reason: "Matched the cited subscription clause.",
        reviewed_by: "Analyst",
        evidence_basis: "source_evidence" as const,
        evidence: [
          {
            id: "evidence-1",
            document_id: activeDocument.id,
            page_id: "page-1",
            page_number: 1,
            quote: "Annual subscription fee: USD 12,000.45",
            char_start: 0,
            char_end: 39,
            quote_sha256: "a".repeat(64),
          },
        ],
      },
    ];
    vi.mocked(addFee).mockResolvedValue(feeFixture());
    const onAdded = vi.fn().mockResolvedValue(undefined);
    render(
      <FeeEntryForm
        caseId="case-1"
        assertions={assertions}
        documents={[activeDocument]}
        onAdded={onAdded}
      />,
    );

    fireEvent.change(screen.getByLabelText(/active input amount/i), {
      target: { value: "12,000.45" },
    });
    fireEvent.change(screen.getByLabelText(/service start/i), {
      target: { value: "2026-01-01" },
    });
    fireEvent.change(screen.getByLabelText(/service end/i), {
      target: { value: "2027-01-01" },
    });
    fireEvent.change(screen.getByLabelText(/obligation date/i), {
      target: { value: "2026-02-01" },
    });
    fireEvent.change(screen.getByLabelText(/payment status/i), {
      target: { value: "unpaid" },
    });
    fireEvent.change(screen.getByLabelText(/billing cadence/i), {
      target: { value: "annual" },
    });
    fireEvent.change(screen.getByLabelText(/proration rule/i), {
      target: { value: "contract_daily" },
    });
    fireEvent.change(screen.getByLabelText(/supporting reviewed fact/i), {
      target: { value: "assertion-1" },
    });
    fireEvent.click(screen.getByLabelText(/verified this obligation/i));
    fireEvent.click(screen.getByRole("button", { name: /add obligation/i }));

    await waitFor(() =>
      expect(addFee).toHaveBeenCalledWith("case-1", {
        category: "subscription",
        amount_minor: 1_200_045,
        currency: "USD",
        service_start: "2026-01-01",
        service_end: "2027-01-01",
        obligation_date: "2026-02-01",
        payment_status: "unpaid",
        billing_cadence: "annual",
        proration_rule: "contract_daily",
        assertion_ids: ["assertion-1"],
        reviewed: true,
      }),
    );
    expect(onAdded).toHaveBeenCalledOnce();
    expect(screen.getByLabelText(/payment status/i)).toHaveValue("unknown");
    expect(screen.getByLabelText(/billing cadence/i)).toHaveValue("");
    expect(screen.getByLabelText(/proration rule/i)).toHaveValue("");
    expect(screen.getByLabelText(/service start/i)).toHaveValue("");
    expect(screen.getByLabelText(/service end/i)).toHaveValue("");
    expect(screen.getByLabelText(/obligation date/i)).toHaveValue("");
  });

  it("clears category-specific scenario state when the obligation category changes", () => {
    render(
      <FeeEntryForm
        caseId="case-1"
        assertions={[reviewedAssertionFixture()]}
        documents={[activeDocument]}
        onAdded={vi.fn()}
      />,
    );

    fireEvent.change(screen.getByLabelText(/service start/i), {
      target: { value: "2026-01-01" },
    });
    fireEvent.change(screen.getByLabelText(/service end/i), {
      target: { value: "2027-01-01" },
    });
    fireEvent.change(screen.getByLabelText(/obligation date/i), {
      target: { value: "2026-02-01" },
    });
    fireEvent.change(screen.getByLabelText(/payment status/i), {
      target: { value: "unpaid" },
    });
    fireEvent.change(screen.getByLabelText(/billing cadence/i), {
      target: { value: "annual" },
    });
    fireEvent.change(screen.getByLabelText(/proration rule/i), {
      target: { value: "contract_daily" },
    });
    fireEvent.change(screen.getByLabelText(/supporting reviewed fact/i), {
      target: { value: "assertion-1" },
    });
    fireEvent.click(screen.getByLabelText(/verified this obligation/i));

    fireEvent.change(screen.getByLabelText(/^category$/i), {
      target: { value: "implementation" },
    });

    expect(screen.getByLabelText(/service start/i)).toHaveValue("");
    expect(screen.getByLabelText(/service end/i)).toHaveValue("");
    expect(screen.getByLabelText(/obligation date/i)).toHaveValue("");
    expect(screen.getByLabelText(/payment status/i)).toHaveValue("unknown");
    expect(screen.getByLabelText(/billing cadence/i)).toHaveValue("");
    expect(screen.getByLabelText(/proration rule/i)).toHaveValue("");
    expect(screen.getByLabelText(/supporting reviewed fact/i)).toHaveValue("");
    expect(screen.getByLabelText(/verified this obligation/i)).not.toBeChecked();
  });

  it("defaults payment status to unknown so unpaid is an explicit scenario input", () => {
    render(<FeeEntryForm caseId="case-1" assertions={[]} onAdded={vi.fn()} />);

    expect(screen.getByLabelText(/payment status/i)).toHaveValue("unknown");
    expect(screen.getByLabelText(/billing cadence/i)).toHaveValue("");
    expect(screen.getByLabelText(/proration rule/i)).toHaveValue("");
  });

  it("rejects fractional cents before making an API request", async () => {
    render(<FeeEntryForm caseId="case-1" assertions={[]} onAdded={vi.fn()} />);
    fireEvent.change(screen.getByLabelText(/active input amount/i), {
      target: { value: "10.999" },
    });
    fireEvent.click(screen.getByRole("button", { name: /add obligation/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/2 decimal places/i);
    expect(addFee).not.toHaveBeenCalled();
  });

  it("requires an explicit cadence before a fee can be recorded as reviewed", async () => {
    render(
      <FeeEntryForm
        caseId="case-1"
        assertions={[reviewedAssertionFixture()]}
        documents={[activeDocument]}
        onAdded={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText(/active input amount/i), {
      target: { value: "12000.45" },
    });
    fireEvent.change(screen.getByLabelText(/supporting reviewed fact/i), {
      target: { value: "assertion-1" },
    });
    fireEvent.click(screen.getByLabelText(/verified this obligation/i));
    fireEvent.click(screen.getByRole("button", { name: /add obligation/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/billing cadence/i);
    expect(addFee).not.toHaveBeenCalled();
  });

  it("offers only category-compatible money facts as reviewed evidence", () => {
    const money = reviewedAssertionFixture();
    const date = {
      ...reviewedAssertionFixture(),
      id: "date-assertion",
      semantic_key: "contract_end_date",
      normalized_value: { type: "date", value: "2027-01-01" },
      display_value: "2027-01-01",
    };
    render(
      <FeeEntryForm
        caseId="case-1"
        assertions={[money, date]}
        documents={[activeDocument]}
        onAdded={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("option", { name: /fee · subscription — usd 12,000\.45/i }),
    ).toBeVisible();
    expect(screen.queryByRole("option", { name: /contract end date/i })).not.toBeInTheDocument();
  });

  it("labels explicit-assumption money facts before they are selected", () => {
    const assumption = {
      ...reviewedAssertionFixture(),
      source: "human" as const,
      review_state: "corrected" as const,
      assumption: true,
      evidence_basis: "explicit_assumption" as const,
    };
    render(
      <FeeEntryForm
        caseId="case-1"
        assertions={[assumption]}
        documents={[activeDocument]}
        onAdded={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("option", { name: /fee · subscription.*explicit assumption/i }),
    ).toBeVisible();
    expect(
      screen.queryByRole("option", { name: /fee · subscription.*source-confirmed/i }),
    ).not.toBeInTheDocument();
  });

  it("excludes reviewed money facts whose only source document is superseded", () => {
    const historical = reviewedAssertionFixture();
    historical.evidence = [
      { ...historical.evidence[0], document_id: supersededDocument.id },
    ];
    render(
      <FeeEntryForm
        caseId="case-1"
        assertions={[historical]}
        documents={[supersededDocument]}
        onAdded={vi.fn()}
      />,
    );

    expect(screen.queryByRole("option", { name: /fee · subscription/i })).not.toBeInTheDocument();
  });

  it("admits only usable typed money facts with active evidence, including assumptions", () => {
    const invalid = {
      ...reviewedAssertionFixture(),
      id: "invalid",
      evidence_basis: "invalid_review" as const,
    };
    const noCitation = {
      ...reviewedAssertionFixture(),
      id: "no-citation",
      evidence: [],
    };
    const unsupportedAssumption = {
      ...reviewedAssertionFixture(),
      id: "unsupported-assumption",
      source: "human" as const,
      review_state: "corrected" as const,
      assumption: true,
      evidence_basis: "explicit_assumption" as const,
      evidence: [],
    };
    const malformedAssumption = {
      ...reviewedAssertionFixture(),
      id: "malformed-assumption",
      source: "human" as const,
      review_state: "corrected" as const,
      assumption: true,
      evidence_basis: "explicit_assumption" as const,
      normalized_value: { type: "money", amount_minor: 1_200_045, currency: "USD" },
    };
    const evidencedAssumption = {
      ...reviewedAssertionFixture(),
      id: "evidenced-assumption",
      source: "human" as const,
      review_state: "corrected" as const,
      assumption: true,
      evidence_basis: "explicit_assumption" as const,
    };
    render(
      <FeeEntryForm
        caseId="case-1"
        assertions={[invalid, noCitation, unsupportedAssumption, malformedAssumption, evidencedAssumption]}
        documents={[activeDocument]}
        onAdded={vi.fn()}
      />,
    );

    expect(screen.queryByRole("option", { name: /invalid review/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /source-confirmed/i })).not.toBeInTheDocument();
    expect(screen.getAllByRole("option", { name: /explicit assumption/i })).toHaveLength(1);
    expect(screen.getByRole("option", { name: /explicit assumption/i })).toHaveValue(
      "evidenced-assumption",
    );
  });

  it("shows payment, proration, and assumption provenance in the obligation ledger", () => {
    const assumption = {
      ...reviewedAssertionFixture(),
      source: "human" as const,
      review_state: "corrected" as const,
      assumption: true,
      evidence_basis: "explicit_assumption" as const,
    };
    render(
      <WorkbenchEconomics
        caseId="case-1"
        caseStatus="evidence_review"
        caseVersion={4}
        fees={[feeFixture()]}
        assertions={[assumption]}
        calculation={null}
        calculationIsCurrent={false}
        onRefresh={vi.fn()}
      />,
    );

    const ledger = screen.getByRole("region", { name: "Obligation ledger" });
    expect(ledger).toHaveTextContent("Unpaid");
    expect(ledger).toHaveTextContent("Contract daily");
    expect(ledger).toHaveTextContent("Explicit assumption");
    expect(ledger).toHaveTextContent("USD 12,000.45");
  });

  it("does not present a fee as reviewed when its primary money assertion is no longer current", () => {
    const staleAssertion = { ...reviewedAssertionFixture(), is_current: false };
    render(
      <WorkbenchEconomics
        caseId="case-1"
        caseStatus="evidence_review"
        caseVersion={4}
        fees={[feeFixture()]}
        assertions={[staleAssertion]}
        calculation={null}
        calculationIsCurrent={false}
        onRefresh={vi.fn()}
      />,
    );

    const ledger = screen.getByRole("region", { name: "Obligation ledger" });
    expect(ledger).toHaveTextContent("Evidence changed");
    expect(ledger).toHaveTextContent(/supersede and re-enter/i);
    expect(ledger).not.toHaveTextContent("Source-confirmed");
    expect(ledger).not.toHaveTextContent("Money evidence reviewed");
  });

  it("disables every fee mutation input when the revision is locked", () => {
    render(
      <FeeEntryForm
        caseId="case-1"
        assertions={[]}
        onAdded={vi.fn()}
        canEdit={false}
        unavailableMessage="Obligation inputs are locked at this workflow state."
      />,
    );

    expect(screen.getByText(/obligation inputs are locked/i)).toBeVisible();
    expect(screen.getByLabelText(/active input amount/i)).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "Currency" })).toBeDisabled();
    expect(screen.getByLabelText(/service start/i)).toBeDisabled();
    expect(screen.getByLabelText(/verified this obligation/i)).toBeDisabled();
    expect(screen.getByRole("button", { name: /add obligation/i })).toBeDisabled();
  });

  it("labels an input-mismatched calculation as historical and requires a rerun", () => {
    const staleCalculation: Calculation = {
      id: "calculation-1",
      case_revision_id: "revision-1",
      as_of_date: "2026-08-21",
      engine_version: "remaining-subscription-v2",
      currencies: [],
      line_items: [],
      blocking_findings: [],
      assumptions: [],
      input_hash: "a".repeat(64),
      result_hash: "b".repeat(64),
      created_at: "2026-08-21T00:00:00Z",
    };

    render(
      <WorkbenchEconomics
        caseId="case-1"
        caseStatus="evidence_review"
        caseVersion={4}
        fees={[]}
        assertions={[]}
        calculation={staleCalculation}
        calculationIsCurrent={false}
        onRefresh={vi.fn()}
      />,
    );

    expect(screen.getByText(/stale · rerun required/i)).toBeVisible();
    expect(screen.getByText(/values below are historical/i)).toBeVisible();
    expect(screen.getByRole("button", { name: /rerun calculation/i })).toBeDisabled();
    expect(screen.queryByText("Reproducible")).not.toBeInTheDocument();
  });

  it("soft-supersedes an active input with the current case version and preserves history", async () => {
    const active = feeFixture({ id: "fee-active", amount_minor: 1_000 });
    const historical = feeFixture({
      id: "fee-history",
      amount_minor: 500,
      superseded: true,
      superseded_at: "2026-08-21T01:00:00Z",
      superseded_by: "analyst-1",
      supersede_reason: "Duplicate import preserved for the audit history.",
    });
    vi.mocked(supersedeFee).mockResolvedValue({
      ...active,
      superseded: true,
      superseded_at: "2026-08-21T02:00:00Z",
      superseded_by: "analyst-1",
      supersede_reason: "Entered from the wrong schedule.",
    });
    const onRefresh = vi.fn().mockResolvedValue(undefined);

    render(
      <WorkbenchEconomics
        caseId="case-1"
        caseStatus="evidence_review"
        caseVersion={7}
        fees={[active, historical]}
        assertions={[]}
        calculation={null}
        calculationIsCurrent={false}
        onRefresh={onRefresh}
      />,
    );

    expect(screen.getAllByText("$10.00")).toHaveLength(2);
    expect(screen.queryByText("$15.00")).not.toBeInTheDocument();
    expect(screen.getByText("Duplicate import preserved for the audit history.")).toBeVisible();
    fireEvent.click(
      screen.getByRole("button", { name: /supersede subscription obligation \$10\.00/i }),
    );
    fireEvent.change(screen.getByLabelText("Recovery reason"), {
      target: { value: "Entered from the wrong schedule." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Confirm supersede" }));

    await waitFor(() =>
      expect(supersedeFee).toHaveBeenCalledWith(
        "case-1",
        "fee-active",
        7,
        "Entered from the wrong schedule.",
      ),
    );
    expect(onRefresh).toHaveBeenCalledOnce();
  });

  it("keeps fee recovery visible but locked after evidence-review handoff", () => {
    render(
      <WorkbenchEconomics
        caseId="case-1"
        caseStatus="internal_packet_approved"
        caseVersion={9}
        fees={[feeFixture()]}
        assertions={[]}
        calculation={null}
        calculationIsCurrent={false}
        onRefresh={vi.fn()}
      />,
    );

    expect(screen.getByText("Locked", { exact: true })).toBeVisible();
    expect(screen.queryByRole("button", { name: /supersede subscription obligation/i })).not.toBeInTheDocument();
  });

  it("reserves a corrected money-fact lineage until its active fee is superseded", () => {
    const assertion = reviewedAssertionFixture();
    const activeFee = feeFixture({
      assertion_ids: ["prior-assertion"],
      primary_money_assertion_id: "prior-assertion",
    });
    const props = {
      caseId: "case-1",
      caseStatus: "evidence_review" as const,
      caseVersion: 4,
      assertions: [assertion],
      documents: [activeDocument],
      calculation: null,
      calculationIsCurrent: false,
      onRefresh: vi.fn(),
    };
    const { rerender } = render(<WorkbenchEconomics {...props} fees={[activeFee]} />);

    expect(
      screen.queryByRole("option", { name: /fee · subscription — usd 12,000\.45/i }),
    ).not.toBeInTheDocument();

    rerender(
      <WorkbenchEconomics
        {...props}
        fees={[
          {
            ...activeFee,
            superseded: true,
            superseded_at: "2026-08-21T01:00:00Z",
            superseded_by: "analyst-1",
            supersede_reason: "The fee was entered twice.",
          },
        ]}
      />,
    );

    expect(
      screen.getByRole("option", { name: /fee · subscription — usd 12,000\.45/i }),
    ).toBeVisible();
  });

  it("does not warn that excluded non-subscription categories need subscription inputs", () => {
    render(
      <WorkbenchEconomics
        caseId="case-1"
        caseStatus="evidence_review"
        caseVersion={4}
        fees={[
          feeFixture({
            id: "implementation-fee",
            category: "implementation",
            payment_status: "unknown",
            service_start: null,
            service_end: null,
            proration_rule: null,
          }),
          feeFixture({
            id: "termination-fee",
            category: "termination",
            payment_status: "unknown",
            service_start: null,
            service_end: null,
            proration_rule: null,
          }),
        ]}
        assertions={[]}
        calculation={null}
        calculationIsCurrent={false}
        onRefresh={vi.fn()}
      />,
    );

    expect(screen.queryByText(/scenario input incomplete/i)).not.toBeInTheDocument();
  });

  it("does not require service dates or proration for a paid subscription", () => {
    render(
      <WorkbenchEconomics
        caseId="case-1"
        caseStatus="evidence_review"
        caseVersion={4}
        fees={[
          feeFixture({
            payment_status: "paid",
            service_start: null,
            service_end: null,
            proration_rule: null,
          }),
        ]}
        assertions={[]}
        calculation={null}
        calculationIsCurrent={false}
        onRefresh={vi.fn()}
      />,
    );

    expect(screen.queryByText(/scenario input incomplete/i)).not.toBeInTheDocument();
  });

  it("reports only the missing inputs required for an unpaid subscription", () => {
    render(
      <WorkbenchEconomics
        caseId="case-1"
        caseStatus="evidence_review"
        caseVersion={4}
        fees={[
          feeFixture({
            payment_status: "unpaid",
            service_start: null,
            service_end: null,
            proration_rule: null,
          }),
        ]}
        assertions={[]}
        calculation={null}
        calculationIsCurrent={false}
        onRefresh={vi.fn()}
      />,
    );

    expect(screen.getByText(/resolve service start, service end, proration rule/i)).toBeVisible();
  });

  it("states that archived obligation inputs are permanently read-only", () => {
    render(
      <WorkbenchEconomics
        caseId="case-1"
        caseStatus="archived"
        caseVersion={4}
        fees={[]}
        assertions={[]}
        calculation={null}
        calculationIsCurrent={false}
        onRefresh={vi.fn()}
      />,
    );

    expect(
      screen.getByText(/archived cases are permanently read-only and have no recovery transition/i),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: /add obligation/i })).toBeDisabled();
  });
});
