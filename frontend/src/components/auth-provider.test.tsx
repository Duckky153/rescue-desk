import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthProvider, RequireAuth } from "@/components/auth-provider";
import { clearPendingIntents } from "@/lib/api";

const { replace } = vi.hoisted(() => ({ replace: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
}));

const currentUser = {
  id: "analyst-1",
  email: "analyst@example.test",
  display_name: "Analyst",
  organization_id: "org-1",
  role: "analyst",
};

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function seedSavedSession(): void {
  window.sessionStorage.setItem("rescuedesk.token", "same-user-token");
  window.sessionStorage.setItem("rescuedesk.intent.pending-fee", "same-fee-receipt");
}

function renderProtectedContent(): void {
  render(
    <AuthProvider>
      <RequireAuth>
        <div>Protected workbench</div>
      </RequireAuth>
    </AuthProvider>,
  );
}

describe("authentication restoration", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    clearPendingIntents();
    replace.mockReset();
    vi.restoreAllMocks();
  });
  afterEach(cleanup);

  it("preserves the same-user token and idempotency receipt across a network outage and retries", async () => {
    seedSavedSession();
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("network unavailable"))
      .mockResolvedValueOnce(jsonResponse(200, currentUser));
    vi.stubGlobal("fetch", fetchMock);

    renderProtectedContent();

    expect(await screen.findByRole("alert")).toHaveTextContent(/session check interrupted/i);
    expect(screen.getByRole("button", { name: "Retry session check" })).toBeVisible();
    expect(window.sessionStorage.getItem("rescuedesk.token")).toBe("same-user-token");
    expect(window.sessionStorage.getItem("rescuedesk.intent.pending-fee")).toBe(
      "same-fee-receipt",
    );
    expect(replace).not.toHaveBeenCalledWith("/login");

    fireEvent.click(screen.getByRole("button", { name: "Retry session check" }));
    expect(await screen.findByText("Protected workbench")).toBeVisible();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(window.sessionStorage.getItem("rescuedesk.intent.pending-fee")).toBe(
      "same-fee-receipt",
    );
  });

  it("preserves the same-user token and idempotency receipt across a retryable 5xx", async () => {
    seedSavedSession();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse(503, { detail: "Temporary database failure" })),
    );

    renderProtectedContent();

    expect(await screen.findByRole("alert")).toHaveTextContent(/temporary database failure/i);
    expect(window.sessionStorage.getItem("rescuedesk.token")).toBe("same-user-token");
    expect(window.sessionStorage.getItem("rescuedesk.intent.pending-fee")).toBe(
      "same-fee-receipt",
    );
    expect(replace).not.toHaveBeenCalledWith("/login");
  });

  it.each([401, 403])("clears credentials and pending receipts after definitive HTTP %s", async (status) => {
    seedSavedSession();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse(status, { detail: "Session is no longer valid" })),
    );

    renderProtectedContent();

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
    expect(window.sessionStorage.getItem("rescuedesk.token")).toBeNull();
    expect(window.sessionStorage.getItem("rescuedesk.intent.pending-fee")).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry session check" })).not.toBeInTheDocument();
  });
});
