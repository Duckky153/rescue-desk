import { describe, expect, it } from "vitest";
import {
  canCreateMatter,
  canManageEconomics,
  canReviewEvidence,
  canUploadDocuments,
  isEvidenceMutable,
} from "@/lib/permissions";

describe("frontend permissions mirror API mutation boundaries", () => {
  it("allows case creation only for analyst-class roles", () => {
    expect(canCreateMatter("analyst")).toBe(true);
    expect(canCreateMatter("approver")).toBe(true);
    expect(canCreateMatter("admin")).toBe(true);
    expect(canCreateMatter("uploader")).toBe(false);
    expect(canCreateMatter("auditor")).toBe(false);
  });

  it("allows uploaders to handle documents but not review facts or economics", () => {
    expect(canUploadDocuments("uploader", "draft")).toBe(true);
    expect(canReviewEvidence("uploader", "draft")).toBe(false);
    expect(canManageEconomics("uploader", "draft")).toBe(false);
  });

  it("locks evidence mutations after internal-review handoff until a reopen", () => {
    expect(isEvidenceMutable("evidence_review")).toBe(true);
    for (const status of [
      "ready_for_internal_review",
      "internal_packet_approved",
      "exported",
      "archived",
    ] as const) {
      expect(canUploadDocuments("admin", status)).toBe(false);
      expect(canReviewEvidence("admin", status)).toBe(false);
      expect(canManageEconomics("admin", status)).toBe(false);
    }
  });
});
