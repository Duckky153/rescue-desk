import type { CaseStatus } from "@/types/api";

const LABELS: Record<CaseStatus, string> = {
  draft: "Draft",
  evidence_review: "Evidence review",
  ready_for_internal_review: "Ready for review",
  internal_packet_approved: "Internally approved",
  exported: "Exported",
  archived: "Archived",
};

export function CaseStatusPill({ status }: { status: CaseStatus }) {
  const tone =
    status === "internal_packet_approved" || status === "exported"
      ? "success"
      : status === "ready_for_internal_review"
        ? "info"
        : status === "evidence_review"
          ? "warning"
          : "";
  return <span className={`status-pill ${tone}`}>{LABELS[status]}</span>;
}

export function ReviewStatusPill({ status }: { status: string }) {
  const tone =
    status === "accepted" || status === "corrected"
      ? "success"
      : status === "rejected" || status === "conflicting"
        ? "danger"
        : "warning";
  return <span className={`status-pill ${tone}`}>{status.replaceAll("_", " ")}</span>;
}
