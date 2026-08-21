import { describe, expect, it } from "vitest";
import {
  formatMinor,
  humanize,
  localDateInputValue,
  parseMoneyToMinor,
  workflowPosition,
} from "@/components/workbench-utils";

describe("workbench money helpers", () => {
  it("converts display amounts into exact integer minor units", () => {
    expect(parseMoneyToMinor("12,000.45")).toBe(1_200_045);
    expect(parseMoneyToMinor("0.1")).toBe(10);
    expect(parseMoneyToMinor("5")).toBe(500);
  });

  it("rejects ambiguous, negative, and unsafe amounts", () => {
    expect(() => parseMoneyToMinor("1.001")).toThrow(/2 decimal places/i);
    expect(() => parseMoneyToMinor("-4.00")).toThrow(/non-negative/i);
    expect(() => parseMoneyToMinor("90071992547409.92")).toThrow(/too large/i);
    expect(() => parseMoneyToMinor("1,00")).toThrow(/non-negative decimal amount/i);
    expect(() => parseMoneyToMinor("12,34.56")).toThrow(/non-negative decimal amount/i);
  });

  it("derives date inputs from local calendar fields rather than UTC rollover", () => {
    expect(localDateInputValue(new Date(2026, 7, 21, 23, 45))).toBe("2026-08-21");
  });

  it("formats currency and workflow labels for human review", () => {
    expect(formatMinor(120_045, "USD")).toBe("$1,200.45");
    expect(parseMoneyToMinor("120000", "JPY")).toBe(120_000);
    expect(parseMoneyToMinor("1.234", "KWD")).toBe(1_234);
    expect(formatMinor(120_000, "JPY")).toBe("¥120,000");
    expect(() => parseMoneyToMinor("1.00", "ZZZ")).toThrow(/supported currency/i);
    expect(humanize("contract_end_date")).toBe("Contract end date");
    expect(workflowPosition("internal_packet_approved")).toBe(3);
    expect(workflowPosition("archived")).toBe(-1);
  });

  it("formats the full supported integer range without floating-point division", () => {
    expect(formatMinor(8_999_999_999_999_999n, "USD")).toBe(
      "$89,999,999,999,999.99",
    );
    expect(formatMinor("9000000000000000", "JPY")).toBe("¥9,000,000,000,000,000");
    expect(formatMinor(8_999_999_999_999_999n, "KWD")).toBe(
      "KWD\u00a08,999,999,999,999.999",
    );
    expect(formatMinor(-123_456n, "USD")).toBe("-$1,234.56");
  });

  it("refuses unsafe or out-of-range minor-unit inputs", () => {
    expect(() => formatMinor(Number.MAX_SAFE_INTEGER + 1, "USD")).not.toThrow();
    expect(formatMinor(Number.MAX_SAFE_INTEGER + 1, "USD")).toContain("minor units");
    expect(formatMinor(9_000_000_000_000_001n, "USD")).toContain("minor units");
  });
});
