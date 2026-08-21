import { describe, expect, it } from "vitest";
import { securityHeaders } from "../../next.config";

describe("Next.js response security headers", () => {
  it("denies framing and disables unnecessary browser capabilities", () => {
    const headers = Object.fromEntries(securityHeaders.map(({ key, value }) => [key, value]));

    const policy = headers["Content-Security-Policy"];
    expect(policy).toContain("default-src 'self'");
    expect(policy).toContain("connect-src 'self' blob: http://127.0.0.1:8000");
    expect(policy).toContain("img-src 'self' data: blob:");
    expect(policy).toContain("worker-src 'self' blob:");
    expect(policy).toContain("frame-src 'none'");
    expect(policy).toContain("frame-ancestors 'none'");
    expect(policy).toContain("form-action 'self'");
    expect(headers["X-Frame-Options"]).toBe("DENY");
    expect(headers["X-Content-Type-Options"]).toBe("nosniff");
    expect(headers["Referrer-Policy"]).toBe("no-referrer");
    expect(headers["Permissions-Policy"]).toBe(
      "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    );
  });
});
