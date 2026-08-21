import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  AssertionReviewForm,
  filterAssertions,
  WorkbenchEvidence,
} from "@/components/workbench-evidence";
import {
  getDocumentPages,
  processContract,
  reviewAssertion,
  supersedeContract,
  uploadAndProcessContract,
  type WorkbenchAssertion,
} from "@/components/workbench-api";
import type { DocumentSummary } from "@/types/api";

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
    getDocumentPages: vi.fn(),
    processContract: vi.fn(),
    reviewAssertion: vi.fn(),
    supersedeContract: vi.fn(),
    uploadAndProcessContract: vi.fn(),
  };
});

const assertion: WorkbenchAssertion = {
  id: "assertion-1",
  case_revision_id: "revision-1",
  semantic_key: "contract_end_date",
  raw_value: "December 31, 2027",
  normalized_value: { date: "2027-12-31" },
  display_value: "December 31, 2027",
  source: "deterministic",
  confidence: "0.95",
  review_state: "proposed",
  version: 2,
  is_current: true,
  created_at: "2026-08-21T00:00:00Z",
  assumption: false,
  review_reason: null,
  reviewed_by: null,
  evidence_basis: "pending_review",
  evidence: [
    {
      id: "evidence-1",
      document_id: "document-1",
      page_id: "page-4",
      page_number: 4,
      quote: "The initial term expires December 31, 2027.",
      char_start: 10,
      char_end: 55,
      quote_sha256: "a".repeat(64),
    },
  ],
};

function documentFixture(
  overrides: Partial<DocumentSummary> = {},
): DocumentSummary {
  return {
    id: "document-1",
    case_revision_id: "revision-1",
    original_filename: "source.pdf",
    sha256: "a".repeat(64),
    media_type: "application/pdf",
    source_type: "synthetic",
    size_bytes: 1_024,
    page_count: 1,
    safety_status: "passed",
    processing_status: "processed",
    processing_error: null,
    superseded: false,
    created_at: "2026-08-21T00:00:00Z",
    ...overrides,
  };
}

