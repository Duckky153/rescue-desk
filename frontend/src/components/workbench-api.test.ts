import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  getWorkbenchSnapshot,
  reviewAssertion,
  supersedeFee,
  type WorkbenchAssertion,
} from "@/components/workbench-api";
import { clearPendingIntents } from "@/lib/api";

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

const assertion: WorkbenchAssertion = {
  id: "assertion-1",
  case_revision_id: "revision-1",
  semantic_key: "contract_end_date",
  raw_value: "December 31, 2027",
  normalized_value: { type: "date", value: "2027-12-31" },
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
  evidence: [],
};

describe("workbench mutation contracts", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    clearPendingIntents();
    window.sessionStorage.setItem("rescuedesk.token", "test-token");
    vi.restoreAllMocks();
  });

  it("loads one transactionally fenced aggregate snapshot instead of independent resources", async () => {
    const caseDetail = {
      id: "case-1",
      organization_id: "org-1",
      display_name: "Synthetic matter",
      applicant_company: "Synthetic Company",
      erp_provider: "Synthetic ERP",
      status: "evidence_review",
      current_revision_number: 1,
      version: 3,
      assigned_analyst_id: "analyst-1",
      assigned_approver_id: "approver-1",
      revisions: [],
      created_at: "2026-08-21T00:00:00Z",
      updated_at: "2026-08-21T00:00:00Z",
    };
    const readiness = {
      ready_for_internal_review: false,
      mandatory_assertions_reviewed: false,
      reproducible_calculation_exists: false,
      open_blocking_findings: 0,
      findings: [],
      disclaimer: "Synthetic demonstration only.",
    };
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        case_detail: caseDetail,
        documents: [],
        assertions: [assertion],
        fees: [],
        calculation: null,
        readiness,
        audit_events: [],
        exports: [],
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(getWorkbenchSnapshot("case-1")).resolves.toEqual({
      caseDetail,
      documents: [],
      assertions: [assertion],
      fees: [],
      calculation: null,
      readiness,
      auditEvents: [],
      exports: [],
    });
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "http://127.0.0.1:8000/v1/cases/case-1/workbench-snapshot",
    );
  });

  it("sends only typed JSON for corrections because display text is server-derived", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        ...assertion,
        normalized_value: { type: "date", value: "2028-01-31" },
        display_value: "2028-01-31",
        review_state: "corrected",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await reviewAssertion(assertion, {
      decision: "correct",
      reason: "The source requires a typed correction.",
      correctedValue: { type: "date", value: "2028-01-31" },
      assumption: true,
    });

    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    const body = JSON.parse(String(init.body)) as Record<string, unknown>;
    expect(body).toMatchObject({
      decision: "correct",
      corrected_value: { type: "date", value: "2028-01-31" },
      assumption: true,
    });
    expect(body).not.toHaveProperty("corrected_display_value");
    expect(new Headers(init.headers).get("Idempotency-Key")).toMatch(/^assertion-review-/);
  });

  it("uses the versioned idempotent fee-recovery endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ id: "fee-1", superseded: true }));
    vi.stubGlobal("fetch", fetchMock);

    await supersedeFee("case-1", "fee-1", 7, "The row was entered from the wrong schedule.");

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "http://127.0.0.1:8000/v1/cases/case-1/fees/fee-1/supersede",
    );
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(JSON.parse(String(init.body))).toEqual({
      expected_case_version: 7,
      reason: "The row was entered from the wrong schedule.",
    });
    expect(new Headers(init.headers).get("Idempotency-Key")).toMatch(/^fee-supersede-/);
  });
});
