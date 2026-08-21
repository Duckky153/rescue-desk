import {
  ApiError,
  apiDownload,
  apiRequest,
  sha256Hex,
  stableIntentFingerprint,
  withIdempotentIntent,
} from "@/lib/api";
import type {
  Assertion,
  AuditEvent,
  Calculation,
  CaseDetail,
  CaseStatus,
  DocumentSummary,
  EvidenceSpan,
  Fee,
  Readiness,
} from "@/types/api";

export type WorkbenchTab = "evidence" | "economics" | "readiness" | "audit";

export interface DocumentPage {
  id: string;
  page_number: number;
  text: string;
  text_sha256: string;
  extraction_confidence: string;
}

export interface WorkbenchEvidenceSpan extends EvidenceSpan {
  page_number?: number;
  document_id: string;
  document_name?: string;
}

export interface WorkbenchAssertion extends Omit<Assertion, "evidence"> {
  evidence: WorkbenchEvidenceSpan[];
}

export type ExportKind =
  | "internal_review_pdf"
  | "customer_explanation_pdf"
  | "evidence_csv"
  | "machine_readable_json";

export interface ExportArtifact {
  id: string;
  case_revision_id: string;
  calculation_id: string;
  kind: ExportKind;
  sha256: string;
  packet_snapshot_sha256: string;
  approval_status: "not_approved" | "approved";
  generator_version: string;
  superseded: boolean;
  content_type: string;
  filename: string;
  size_bytes: number;
  download_url: string;
  disclaimer: string;
  created_at: string;
}

export interface WorkbenchSnapshot {
  caseDetail: CaseDetail;
  documents: DocumentSummary[];
  assertions: WorkbenchAssertion[];
  fees: Fee[];
  calculation: Calculation | null;
  readiness: Readiness;
  auditEvents: AuditEvent[];
  exports: ExportArtifact[];
}

interface WorkbenchSnapshotResponse {
  case_detail: CaseDetail;
  documents: DocumentSummary[];
  assertions: WorkbenchAssertion[];
  fees: Fee[];
  calculation: Calculation | null;
  readiness: Readiness;
  audit_events: AuditEvent[];
  exports: ExportArtifact[];
}

async function optionalRequest<T>(path: string, fallback: T): Promise<T> {
  try {
    return await apiRequest<T>(path);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return fallback;
    throw error;
  }
}

export async function getWorkbenchSnapshot(caseId: string): Promise<WorkbenchSnapshot> {
  const response = await apiRequest<WorkbenchSnapshotResponse>(
    `/v1/cases/${caseId}/workbench-snapshot`,
  );
  return {
    caseDetail: response.case_detail,
    documents: response.documents,
    assertions: response.assertions,
    fees: response.fees,
    calculation: response.calculation,
    readiness: response.readiness,
    auditEvents: response.audit_events,
    exports: response.exports,
  };
}

export async function getDocumentPages(documentId: string): Promise<DocumentPage[]> {
  return optionalRequest<DocumentPage[]>(`/v1/documents/${documentId}/pages`, []);
}

export async function uploadContract(
  caseId: string,
  file: File,
  sourceType: "synthetic" | "public" | "redacted",
): Promise<DocumentSummary> {
  const fileHash = await sha256Hex(await file.arrayBuffer());
  const body = new FormData();
  body.set("file", file);
  body.set("source_type", sourceType);
  return apiRequest<DocumentSummary>(
    `/v1/cases/${caseId}/documents`,
    { method: "POST", body },
    {
      intent: {
        scope: "upload-contract",
        fingerprint: stableIntentFingerprint({
          caseId,
          sourceType,
          filename: file.name,
          size: file.size,
          fileHash,
        }),
      },
    },
  );
}

export function downloadContract(documentId: string): Promise<Blob> {
  return apiDownload(`/v1/documents/${documentId}/file`);
}

export async function processContract(documentId: string): Promise<DocumentSummary> {
  return apiRequest<DocumentSummary>(
    `/v1/documents/${documentId}/process`,
    { method: "POST" },
    {
      intent: {
        scope: "process-contract",
        fingerprint: stableIntentFingerprint({ documentId }),
      },
    },
  );
}

export async function uploadAndProcessContract(
  caseId: string,
  file: File,
  sourceType: "synthetic" | "public" | "redacted",
): Promise<DocumentSummary> {
  const fileHash = await sha256Hex(await file.arrayBuffer());
  const fingerprint = stableIntentFingerprint({
    caseId,
    sourceType,
    filename: file.name,
    size: file.size,
    fileHash,
  });
  return withIdempotentIntent({ scope: "contract-ingest", fingerprint }, async (workflowKey) => {
    const body = new FormData();
    body.set("file", file);
    body.set("source_type", sourceType);
    const uploaded = await apiRequest<DocumentSummary>(
      `/v1/cases/${caseId}/documents`,
      { method: "POST", body },
      { idempotencyKey: `${workflowKey}-upload` },
    );
    return apiRequest<DocumentSummary>(
      `/v1/documents/${uploaded.id}/process`,
      { method: "POST" },
      { idempotencyKey: `${workflowKey}-process` },
    );
  });
}

