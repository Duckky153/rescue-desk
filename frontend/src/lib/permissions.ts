import type { CaseStatus, Role } from "@/types/api";

const REVIEW_ROLES: Role[] = ["analyst", "approver", "admin"];
const DOCUMENT_ROLES: Role[] = ["uploader", "analyst", "approver", "admin"];

export function isEvidenceMutable(status: CaseStatus): boolean {
  return status === "draft" || status === "evidence_review";
}

export function canCreateMatter(role: Role | null | undefined): boolean {
  return Boolean(role && REVIEW_ROLES.includes(role));
}

export function canUploadDocuments(
  role: Role | null | undefined,
  status: CaseStatus,
): boolean {
  return Boolean(role && DOCUMENT_ROLES.includes(role) && isEvidenceMutable(status));
}

export function canReviewEvidence(
  role: Role | null | undefined,
  status: CaseStatus,
): boolean {
  return Boolean(role && REVIEW_ROLES.includes(role) && isEvidenceMutable(status));
}

export const canManageEconomics = canReviewEvidence;
