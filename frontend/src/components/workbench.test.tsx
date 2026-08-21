import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Workbench } from "@/components/workbench";
import {
  addFee,
  getWorkbenchSnapshot,
  uploadAndProcessContract,
  type WorkbenchSnapshot,
} from "@/components/workbench-api";
import type { Fee } from "@/types/api";

vi.mock("next/dynamic", () => ({
  default: () => () => <div data-testid="pdf-viewer-stub" />,
}));

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
  return {
    ...actual,
    addFee: vi.fn(),
    getWorkbenchSnapshot: vi.fn(),
    uploadAndProcessContract: vi.fn(),
  };
});

const fee: Fee = {
  id: "fee-1",
  case_revision_id: "revision-1",
  category: "subscription",
  amount_minor: 999,
  currency: "USD",
  service_start: null,
  service_end: null,
  obligation_date: null,
  payment_status: "unknown",
  billing_cadence: null,
  proration_rule: null,
  assertion_ids: [],
  reviewed: false,
  primary_money_assertion_id: null,
  superseded: false,
  superseded_at: null,
  superseded_by: null,
  supersede_reason: null,
  created_at: "2026-08-21T00:00:00Z",
};

const snapshot: WorkbenchSnapshot = {
  caseDetail: {
    id: "case-1",
    organization_id: "org-1",
    display_name: "Refresh recovery matter",
    applicant_company: "Synthetic Company",
    erp_provider: "Synthetic ERP",
    status: "evidence_review",
    current_revision_number: 1,
    version: 1,
    assigned_analyst_id: "analyst-1",
    assigned_approver_id: "approver-1",
    revisions: [],
    created_at: "2026-08-21T00:00:00Z",
    updated_at: "2026-08-21T00:00:00Z",
  },
  documents: [],
  assertions: [],
  fees: [],
  calculation: null,
  readiness: {
    ready_for_internal_review: false,
    mandatory_assertions_reviewed: false,
    reproducible_calculation_exists: false,
    open_blocking_findings: 0,
    findings: [],
    disclaimer: "Synthetic demonstration only.",
  },
  auditEvents: [],
  exports: [],
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

describe("workbench snapshot recovery", () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(cleanup);

  it("locks stale controls after a committed mutation loses its refresh response", async () => {
    vi.mocked(getWorkbenchSnapshot)
      .mockResolvedValueOnce(snapshot)
      .mockRejectedValueOnce(new Error("Snapshot temporarily unavailable"))
      .mockResolvedValueOnce({ ...snapshot, fees: [fee], caseDetail: { ...snapshot.caseDetail, version: 2 } });
    vi.mocked(addFee).mockResolvedValue(fee);

    render(<Workbench caseId="case-1" />);
    await screen.findByRole("heading", { name: "Refresh recovery matter" });
    fireEvent.click(screen.getByRole("button", { name: /^Economics/ }));
    fireEvent.change(screen.getByLabelText("Active input amount"), { target: { value: "9.99" } });
    fireEvent.click(screen.getByRole("button", { name: "Add obligation" }));

    await waitFor(() => expect(addFee).toHaveBeenCalledOnce());
    expect(await screen.findByText(/change was saved.*could not refresh/i)).toBeVisible();
    expect(screen.getByText(/obligation was saved.*could not refresh/i)).toBeVisible();
    expect(screen.getByRole("button", { name: "Add obligation" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Add obligation" }));
    expect(addFee).toHaveBeenCalledOnce();

    fireEvent.click(screen.getByRole("button", { name: "Reload fresh snapshot" }));
    await waitFor(() => expect(getWorkbenchSnapshot).toHaveBeenCalledTimes(3));
    expect(screen.getByRole("button", { name: "Add obligation" })).toBeEnabled();
    expect(screen.getByRole("region", { name: "Obligation ledger" })).toHaveTextContent("$9.99");
    expect(addFee).toHaveBeenCalledOnce();
  });

  it("ignores an older pre-commit refresh that resolves after the mutation snapshot", async () => {
    const preCommitRefresh = deferred<WorkbenchSnapshot>();
    const feeCommit = deferred<Fee>();
    const freshSnapshot = {
      ...snapshot,
      fees: [fee],
      caseDetail: { ...snapshot.caseDetail, version: 2 },
    };
    vi.mocked(getWorkbenchSnapshot)
      .mockResolvedValueOnce(snapshot)
      .mockReturnValueOnce(preCommitRefresh.promise)
      .mockResolvedValueOnce(freshSnapshot);
    vi.mocked(addFee).mockReturnValue(feeCommit.promise);

    render(<Workbench caseId="case-1" />);
    await screen.findByRole("heading", { name: "Refresh recovery matter" });
    fireEvent.click(screen.getByRole("button", { name: /^Economics/ }));
    fireEvent.change(screen.getByLabelText("Active input amount"), { target: { value: "9.99" } });
    fireEvent.click(screen.getByRole("button", { name: "Add obligation" }));
    await waitFor(() => expect(addFee).toHaveBeenCalledOnce());

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(getWorkbenchSnapshot).toHaveBeenCalledTimes(2));
    feeCommit.resolve(fee);
    await waitFor(() => expect(getWorkbenchSnapshot).toHaveBeenCalledTimes(3));
    await waitFor(() =>
      expect(screen.getByRole("region", { name: "Obligation ledger" })).toHaveTextContent("$9.99"),
    );

    preCommitRefresh.resolve(snapshot);
    await waitFor(() =>
      expect(screen.getByRole("region", { name: "Obligation ledger" })).toHaveTextContent("$9.99"),
    );
    expect(screen.getByRole("button", { name: "Add obligation" })).toBeEnabled();
    expect(addFee).toHaveBeenCalledOnce();
  });

  it("uses neutral recovery language when a rejected upload and recovery refresh both fail", async () => {
    vi.mocked(getWorkbenchSnapshot)
      .mockResolvedValueOnce(snapshot)
      .mockRejectedValueOnce(new Error("Snapshot temporarily unavailable"));
    vi.mocked(uploadAndProcessContract).mockRejectedValue(
      new Error("The PDF failed safety validation and was rejected"),
    );

    render(<Workbench caseId="case-1" />);
    await screen.findByRole("heading", { name: "Refresh recovery matter" });
    fireEvent.change(screen.getByLabelText("Document source"), {
      target: { value: "synthetic" },
    });
    const file = new File(["%PDF-1.7 unsafe synthetic"], "rejected.pdf", {
      type: "application/pdf",
    });
    fireEvent.change(screen.getByLabelText("Contract PDF file"), {
      target: { files: [file] },
    });

    await waitFor(() =>
      expect(uploadAndProcessContract).toHaveBeenCalledWith("case-1", file, "synthetic"),
    );
    expect(await screen.findByText(/recovery refresh could not load a fresh workspace/i)).toBeVisible();
    expect(screen.getByText(/PDF failed safety validation and was rejected/i)).toBeVisible();
    expect(screen.queryByText(/change was saved/i)).not.toBeInTheDocument();
    expect(getWorkbenchSnapshot).toHaveBeenCalledTimes(2);
  });
});