export async function supersedeContract(
  documentId: string,
  expectedCaseVersion: number,
  reason: string,
): Promise<DocumentSummary> {
  return apiRequest<DocumentSummary>(
    `/v1/documents/${documentId}/supersede`,
    {
      method: "POST",
      body: JSON.stringify({ expected_case_version: expectedCaseVersion, reason }),
    },
    {
      intent: {
        scope: "supersede-contract",
        fingerprint: stableIntentFingerprint({ documentId, expectedCaseVersion, reason }),
      },
    },
  );
}

export async function reviewAssertion(
  assertion: WorkbenchAssertion,
  input: {
    decision: "accept" | "correct" | "reject";
    reason: string;
    correctedValue?: Record<string, unknown>;
    assumption: boolean;
  },
): Promise<WorkbenchAssertion> {
  return apiRequest<WorkbenchAssertion>(
    `/v1/cases/assertions/${assertion.id}/reviews`,
    {
      method: "POST",
      body: JSON.stringify({
        decision: input.decision,
        expected_assertion_version: assertion.version,
        reason: input.reason,
        corrected_value: input.correctedValue ?? null,
        assumption: input.assumption,
      }),
    },
    {
      intent: {
        scope: "assertion-review",
        fingerprint: stableIntentFingerprint({
          assertionId: assertion.id,
          assertionVersion: assertion.version,
          ...input,
        }),
      },
    },
  );
}

export async function addFee(
  caseId: string,
  input: {
    category: Fee["category"];
    amount_minor: number;
    currency: string;
    service_start: string | null;
    service_end: string | null;
    obligation_date: string | null;
    payment_status: string;
    billing_cadence: string | null;
    proration_rule: string | null;
    assertion_ids: string[];
    reviewed: boolean;
  },
): Promise<Fee> {
  return apiRequest<Fee>(
    `/v1/cases/${caseId}/fees`,
    { method: "POST", body: JSON.stringify(input) },
    {
      intent: {
        scope: "fee",
        fingerprint: stableIntentFingerprint({ caseId, ...input }),
      },
    },
  );
}

export async function supersedeFee(
  caseId: string,
  feeId: string,
  expectedCaseVersion: number,
  reason: string,
): Promise<Fee> {
  return apiRequest<Fee>(
    `/v1/cases/${caseId}/fees/${feeId}/supersede`,
    {
      method: "POST",
      body: JSON.stringify({
        expected_case_version: expectedCaseVersion,
        reason,
      }),
    },
    {
      intent: {
        scope: "fee-supersede",
        fingerprint: stableIntentFingerprint({
          caseId,
          feeId,
          expectedCaseVersion,
          reason,
        }),
      },
    },
  );
}

export async function runCalculation(caseId: string, asOfDate: string): Promise<Calculation> {
  return apiRequest<Calculation>(
    `/v1/cases/${caseId}/calculations`,
    { method: "POST", body: JSON.stringify({ as_of_date: asOfDate }) },
    {
      intent: {
        scope: "calculation",
        fingerprint: stableIntentFingerprint({ caseId, asOfDate }),
      },
    },
  );
}

export async function transitionCase(
  caseId: string,
  target: CaseStatus,
  expectedVersion: number,
  reason: string,
): Promise<CaseDetail> {
  return apiRequest<CaseDetail>(
    `/v1/cases/${caseId}/transitions`,
    {
      method: "POST",
      body: JSON.stringify({ target, expected_version: expectedVersion, reason }),
    },
    {
      intent: {
        scope: "case-transition",
        fingerprint: stableIntentFingerprint({ caseId, target, expectedVersion, reason }),
      },
    },
  );
}

export async function resolveFinding(
  caseId: string,
  finding: Readiness["findings"][number],
  disposition: "resolved" | "accepted_risk",
  reason: string,
): Promise<Readiness["findings"][number]> {
  return apiRequest<Readiness["findings"][number]>(
    `/v1/cases/${caseId}/findings/${finding.id}/disposition`,
    {
      method: "POST",
      body: JSON.stringify({
        status: disposition,
        expected_version: finding.version,
        reason,
      }),
    },
    {
      intent: {
        scope: "finding-disposition",
        fingerprint: stableIntentFingerprint({
          caseId,
          findingId: finding.id,
          findingVersion: finding.version,
          disposition,
          reason,
        }),
      },
    },
  );
}

export async function createExport(
  caseId: string,
  kind: ExportKind,
  calculationId: string | null,
): Promise<ExportArtifact> {
  return apiRequest<ExportArtifact>(
    `/v1/cases/${caseId}/exports`,
    {
      method: "POST",
      body: JSON.stringify({ kind, calculation_id: calculationId }),
    },
    {
      intent: {
        scope: "export",
        fingerprint: stableIntentFingerprint({ caseId, kind, calculationId }),
      },
    },
  );
}

export function downloadExport(caseId: string, exportId: string): Promise<Blob> {
  return apiDownload(`/v1/cases/${caseId}/exports/${exportId}/download`);
}