describe("evidence review", () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(cleanup);

  it("filters only current matching assertions", () => {
    const superseded = { ...assertion, id: "old", is_current: false };
    const conflicting = {
      ...assertion,
      id: "conflict",
      semantic_key: "renewal_notice_days",
      review_state: "conflicting" as const,
    };
    expect(filterAssertions([assertion, superseded, conflicting], "renewal", "conflicting")).toEqual([
      conflicting,
    ]);
    expect(filterAssertions([assertion, superseded], "", "all")).toEqual([assertion]);
  });

  it("distinguishes explicit assumptions from source-confirmed facts", () => {
    const explicitAssumption: WorkbenchAssertion = {
      ...assertion,
      source: "human",
      confidence: "1",
      review_state: "corrected",
      assumption: true,
      review_reason: "Used as the scenario date because the source is silent.",
      reviewed_by: "Avery Analyst",
      evidence_basis: "explicit_assumption",
    };
    const sourceConfirmed: WorkbenchAssertion = {
      ...assertion,
      id: "assertion-2",
      semantic_key: "implementation_fee",
      display_value: "USD 5,000.00",
      review_state: "accepted",
      assumption: false,
      review_reason: "Matched the implementation fee to the cited schedule.",
      reviewed_by: "Morgan Reviewer",
      evidence_basis: "source_evidence",
    };

    render(
      <WorkbenchEvidence
        caseId="case-1"
        caseStatus="internal_packet_approved"
        caseVersion={8}
        documents={[]}
        assertions={[explicitAssumption, sourceConfirmed]}
        onRefresh={vi.fn()}
      />,
    );

    const assumptionCard = screen.getByRole("button", {
      name: /contract end date.*explicit assumption/i,
    });
    expect(assumptionCard).toHaveAttribute("data-evidence-basis", "explicit_assumption");
    expect(assumptionCard).toHaveTextContent("Human-supplied · recorded by Avery Analyst");
    expect(assumptionCard).not.toHaveTextContent("Human · 100%");
    expect(
      screen.getByText("This is a human-supplied scenario assumption, not a source-confirmed fact."),
    ).toBeVisible();
    expect(screen.getByText("Used as the scenario date because the source is silent.")).toBeVisible();
    expect(
      screen.getByText("Referenced source context — not proof of this assumption"),
    ).toBeVisible();

    fireEvent.click(
      screen.getByRole("button", { name: /implementation fee.*source-confirmed/i }),
    );
    expect(
      screen.getByText("A reviewer confirmed this value against the attached source evidence."),
    ).toBeVisible();
    expect(screen.getByText("Matched the implementation fee to the cited schedule.")).toBeVisible();
    expect(screen.getByText("Exact source evidence")).toBeVisible();
  });

  it("marks reviewed facts as historical when every cited document is superseded", () => {
    const supersededDocument = documentFixture({ superseded: true });
    const historicalFact: WorkbenchAssertion = {
      ...assertion,
      review_state: "accepted",
      evidence_basis: "source_evidence",
      review_reason: "Previously matched the source.",
      reviewed_by: "Avery Analyst",
      evidence: [{ ...assertion.evidence[0], document_id: supersededDocument.id }],
    };
    vi.mocked(getDocumentPages).mockResolvedValue([]);
    render(
      <WorkbenchEvidence
        caseId="case-1"
        caseStatus="evidence_review"
        caseVersion={3}
        documents={[supersededDocument]}
        assertions={[historicalFact]}
        onRefresh={vi.fn()}
      />,
    );

    const card = screen.getByRole("button", { name: /contract end date.*superseded source/i });
    expect(card).toHaveAttribute("data-evidence-basis", "inactive_evidence");
    expect(card).toHaveTextContent(/do not use.*all cited sources are superseded/i);
    expect(screen.getByText(/cites only superseded documents/i)).toBeVisible();
    expect(screen.getByText("Historical source evidence — inactive")).toBeVisible();
  });

  it("never leaves a hidden assertion actionable after filtering", () => {
    const accepted = {
      ...assertion,
      id: "accepted-assertion",
      review_state: "accepted" as const,
      evidence_basis: "source_evidence" as const,
    };
    const proposed = {
      ...assertion,
      id: "proposed-assertion",
      semantic_key: "auto_renewal",
      display_value: "Yes",
    };
    render(
      <WorkbenchEvidence
        caseId="case-1"
        caseStatus="evidence_review"
        caseVersion={3}
        documents={[]}
        assertions={[accepted, proposed]}
        onRefresh={vi.fn()}
      />,
    );

    fireEvent.change(screen.getByLabelText("Filter review status"), {
      target: { value: "proposed" },
    });
    expect(screen.getByRole("heading", { name: "Auto renewal" })).toBeVisible();
    expect(screen.queryByText(/a final accepted decision is recorded/i)).not.toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText(/search facts or values/i), {
      target: { value: "no-match-value" },
    });
    expect(screen.getByText(/no current assertions match these filters/i)).toBeVisible();
    expect(screen.queryByText("Selected fact")).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: /review decision/i })).not.toBeInTheDocument();
  });

  it("records an explicit evidence-backed human decision", async () => {
    vi.mocked(reviewAssertion).mockResolvedValue({ ...assertion, review_state: "accepted" });
    const onReviewed = vi.fn().mockResolvedValue(undefined);
    render(
      <AssertionReviewForm
        assertion={assertion}
        documents={[documentFixture()]}
        canReview
        onReviewed={onReviewed}
      />,
    );
    expect(screen.queryByRole("checkbox", { name: /explicit assumption/i })).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/reason for this decision/i), {
      target: { value: "Verified the proposed date against the cited clause." },
    });
    fireEvent.click(screen.getByRole("button", { name: /record accept/i }));

    await waitFor(() =>
      expect(reviewAssertion).toHaveBeenCalledWith(assertion, {
        decision: "accept",
        reason: "Verified the proposed date against the cited clause.",
        correctedValue: undefined,
        assumption: false,
      }),
    );
    expect(onReviewed).toHaveBeenCalledOnce();
  });

  it("offers only correction recovery for a final reviewed assertion", () => {
    render(
      <AssertionReviewForm
        assertion={{ ...assertion, review_state: "accepted" }}
        documents={[documentFixture()]}
        canReview
        onReviewed={vi.fn()}
      />,
    );

    expect(screen.getByText(/a final accepted decision is recorded/i)).toBeVisible();
    expect(screen.getByRole("group", { name: /review decision/i })).toBeVisible();
    expect(screen.getByRole("button", { name: "Correct" })).toBeVisible();
    expect(screen.getByLabelText(/normalized value/i)).toBeVisible();
    expect(screen.queryByRole("button", { name: /record accept/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Accept$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Reject$/i })).not.toBeInTheDocument();
  });

  it("preserves an inherited assumption label through a later correction", async () => {
    const assumed = {
      ...assertion,
      source: "human" as const,
      review_state: "corrected" as const,
      assumption: true,
      evidence_basis: "explicit_assumption" as const,
    };
    vi.mocked(reviewAssertion).mockResolvedValue({ ...assumed, version: 3 });
    const onReviewed = vi.fn().mockResolvedValue(undefined);
    render(
      <AssertionReviewForm
        assertion={assumed}
        documents={[documentFixture()]}
        canReview
        onReviewed={onReviewed}
      />,
    );

    const checkbox = screen.getByRole("checkbox", { name: /explicit assumption/i });
    expect(checkbox).toBeChecked();
    expect(checkbox).toBeDisabled();
    expect(screen.getByText(/descends from an explicit assumption/i)).toBeVisible();
    fireEvent.change(screen.getByLabelText(/reason for this decision/i), {
      target: { value: "Later review confirms the scenario value remains unchanged." },
    });
    fireEvent.click(screen.getByRole("button", { name: /record correct/i }));

    await waitFor(() =>
      expect(reviewAssertion).toHaveBeenCalledWith(assumed, {
        decision: "correct",
        reason: "Later review confirms the scenario value remains unchanged.",
        correctedValue: assertion.normalized_value,
        assumption: true,
      }),
    );
    expect(onReviewed).toHaveBeenCalledOnce();
  });

  it("prevents a correction with invalid normalized JSON", async () => {
    render(
      <AssertionReviewForm
        assertion={assertion}
        documents={[documentFixture()]}
        canReview
        onReviewed={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Correct" }));
    fireEvent.change(screen.getByLabelText(/normalized value/i), { target: { value: "not-json" } });
    fireEvent.change(screen.getByLabelText(/reason for this decision/i), {
      target: { value: "The extracted date is wrong." },
    });
    fireEvent.click(screen.getByRole("button", { name: /record correct/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/unexpected token|json/i);
    expect(reviewAssertion).not.toHaveBeenCalled();
  });

  it("submits only typed JSON for a correction and explains that display text is derived", async () => {
    vi.mocked(reviewAssertion).mockResolvedValue({
      ...assertion,
      normalized_value: { type: "date", value: "2028-01-31" },
      display_value: "January 31, 2028",
      review_state: "corrected",
    });
    const onReviewed = vi.fn().mockResolvedValue(undefined);
    render(
      <AssertionReviewForm
        assertion={assertion}
        documents={[documentFixture()]}
        canReview
        onReviewed={onReviewed}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Correct" }));
    expect(screen.queryByLabelText(/corrected display value/i)).not.toBeInTheDocument();
    expect(screen.getByText(/display text is derived from this typed json/i)).toBeVisible();
    fireEvent.change(screen.getByLabelText(/normalized value/i), {
      target: { value: JSON.stringify({ type: "date", value: "2028-01-31" }) },
    });
    fireEvent.change(screen.getByLabelText(/reason for this decision/i), {
      target: { value: "The cited end date is January 31, 2028." },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: /explicit assumption/i }));
    fireEvent.click(screen.getByRole("button", { name: /record correct/i }));

    await waitFor(() =>
      expect(reviewAssertion).toHaveBeenCalledWith(assertion, {
        decision: "correct",
        reason: "The cited end date is January 31, 2028.",
        correctedValue: { type: "date", value: "2028-01-31" },
        assumption: true,
      }),
    );
    expect(onReviewed).toHaveBeenCalledOnce();
  });

  it("requires an explicit assumption when a correction changes the typed value", async () => {
    render(
      <AssertionReviewForm
        assertion={assertion}
        documents={[documentFixture()]}
        canReview
        onReviewed={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Correct" }));
    fireEvent.change(screen.getByLabelText(/normalized value/i), {
      target: { value: JSON.stringify({ type: "date", value: "2028-01-31" }) },
    });
    fireEvent.change(screen.getByLabelText(/reason for this decision/i), {
      target: { value: "The typed value must change." },
    });
    fireEvent.click(screen.getByRole("button", { name: /record correct/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/explicit assumption/i);
    expect(reviewAssertion).not.toHaveBeenCalled();
  });

  it("does not offer source acceptance for a proposal without exact evidence", () => {
    render(
      <AssertionReviewForm
        assertion={{ ...assertion, evidence: [] }}
        canReview
        onReviewed={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: "Accept" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Correct" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Reject" })).toBeVisible();
    const assumption = screen.getByRole("checkbox", { name: /explicit assumption/i });
    expect(assumption).toBeChecked();
    expect(assumption).toBeDisabled();
    expect(screen.getByText(/no active exact source span supports this proposal/i)).toBeVisible();
  });

  it("treats citations from only superseded documents like missing active evidence", () => {
    const supersededDocument = documentFixture({ superseded: true });
    render(
      <AssertionReviewForm
        assertion={assertion}
        documents={[supersededDocument]}
        canReview
        onReviewed={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: "Accept" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Correct" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Reject" })).toBeVisible();
    expect(screen.getByRole("checkbox", { name: /explicit assumption/i })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /explicit assumption/i })).toBeDisabled();
    expect(screen.getByText(/no active exact source span supports this proposal/i)).toBeVisible();
  });

  it("does not offer a known-invalid accept decision for a conflict object", () => {
    render(
      <AssertionReviewForm
        assertion={{
          ...assertion,
          review_state: "conflicting",
          normalized_value: {
            type: "conflict",
            candidates: [
              { type: "date", value: "2027-12-31" },
              { type: "date", value: "2028-12-31" },
            ],
          },
        }}
        documents={[documentFixture()]}
        canReview
        onReviewed={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: "Accept" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Correct" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Reject" })).toBeVisible();
  });

  it("retries a transient page-catalog failure for the same document", async () => {
    vi.mocked(getDocumentPages)
      .mockRejectedValueOnce(new Error("Temporary page service failure"))
      .mockResolvedValueOnce([]);
    render(
      <WorkbenchEvidence
        caseId="case-1"
        caseStatus="evidence_review"
        caseVersion={3}
        documents={[documentFixture()]}
        assertions={[assertion]}
        onRefresh={vi.fn()}
      />,
    );

    const retry = await screen.findByRole("button", { name: "Retry page catalog" });
    expect(screen.getByRole("alert")).toHaveTextContent(/page catalog unavailable/i);
    fireEvent.click(retry);

    await waitFor(() => expect(getDocumentPages).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Retry page catalog" })).not.toBeInTheDocument(),
    );
  });

  it("states that archived evidence is permanently read-only", async () => {
    vi.mocked(getDocumentPages).mockResolvedValue([]);
    render(
      <WorkbenchEvidence
        caseId="case-1"
        caseStatus="archived"
        caseVersion={3}
        documents={[documentFixture()]}
        assertions={[assertion]}
        onRefresh={vi.fn()}
      />,
    );

    expect(
      screen.getByText(/archived cases are permanently read-only and have no further transitions/i),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Evidence locked at this workflow state" }),
    ).toBeDisabled();
    expect(screen.queryByRole("group", { name: /review decision/i })).not.toBeInTheDocument();
  });

  it("refreshes after a persisted upload failure without masking the processing error", async () => {
    vi.mocked(uploadAndProcessContract).mockRejectedValue(
      new Error("Document processing failed safely; inspect the document status for details"),
    );
    const onRefresh = vi.fn();
    const onRecoveryRefresh = vi
      .fn()
      .mockRejectedValue(new Error("Snapshot refresh also failed"));
    render(
      <WorkbenchEvidence
        caseId="case-1"
        caseStatus="draft"
        caseVersion={1}
        documents={[]}
        assertions={[]}
        onRefresh={onRefresh}
        onRecoveryRefresh={onRecoveryRefresh}
      />,
    );

    const file = new File(["%PDF-1.7 synthetic"], "partial-upload.pdf", {
      type: "application/pdf",
    });
    fireEvent.change(screen.getByLabelText("Document source"), {
      target: { value: "synthetic" },
    });
    fireEvent.change(screen.getByLabelText("Contract PDF file"), {
      target: { files: [file] },
    });

    await waitFor(() => expect(uploadAndProcessContract).toHaveBeenCalledWith("case-1", file, "synthetic"));
    expect(onRefresh).not.toHaveBeenCalled();
    expect(onRecoveryRefresh).toHaveBeenCalledOnce();
    expect(await screen.findByRole("alert")).toHaveTextContent(/processing failed safely/i);
    expect(screen.getByRole("alert")).not.toHaveTextContent(/snapshot refresh/i);
  });

  it("requires an explicit source classification before accepting an upload", async () => {
    render(
      <WorkbenchEvidence
        caseId="case-1"
        caseStatus="draft"
        caseVersion={1}
        documents={[]}
        assertions={[]}
        onRefresh={vi.fn()}
      />,
    );

    const file = new File(["%PDF-1.7 synthetic"], "unclassified.pdf", {
      type: "application/pdf",
    });
    fireEvent.change(screen.getByLabelText("Contract PDF file"), {
      target: { files: [file] },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(/choose whether/i);
    expect(uploadAndProcessContract).not.toHaveBeenCalled();
  });

  it("renders the operator-supplied source classification and selected-state semantics", () => {
    vi.mocked(getDocumentPages).mockResolvedValue([]);
    render(
      <WorkbenchEvidence
        caseId="case-1"
        caseStatus="evidence_review"
        caseVersion={2}
        documents={[documentFixture({ source_type: "redacted" })]}
        assertions={[assertion]}
        onRefresh={vi.fn()}
      />,
    );

    expect(screen.getByText("Source classification: Redacted")).toBeVisible();
    expect(screen.getByText(/redacted.*does not certify/i)).toBeVisible();
    expect(screen.getByRole("button", { name: /source\.pdf/i })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: /contract end date/i })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("resets source classification after a successful upload and distinguishes refresh failure", async () => {
    vi.mocked(uploadAndProcessContract).mockResolvedValue(documentFixture());
    const onRefresh = vi.fn().mockRejectedValue(new Error("Snapshot unavailable"));
    render(
      <WorkbenchEvidence
        caseId="case-1"
        caseStatus="draft"
        caseVersion={1}
        documents={[]}
        assertions={[]}
        onRefresh={onRefresh}
      />,
    );

    fireEvent.change(screen.getByLabelText("Document source"), {
      target: { value: "public" },
    });
    const file = new File(["%PDF-1.7 public"], "public-source.pdf", {
      type: "application/pdf",
    });
    fireEvent.change(screen.getByLabelText("Contract PDF file"), {
      target: { files: [file] },
    });

    await waitFor(() =>
      expect(uploadAndProcessContract).toHaveBeenCalledWith("case-1", file, "public"),
    );
    expect(screen.getByLabelText("Document source")).toHaveValue("");
    expect(await screen.findByRole("alert")).toHaveTextContent(/processed successfully/i);
    expect(screen.getByRole("alert")).toHaveTextContent(/could not refresh/i);
  });

  it("retries processing for a persisted failed document and refreshes its status", async () => {
    const failed = documentFixture({
      processing_status: "failed",
      processing_error: "The extraction worker stopped safely.",
    });
    vi.mocked(getDocumentPages).mockResolvedValue([]);
    vi.mocked(processContract).mockResolvedValue({
      ...failed,
      processing_status: "processed",
      processing_error: null,
    });
    const onRefresh = vi.fn().mockResolvedValue(undefined);
    render(
      <WorkbenchEvidence
        caseId="case-1"
        caseStatus="evidence_review"
        caseVersion={3}
        documents={[failed]}
        assertions={[]}
        onRefresh={onRefresh}
      />,
    );

    expect(screen.getByText("The extraction worker stopped safely.")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Retry processing" }));

    await waitFor(() => expect(processContract).toHaveBeenCalledWith("document-1"));
    expect(onRefresh).toHaveBeenCalledOnce();
  });

  it("does not offer processing retry when OCR or workflow recovery is unsupported", () => {
    vi.mocked(getDocumentPages).mockResolvedValue([]);
    const needsOcr = documentFixture({ processing_status: "needs_ocr" });
    const { rerender } = render(
      <WorkbenchEvidence
        caseId="case-1"
        caseStatus="evidence_review"
        caseVersion={3}
        documents={[needsOcr]}
        assertions={[]}
        onRefresh={vi.fn()}
      />,
    );

    expect(screen.getByText("OCR required")).toBeVisible();
    expect(screen.getByText(/retrying processing will not add ocr/i)).toBeVisible();
    expect(screen.queryByRole("button", { name: /process document|retry processing/i })).not.toBeInTheDocument();

    rerender(
      <WorkbenchEvidence
        caseId="case-1"
        caseStatus="internal_packet_approved"
        caseVersion={4}
        documents={[documentFixture({ processing_status: "failed" })]}
        assertions={[]}
        onRefresh={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: /retry processing/i })).not.toBeInTheDocument();
    expect(screen.getByText(/revision is locked.*reopen evidence review/i)).toBeVisible();
  });

  it("supersedes a selected source with the current case version and written reason", async () => {
    const documents: DocumentSummary[] = ["source-a.pdf", "source-b.pdf"].map(
      (original_filename, index) => ({
        id: `document-${index + 1}`,
        case_revision_id: "revision-1",
        original_filename,
        sha256: String(index + 1).repeat(64),
        media_type: "application/pdf",
        source_type: "synthetic",
        size_bytes: 1_024,
        page_count: 1,
        safety_status: "passed",
        processing_status: "processed",
        processing_error: null,
        superseded: false,
        created_at: "2026-08-21T00:00:00Z",
      }),
    );
    vi.mocked(getDocumentPages).mockResolvedValue([]);
    vi.mocked(supersedeContract).mockResolvedValue({ ...documents[0], superseded: true });
    const onRefresh = vi.fn().mockResolvedValue(undefined);

    render(
      <WorkbenchEvidence
        caseId="case-1"
        caseStatus="evidence_review"
        caseVersion={7}
        documents={documents}
        assertions={[]}
        onRefresh={onRefresh}
      />,
    );

    fireEvent.change(screen.getByLabelText(/replacement reason/i), {
      target: { value: "The second processed PDF is the governing amendment." },
    });
    fireEvent.click(screen.getByRole("button", { name: /supersede selected source/i }));

    await waitFor(() =>
      expect(supersedeContract).toHaveBeenCalledWith(
        "document-1",
        7,
        "The second processed PDF is the governing amendment.",
      ),
    );
    expect(onRefresh).toHaveBeenCalledOnce();
  });

  it("allows a failed or OCR-only source to be superseded when one processed replacement exists", () => {
    vi.mocked(getDocumentPages).mockResolvedValue([]);
    render(
      <WorkbenchEvidence
        caseId="case-1"
        caseStatus="evidence_review"
        caseVersion={7}
        documents={[
          documentFixture({ id: "needs-ocr", processing_status: "needs_ocr" }),
          documentFixture({ id: "replacement", original_filename: "replacement.pdf" }),
        ]}
        assertions={[]}
        onRefresh={vi.fn()}
      />,
    );

    expect(screen.getByText("OCR required")).toBeVisible();
    expect(screen.getByRole("button", { name: /supersede selected source/i })).toBeEnabled();
  });

  it("locks document and assertion mutations after internal-review handoff", () => {
    vi.mocked(getDocumentPages).mockResolvedValue([]);

    render(
      <WorkbenchEvidence
        caseId="case-1"
        caseStatus="internal_packet_approved"
        caseVersion={8}
        documents={[]}
        assertions={[assertion]}
        onRefresh={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: /evidence locked at this workflow state/i })).toBeDisabled();
    expect(screen.getByText(/revision is locked at the current workflow state/i)).toBeVisible();
    expect(screen.queryByRole("group", { name: /review decision/i })).not.toBeInTheDocument();
  });
});
