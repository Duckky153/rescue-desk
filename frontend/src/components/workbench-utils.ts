import type { CaseStatus } from "@/types/api";

const MONEY_PATTERN = /^(?:(?:0|[1-9]\d*)|(?:[1-9]\d{0,2}(?:,\d{3})+))(?:\.(\d+))?$/;
export const MAX_MINOR_UNITS = 9_000_000_000_000_000n;

export type MinorUnitValue = bigint | number | string;

export const CURRENCY_EXPONENTS = {
  AUD: 2,
  BHD: 3,
  CAD: 2,
  CHF: 2,
  CLP: 0,
  EUR: 2,
  GBP: 2,
  INR: 2,
  JOD: 3,
  JPY: 0,
  KRW: 0,
  KWD: 3,
  OMR: 3,
  TND: 3,
  USD: 2,
  VND: 0,
} as const;

export type SupportedCurrency = keyof typeof CURRENCY_EXPONENTS;

export function currencyExponent(currency: string): number {
  const normalized = currency.toUpperCase();
  if (!(normalized in CURRENCY_EXPONENTS)) throw new Error("Choose a supported currency");
  return CURRENCY_EXPONENTS[normalized as SupportedCurrency];
}

export function parseMoneyToMinor(value: string, currency = "USD"): number {
  const input = value.trim();
  const match = MONEY_PATTERN.exec(input);
  const exponent = currencyExponent(currency);
  if (!match) throw new Error("Enter a non-negative decimal amount");
  const normalized = input.replaceAll(",", "");
  const [whole, fraction = ""] = normalized.split(".");
  if (fraction.length > exponent) {
    throw new Error(`Enter no more than ${exponent} decimal places for ${currency.toUpperCase()}`);
  }
  const scale = 10n ** BigInt(exponent);
  const minor = BigInt(whole) * scale + BigInt(fraction.padEnd(exponent, "0") || "0");
  if (minor > MAX_MINOR_UNITS) throw new Error("Amount is too large");
  return Number(minor);
}

export function localDateInputValue(date = new Date()): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export function evidenceUsesOnlySupersededDocuments(
  evidence: Array<{ document_id: string }>,
  documents: Array<{ id: string; superseded: boolean }>,
): boolean {
  const referencedDocumentIds = evidence.map((item) => item.document_id);
  return (
    referencedDocumentIds.length > 0 &&
    referencedDocumentIds.every((documentId) =>
      documents.some((document) => document.id === documentId && document.superseded),
    )
  );
}

export function evidenceHasActiveDocument(
  evidence: Array<{ document_id: string }>,
  documents: Array<{ id: string; superseded: boolean }>,
): boolean {
  return evidence.some((span) =>
    documents.some((document) => document.id === span.document_id && !document.superseded),
  );
}

export function minorUnitsBigInt(value: MinorUnitValue): bigint {
  let minor: bigint;
  if (typeof value === "bigint") {
    minor = value;
  } else if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) throw new Error("Minor units must be a safe integer");
    minor = BigInt(value);
  } else {
    if (!/^-?(?:0|[1-9]\d*)$/.test(value)) {
      throw new Error("Minor units must be an integer");
    }
    minor = BigInt(value);
  }
  if (minor > MAX_MINOR_UNITS || minor < -MAX_MINOR_UNITS) {
    throw new Error("Minor units exceed the supported range");
  }
  return minor;
}

export function formatMinor(amountMinor: MinorUnitValue, currency: string): string {
  try {
    const exponent = currencyExponent(currency);
    const minor = minorUnitsBigInt(amountMinor);
    const negative = minor < 0n;
    const absoluteMinor = negative ? -minor : minor;
    const scale = 10n ** BigInt(exponent);
    const whole = absoluteMinor / scale;
    const fraction = (absoluteMinor % scale).toString().padStart(exponent, "0");
    const formatter = new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: currency.toUpperCase(),
      minimumFractionDigits: 0,
      maximumFractionDigits: 0,
    });
    const parts = formatter.formatToParts(whole);
    const finalIntegerPart = parts.findLastIndex((part) => part.type === "integer");
    const exactAmount = parts
      .map((part, index) =>
        index === finalIntegerPart && exponent > 0 ? `${part.value}.${fraction}` : part.value,
      )
      .join("");
    return negative ? `-${exactAmount}` : exactAmount;
  } catch {
    return `${currency} ${amountMinor} minor units`;
  }
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "Not provided";
  const date = new Date(value.includes("T") ? value : `${value}T00:00:00`);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(date);
}

export function formatDateTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}

export function humanize(value: string): string {
  const spaced = value.replaceAll("_", " ").replaceAll(".", " · ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

export const WORKFLOW: Array<{ status: CaseStatus; label: string }> = [
  { status: "draft", label: "Intake" },
  { status: "evidence_review", label: "Evidence" },
  { status: "ready_for_internal_review", label: "Internal review" },
  { status: "internal_packet_approved", label: "Approved" },
  { status: "exported", label: "Exported" },
];

export function workflowPosition(status: CaseStatus): number {
  if (status === "archived") return -1;
  return WORKFLOW.findIndex((step) => step.status === status);
}

export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

export function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return "The request could not be completed";
}
