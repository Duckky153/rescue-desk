import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkbenchReadiness } from "@/components/workbench-readiness";
import {
  resolveFinding,
  transitionCase,
  type ExportArtifact,
} from "@/components/workbench-api";
import { DEMO_COMPLETED_CASE_NAME } from "@/lib/demo";
import type { Calculation, CaseDetail, DocumentSummary, Readiness } from "@/types/api";

vi.mock("@/components/auth-provider", () => ({
  useAuth: () => ({
    user: {
      id: "approver-1",
      email: "approver@example.test",
      display_name: "Approver",
      organization_id: "org-1",
      role: "approver",
    },
  }),
}));
vi.mock("@/components/workbench-api", async () => {
  const actual = await vi.importActual<typeof import("@/components/workbench-api")>(
    "@/components/workbench-api",
  );
  return { ...actual, resolveFinding: vi.fn(), transitionCase: vi.fn() };
});

const caseDetail: CaseDetail = {
  id: "case-1",
  organization_id: "org-1",
  display_name: "Synthetic review",
  applicant_company: "Northstar",
  erp_provider: "LegacySuite",
  status: "evidence_review",
  current_revision_number: 1,
  version: 5,
  assigned_analyst_id: "analyst-1",
  assigned_approver_id: "approver-1",
  revisions: [],
  created_at: "2026-08-21T00:00:00Z",
  updated_at: "2026-08-21T00:00:00Z",
};

const finding: Readiness["findings"][number] = {
  id: "finding-1",
  code: "document.needs_ocr",
  title: "OCR review required",
  detail: "The source has no machine-readable text.",
  severity: "blocking",
  status: "open",
  version: 3,
  resolution_reason: null,
  created_at: "2026-08-21T00:00:00Z",
  resolved_at: null,
};

const readiness: Readiness = {
  ready_for_internal_review: false,
  mandatory_assertions_reviewed: false,
  reproducible_calculation_exists: false,
  open_blocking_findings: 1,
  findings: [finding],
  disclaimer: "Synthetic demonstration only.",
};

const calculation: Calculation = {
  id: "calculation-1",
  case_revision_id: "revision-1",
  as_of_date: "2026-08-21",
  engine_version: "remaining-subscription-v2",
  currencies: [],
  line_items: [],
  blocking_findings: [],
  assumptions: [],
  input_hash: "c".repeat(64),
  result_hash: "d".repeat(64),
  created_at: "2026-08-21T00:00:00Z",
};

