import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  apiRequest,
  clearPendingIntents,
  stableIntentFingerprint,
  storeToken,
} from "@/lib/api";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function requestKey(fetchMock: ReturnType<typeof vi.fn>, call: number): string {
  const init = fetchMock.mock.calls[call]?.[1] as RequestInit;
  return new Headers(init.headers).get("Idempotency-Key") ?? "";
}

describe("idempotent API intents", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    clearPendingIntents();
    vi.restoreAllMocks();
  });

  it("reuses one persisted key after network response loss and rotates after success", async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("network response lost"))
      .mockResolvedValueOnce(jsonResponse(201, { id: "case-1" }))
      .mockResolvedValueOnce(jsonResponse(201, { id: "case-2" }));
    vi.stubGlobal("fetch", fetchMock);
    const intent = { scope: "create-case", fingerprint: '{"display_name":"Synthetic"}' };

    await expect(
      apiRequest("/v1/cases", { method: "POST", body: "{}" }, { auth: false, intent }),
    ).rejects.toThrow(/network response lost/i);
    await expect(
      apiRequest("/v1/cases", { method: "POST", body: "{}" }, { auth: false, intent }),
    ).resolves.toEqual({ id: "case-1" });

    expect(requestKey(fetchMock, 0)).toBeTruthy();
    expect(requestKey(fetchMock, 1)).toBe(requestKey(fetchMock, 0));

    await apiRequest("/v1/cases", { method: "POST", body: "{}" }, { auth: false, intent });
    expect(requestKey(fetchMock, 2)).not.toBe(requestKey(fetchMock, 1));
  });

  it("retains the key for 5xx and in-progress conflicts but clears it for definitive conflicts", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(503, { detail: "Temporary database failure" }))
      .mockResolvedValueOnce(
        jsonResponse(409, { detail: "An identical request is still being processed" }),
      )
      .mockResolvedValueOnce(
        jsonResponse(409, {
          detail: { message: "Case changed; refresh before retrying", current_version: 8 },
        }),
      )
      .mockResolvedValueOnce(jsonResponse(201, { id: "case-2" }));
    vi.stubGlobal("fetch", fetchMock);
    const intent = { scope: "case-transition", fingerprint: "case-1-to-review" };

    for (let call = 0; call < 3; call += 1) {
      await expect(
        apiRequest("/v1/cases/case-1/transitions", { method: "POST", body: "{}" }, {
          auth: false,
          intent,
        }),
      ).rejects.toBeInstanceOf(ApiError);
    }
    expect(requestKey(fetchMock, 1)).toBe(requestKey(fetchMock, 0));
    expect(requestKey(fetchMock, 2)).toBe(requestKey(fetchMock, 0));

    await apiRequest("/v1/cases/case-1/transitions", { method: "POST", body: "{}" }, {
      auth: false,
      intent,
    });
    expect(requestKey(fetchMock, 3)).not.toBe(requestKey(fetchMock, 2));
  });

  it("surfaces structured conflict messages and useful metadata safely", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(409, {
        detail: {
          message: "Assertion changed; refresh before retrying",
          current_version: 4,
          current_status: "evidence_review",
        },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const error = await apiRequest("/v1/assertion", {}, { auth: false }).catch(
      (caught: unknown) => caught,
    );
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 409,
      metadata: {
        message: "Assertion changed; refresh before retrying",
        current_version: 4,
        current_status: "evidence_review",
      },
    });
    expect((error as Error).message).toContain("Assertion changed; refresh before retrying");
    expect((error as Error).message).toContain("current version: 4");
    expect((error as Error).message).toContain("current status: evidence_review");
  });

  it("canonicalizes equivalent intent objects independent of key order", () => {
    expect(stableIntentFingerprint({ b: 2, a: { d: 4, c: 3 } })).toBe(
      stableIntentFingerprint({ a: { c: 3, d: 4 }, b: 2 }),
    );
  });

  it("does not carry pending mutation intents into a different authenticated session", () => {
    window.sessionStorage.setItem("rescuedesk.intent.pending", "fee-key");
    storeToken("first-user-token");
    expect(window.sessionStorage.getItem("rescuedesk.intent.pending")).toBeNull();

    window.sessionStorage.setItem("rescuedesk.intent.pending", "transition-key");
    storeToken("second-user-token");
    expect(window.sessionStorage.getItem("rescuedesk.intent.pending")).toBeNull();
  });
});
