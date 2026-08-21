import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { WorkbenchAudit } from "@/components/workbench-audit";
import type { AuditEvent } from "@/types/api";

const events: AuditEvent[] = [
  {
    id: "event-1",
    actor_id: "actor-1",
    action: "case.created",
    object_type: "case",
    object_id: "case-1",
    correlation_id: "correlation-1",
    before: null,
    after: { display_name: "Synthetic matter" },
    previous_hash: null,
    event_hash: "a".repeat(64),
    created_at: "2026-08-21T12:00:00Z",
  },
  {
    id: "event-2",
    actor_id: "actor-1",
    action: "fee.created",
    object_type: "fee",
    object_id: "fee-1",
    correlation_id: "correlation-2",
    before: null,
    after: { amount_minor: 12345 },
    previous_hash: "a".repeat(64),
    event_hash: "b".repeat(64),
    created_at: "2026-08-21T12:01:00Z",
  },
];

describe("WorkbenchAudit", () => {
  it("labels hash fields as recorded metadata rather than browser verification", () => {
    render(<WorkbenchAudit events={events} />);

    expect(screen.getByText(/does not independently verify them/i)).toBeInTheDocument();
    expect(screen.getByText("No previous hash")).toBeInTheDocument();
    expect(screen.getByText("Previous hash recorded")).toBeInTheDocument();
    expect(screen.queryByText(/chain linked/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/verified/i)).not.toBeInTheDocument();
  });
});