const processedDocument: DocumentSummary = {
  id: "document-1",
  case_revision_id: "revision-1",
  original_filename: "synthetic.pdf",
  sha256: "e".repeat(64),
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

describe("readiness finding disposition", () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(cleanup);

  it("records an explicit optimistic-versioned accepted-risk decision", async () => {
    vi.mocked(resolveFinding).mockResolvedValue({
      ...finding,
      status: "accepted_risk",
      resolution_reason: "A human compared the scanned original.",
    });
    const onRefresh = vi.fn().mockResolvedValue(undefined);
    render(
      <WorkbenchReadiness
        caseDetail={caseDetail}
        readiness={readiness}
        calculation={null}
        exports={[]}
        onRefresh={onRefresh}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /record disposition/i }));
    fireEvent.change(screen.getByLabelText(/^disposition$/i), {
      target: { value: "accepted_risk" },
    });
    fireEvent.change(screen.getByLabelText(/disposition reason/i), {
      target: { value: "A human compared the scanned original." },
    });
    fireEvent.click(screen.getAllByRole("button", { name: /record disposition/i }).at(-1)!);

    await waitFor(() =>
      expect(resolveFinding).toHaveBeenCalledWith(
        "case-1",
        finding,
        "accepted_risk",
        "A human compared the scanned original.",
      ),
    );
    expect(onRefresh).toHaveBeenCalledOnce();
    expect(screen.getByText("Blocking · Open")).toBeVisible();
  });

  it("resets disposition and reason between sequential finding decisions", async () => {
    const secondFinding = {
      ...finding,
      id: "finding-2",
      code: "document.redacted_value",
      title: "Redacted value requires review",
      version: 1,
    };
    vi.mocked(resolveFinding).mockResolvedValue({ ...secondFinding, status: "accepted_risk" });
    render(
      <WorkbenchReadiness
        caseDetail={caseDetail}
        readiness={{ ...readiness, open_blocking_findings: 2, findings: [finding, secondFinding] }}
        calculation={null}
        exports={[]}
        onRefresh={vi.fn().mockResolvedValue(undefined)}
      />,
    );

    fireEvent.click(screen.getAllByRole("button", { name: /record disposition/i })[0]);
    fireEvent.change(screen.getByLabelText(/^disposition$/i), {
      target: { value: "accepted_risk" },
    });
    fireEvent.change(screen.getByLabelText(/disposition reason/i), {
      target: { value: "This must not leak into another finding." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    fireEvent.click(screen.getAllByRole("button", { name: /record disposition/i })[1]);
    expect(screen.getByLabelText(/^disposition$/i)).toHaveValue("resolved");
    expect(screen.getByLabelText(/disposition reason/i)).toHaveValue("");

    fireEvent.change(screen.getByLabelText(/^disposition$/i), {
      target: { value: "accepted_risk" },
    });
    fireEvent.change(screen.getByLabelText(/disposition reason/i), {
      target: { value: "The second finding has its own reviewed reason." },
    });
    fireEvent.click(screen.getAllByRole("button", { name: /record disposition/i }).at(-1)!);
    await waitFor(() => expect(resolveFinding).toHaveBeenCalledOnce());

    fireEvent.click(screen.getAllByRole("button", { name: /record disposition/i })[0]);
    expect(screen.getByLabelText(/^disposition$/i)).toHaveValue("resolved");
    expect(screen.getByLabelText(/disposition reason/i)).toHaveValue("");
  });

  it("requires replacing a pre-approval artifact before the exported transition is enabled", () => {
    const artifact: ExportArtifact = {
      id: "export-1",
      case_revision_id: "revision-1",
      calculation_id: "calculation-1",
      kind: "internal_review_pdf",
      sha256: "a".repeat(64),
      packet_snapshot_sha256: "b".repeat(64),
      approval_status: "not_approved",
      generator_version: "1.0.0",
      superseded: false,
      content_type: "application/pdf",
      filename: "internal-review.pdf",
      size_bytes: 1_024,
      download_url: "/download",
      disclaimer: "Synthetic demonstration only.",
      created_at: "2026-08-21T00:00:00Z",
    };
    const approvedCase = {
      ...caseDetail,
      status: "internal_packet_approved" as const,
    };

    render(
      <WorkbenchReadiness
        caseDetail={approvedCase}
        readiness={{
          ...readiness,
          findings: [],
          reproducible_calculation_exists: true,
        }}
        calculation={calculation}
        exports={[artifact]}
        onRefresh={vi.fn()}
      />,
    );

    expect(screen.getByText("Not approved")).toBeVisible();
    expect(screen.getByRole("button", { name: /regenerate approved/i })).toBeEnabled();
    expect(screen.getByRole("button", { name: /mark packet exported/i })).toBeDisabled();
  });

  it("labels superseded exports as historical and never presents them as current approval", () => {
    const retiredArtifact: ExportArtifact = {
      id: "export-retired",
      case_revision_id: "revision-1",
      calculation_id: "calculation-old",
      kind: "internal_review_pdf",
      sha256: "a".repeat(64),
      packet_snapshot_sha256: "b".repeat(64),
      approval_status: "approved",
      generator_version: "1.0.0",
      superseded: true,
      content_type: "application/pdf",
      filename: "historical-internal-review.pdf",
      size_bytes: 1_024,
      download_url: "/download",
      disclaimer: "Synthetic demonstration only.",
      created_at: "2026-08-21T00:00:00Z",
    };

    render(
      <WorkbenchReadiness
        caseDetail={{ ...caseDetail, status: "internal_packet_approved" }}
        readiness={{ ...readiness, findings: [] }}
        calculation={null}
        exports={[retiredArtifact]}
        onRefresh={vi.fn()}
      />,
    );

    expect(screen.getByText(/1 prior artifact is retired/i)).toBeVisible();
    expect(screen.queryByText("Approver-recorded snapshot")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Download" })).not.toBeInTheDocument();
    expect(screen.getByText(/a current reproducible calculation is required/i)).toBeVisible();
    for (const button of screen.getAllByRole("button", { name: "Generate" })) {
      expect(button).toBeDisabled();
    }
    expect(screen.getByRole("button", { name: /mark packet exported/i })).toBeDisabled();
  });

  it("labels the programmatically completed demo as seeded approver-role history", () => {
    const artifact: ExportArtifact = {
      id: "export-seeded",
      case_revision_id: "revision-1",
      calculation_id: "calculation-1",
      kind: "internal_review_pdf",
      sha256: "a".repeat(64),
      packet_snapshot_sha256: "b".repeat(64),
      approval_status: "approved",
      generator_version: "1.0.0",
      superseded: false,
      content_type: "application/pdf",
      filename: "seeded-internal-review.pdf",
      size_bytes: 1_024,
      download_url: "/download",
      disclaimer: "Synthetic demonstration only.",
      created_at: "2026-08-21T00:00:00Z",
    };

    render(
      <WorkbenchReadiness
        caseDetail={{
          ...caseDetail,
          display_name: DEMO_COMPLETED_CASE_NAME,
          status: "exported",
        }}
        readiness={{
          ...readiness,
          findings: [],
          open_blocking_findings: 0,
          reproducible_calculation_exists: true,
        }}
        calculation={calculation}
        exports={[artifact]}
        onRefresh={vi.fn()}
      />,
    );

    expect(screen.getByText("Seeded synthetic approver-role snapshot")).toBeVisible();
    expect(screen.queryByText("Approver-recorded snapshot")).not.toBeInTheDocument();
  });

  it("does not offer an executable evidence-review transition before a contract exists", () => {
    render(
      <WorkbenchReadiness
        caseDetail={{ ...caseDetail, status: "draft" }}
        readiness={{
          ...readiness,
          findings: [
            {
              ...finding,
              id: "computed:document.missing",
              code: "document.missing",
              title: "Contract missing",
            },
          ],
        }}
        calculation={null}
        exports={[]}
        onRefresh={vi.fn()}
      />,
    );

    expect(screen.getByText(/requires a processed contract document/i)).toBeVisible();
    expect(screen.getByRole("button", { name: /begin evidence review/i })).toBeDisabled();
  });

  it("blocks draft handoff when the only active document has not processed", () => {
    render(
      <WorkbenchReadiness
        caseDetail={{ ...caseDetail, status: "draft" }}
        readiness={{ ...readiness, findings: [] }}
        calculation={null}
        exports={[]}
        documents={[{ ...processedDocument, processing_status: "failed" }]}
        onRefresh={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: /begin evidence review/i })).toBeDisabled();
  });

  it("shows explicit assumptions instead of calling the completed review source-backed", () => {
    render(
      <WorkbenchReadiness
        caseDetail={caseDetail}
        readiness={{ ...readiness, mandatory_assertions_reviewed: true }}
        calculation={null}
        exports={[]}
        hasExplicitAssumptions
        onRefresh={vi.fn()}
      />,
    );

    expect(screen.getByText(/reviewed; explicit assumptions remain labelled/i)).toBeVisible();
    expect(screen.queryByText(/reviewed and evidence-backed/i)).not.toBeInTheDocument();
  });

  it("does not enable approval for a different assigned approver", () => {
    render(
      <WorkbenchReadiness
        caseDetail={{
          ...caseDetail,
          status: "ready_for_internal_review",
          assigned_approver_id: "approver-2",
        }}
        readiness={{
          ...readiness,
          mandatory_assertions_reviewed: true,
          reproducible_calculation_exists: true,
          open_blocking_findings: 0,
          findings: [],
          ready_for_internal_review: true,
        }}
        calculation={calculation}
        exports={[]}
        onRefresh={vi.fn()}
      />,
    );

    expect(screen.getByText(/assigned to another approver/i)).toBeVisible();
    expect(screen.getByRole("button", { name: /approve internal packet/i })).toBeDisabled();
  });

  it("returns a ready packet to evidence review without requiring a false approval", async () => {
    vi.mocked(transitionCase).mockResolvedValue({
      ...caseDetail,
      status: "evidence_review",
      version: 6,
    });
    const onRefresh = vi.fn().mockResolvedValue(undefined);
    render(
      <WorkbenchReadiness
        caseDetail={{ ...caseDetail, status: "ready_for_internal_review" }}
        readiness={{ ...readiness, findings: [] }}
        calculation={calculation}
        exports={[]}
        onRefresh={onRefresh}
      />,
    );

    fireEvent.change(screen.getByLabelText("Return reason"), {
      target: { value: "A cited obligation must be corrected before approval." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Return to evidence review" }));

    await waitFor(() =>
      expect(transitionCase).toHaveBeenCalledWith(
        "case-1",
        "evidence_review",
        5,
        "A cited obligation must be corrected before approval.",
      ),
    );
    expect(onRefresh).toHaveBeenCalledOnce();
  });

  it("distinguishes a stale calculation from a missing calculation", () => {
    render(
      <WorkbenchReadiness
        caseDetail={caseDetail}
        readiness={{
          ...readiness,
          findings: [
            {
              ...finding,
              id: "computed:calculation.stale",
              code: "calculation.stale",
              title: "Calculation is stale",
              detail: "Fee inputs changed after the latest calculation.",
            },
          ],
        }}
        calculation={null}
        exports={[]}
        onRefresh={vi.fn()}
      />,
    );

    expect(screen.getByText(/latest result is stale · rerun required/i)).toBeVisible();
    expect(screen.queryByText("No reproducible result")).not.toBeInTheDocument();
  });
});
