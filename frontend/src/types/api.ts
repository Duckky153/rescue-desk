export type Role = "uploader" | "analyst" | "approver" | "auditor" | "admin";
export type CaseStatus =
  | "draft"
  | "evidence_review"
  | "ready_for_internal_review"
  | "internal_packet_approved"
  | "exported"
  | "archived";

export interface CurrentUser {
  id: string;
  email: string;
  display_name: string;
  organization_id: string;
  role: Role;
}

export interface TokenResponse {
  access_token: string;
  token_type: "bearer";
  expires_at: string;
  organization_id: string;
  role: Role;
}

export interface CaseSummary {
  id: string;
  display_name: string;
  applicant_company: string;
  erp_provider: string;
  status: CaseStatus;
  current_revision_number: number;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface RevisionSummary {
  id: string;
  number: number;
  reason: string;
  snapshot_hash: string | null;
  created_by: string;
  created_at: string;
}

export interface CaseDetail extends CaseSummary {
  organization_id: string;
  assigned_analyst_id: string | null;
  assigned_approver_id: string | null;
  revisions: RevisionSummary[];
}

export interface DocumentSummary {
  id: string;
  case_revision_id: string;
  original_filename: string;
  sha256: string;
  media_type: string;
  source_type: "synthetic" | "public" | "redacted";
  size_bytes: number;
  page_count: number;
  safety_status: "pending" | "passed" | "rejected";
  processing_status: "uploaded" | "processing" | "processed" | "needs_ocr" | "failed";
  processing_error: string | null;
  superseded: boolean;
  created_at: string;
}

export interface EvidenceSpan {
  id: string;
  page_id: string;
  page_number?: number;
  quote: string;
  char_start: number;
  char_end: number;
  quote_sha256: string;
}

export interface Assertion {
  id: string;
  case_revision_id: string;
  semantic_key: string;
  raw_value: string;
  normalized_value: Record<string, unknown>;
  display_value: string;
  source: "deterministic" | "ai" | "human" | "derived";
  confidence: string;
  review_state: "proposed" | "accepted" | "corrected" | "rejected" | "conflicting";
  version: number;
  is_current: boolean;
  created_at: string;
  evidence: EvidenceSpan[];
  assumption: boolean;
  review_reason: string | null;
  reviewed_by: string | null;
  evidence_basis:
    | "pending_review"
    | "source_evidence"
    | "explicit_assumption"
    | "rejected_source"
    | "invalid_review";
}

export interface Fee {
  id: string;
  case_revision_id: string;
  category:
    | "subscription"
    | "tax"
    | "penalty"
    | "professional_service"
    | "implementation"
    | "termination"
    | "other"
    | "unclassified";
  amount_minor: number;
  currency: string;
  service_start: string | null;
  service_end: string | null;
  obligation_date: string | null;
  payment_status: "unknown" | "unpaid" | "paid";
  billing_cadence: string | null;
  proration_rule: string | null;
  assertion_ids: string[];
  reviewed: boolean;
  primary_money_assertion_id: string | null;
  superseded: boolean;
  superseded_at: string | null;
  superseded_by: string | null;
  supersede_reason: string | null;
  created_at: string;
}

export interface CalculationCurrency {
  currency: string;
  documented_remaining_subscription_minor: number;
  excluded_non_subscription_minor: number;
  unclassified_minor: number;
  potential_coverage_min_minor: number;
  potential_coverage_max_minor: number;
}

export interface Calculation {
  id: string;
  case_revision_id: string;
  as_of_date: string;
  engine_version: string;
  currencies: CalculationCurrency[];
  line_items: Array<{
    identifier: string;
    treatment: string;
    original_amount_minor: number;
    remaining_amount_minor: number | null;
    currency: string;
    formula_identifier: string;
    explanation: string;
  }>;
  blocking_findings: string[];
  assumptions: string[];
  input_hash: string;
  result_hash: string;
  created_at: string;
}

export interface Readiness {
  ready_for_internal_review: boolean;
  mandatory_assertions_reviewed: boolean;
  reproducible_calculation_exists: boolean;
  open_blocking_findings: number;
  findings: Array<{
    id: string;
    code: string;
    title: string;
    detail: string;
    severity: "blocking" | "warning" | "info";
    status: "open" | "resolved" | "accepted_risk";
    version: number;
    resolution_reason: string | null;
    created_at: string;
    resolved_at: string | null;
  }>;
  disclaimer: string;
}

export interface AuditEvent {
  id: string;
  actor_id: string;
  action: string;
  object_type: string;
  object_id: string;
  correlation_id: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  previous_hash: string | null;
  event_hash: string;
  created_at: string;
}
