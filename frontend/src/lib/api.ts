const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";
const INTENT_STORAGE_PREFIX = "rescuedesk.intent.";

export interface IdempotentIntent {
  scope: string;
  fingerprint: string;
}

function detailMetadata(detail: unknown): Record<string, unknown> | null {
  if (!detail || typeof detail !== "object" || Array.isArray(detail)) return null;
  return { ...(detail as Record<string, unknown>) };
}

function detailMessage(status: number, detail: unknown): string {
  if (typeof detail === "string" && detail.trim()) return detail;
  const metadata = detailMetadata(detail);
  const base =
    typeof metadata?.message === "string" && metadata.message.trim()
      ? metadata.message
      : `Request failed with status ${status}`;
  if (!metadata) return base;
  const context = Object.entries(metadata)
    .filter(([key, value]) => key !== "message" && ["string", "number", "boolean"].includes(typeof value))
    .slice(0, 4)
    .map(([key, value]) => `${key.replaceAll("_", " ")}: ${String(value).slice(0, 120)}`)
    .join(", ");
  return context ? `${base} (${context})` : base;
}

export class ApiError extends Error {
  public readonly metadata: Record<string, unknown> | null;

  constructor(
    public readonly status: number,
    public readonly detail: unknown,
  ) {
    super(detailMessage(status, detail));
    this.name = "ApiError";
    this.metadata = detailMetadata(detail);
  }
}

export function getStoredToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.sessionStorage.getItem("rescuedesk.token");
}

export function storeToken(token: string): void {
  const previous = window.sessionStorage.getItem("rescuedesk.token");
  if (previous !== token) clearPendingIntents();
  window.sessionStorage.setItem("rescuedesk.token", token);
}

export function clearToken(): void {
  window.sessionStorage.removeItem("rescuedesk.token");
  clearPendingIntents();
}

export function idempotencyKey(prefix: string): string {
  return `${prefix}-${crypto.randomUUID()}`;
}

export function stableIntentFingerprint(value: unknown): string {
  if (value === null || typeof value !== "object") {
    if (typeof value === "bigint") return JSON.stringify({ $bigint: value.toString() });
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map((item) => stableIntentFingerprint(item)).join(",")}]`;
  }
  return `{${Object.entries(value as Record<string, unknown>)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, item]) => `${JSON.stringify(key)}:${stableIntentFingerprint(item)}`)
    .join(",")}}`;
}

export async function sha256Hex(value: string | ArrayBuffer): Promise<string> {
  const bytes = typeof value === "string" ? new TextEncoder().encode(value) : new Uint8Array(value);
  if (globalThis.crypto?.subtle) {
    const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
    return [...new Uint8Array(digest)].map((item) => item.toString(16).padStart(2, "0")).join("");
  }
  // Exact, collision-free fallback for constrained test/runtime environments without Web Crypto.
  return [...bytes].map((item) => item.toString(16).padStart(2, "0")).join("");
}

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null;
  }
}

const inMemoryIntents = new Map<string, string>();

function readIntent(storageKey: string): string | null {
  return storage()?.getItem(storageKey) ?? inMemoryIntents.get(storageKey) ?? null;
}

function writeIntent(storageKey: string, key: string): void {
  try {
    storage()?.setItem(storageKey, key);
  } catch {
    // The in-memory copy still preserves retries within this page lifecycle.
  }
  inMemoryIntents.set(storageKey, key);
}

function clearIntent(storageKey: string, expectedKey: string): void {
  if (readIntent(storageKey) !== expectedKey) return;
  try {
    storage()?.removeItem(storageKey);
  } catch {
    // The in-memory copy is cleared below.
  }
  inMemoryIntents.delete(storageKey);
}

export function clearPendingIntents(): void {
  const target = storage();
  if (target) {
    for (let index = target.length - 1; index >= 0; index -= 1) {
      const key = target.key(index);
      if (key?.startsWith(INTENT_STORAGE_PREFIX)) target.removeItem(key);
    }
  }
  inMemoryIntents.clear();
}

function isAmbiguousFailure(error: unknown): boolean {
  if (!(error instanceof ApiError)) return true;
  if ([408, 425, 429].includes(error.status) || error.status >= 500) return true;
  return (
    error.status === 409 &&
    typeof error.detail === "string" &&
    error.detail.toLowerCase().includes("still being processed")
  );
}

export async function withIdempotentIntent<T>(
  intent: IdempotentIntent,
  operation: (key: string) => Promise<T>,
): Promise<T> {
  const storageHash = await sha256Hex(`${intent.scope}\u0000${intent.fingerprint}`);
  const storageKey = `${INTENT_STORAGE_PREFIX}${storageHash}`;
  const key = readIntent(storageKey) ?? idempotencyKey(intent.scope);
  writeIntent(storageKey, key);
  try {
    const result = await operation(key);
    clearIntent(storageKey, key);
    return result;
  } catch (error) {
    if (!isAmbiguousFailure(error)) clearIntent(storageKey, key);
    throw error;
  }
}

export async function apiRequest<T>(
  path: string,
  init: RequestInit = {},
  options: { auth?: boolean; idempotencyKey?: string; intent?: IdempotentIntent } = {},
): Promise<T> {
  if (options.intent && options.idempotencyKey) {
    throw new Error("Choose either an intent or an explicit idempotency key");
  }
  if (options.intent) {
    return withIdempotentIntent(options.intent, (key) =>
      apiRequest<T>(path, init, { ...options, intent: undefined, idempotencyKey: key }),
    );
  }
  const headers = new Headers(init.headers);
  if (!(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  if (options.auth !== false) {
    const token = getStoredToken();
    if (!token) throw new ApiError(401, "Sign in required");
    headers.set("Authorization", `Bearer ${token}`);
  }
  if (options.idempotencyKey) headers.set("Idempotency-Key", options.idempotencyKey);
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers, cache: "no-store" });
  if (!response.ok) {
    let detail: unknown = response.statusText;
    try {
      const body = (await response.json()) as { detail?: unknown };
      detail = body.detail ?? body;
    } catch {
      // Preserve the HTTP status text when the response is intentionally not JSON.
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export async function apiDownload(path: string): Promise<Blob> {
  const token = getStoredToken();
  if (!token) throw new ApiError(401, "Sign in required");
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
  });
  if (!response.ok) throw new ApiError(response.status, response.statusText);
  return response.blob();
}
