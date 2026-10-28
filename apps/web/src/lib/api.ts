/** Typed client for the RepoLens API (via the same-origin `/api` proxy). */

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId: string | null,
    readonly details: unknown = null,
  ) {
    super(message);
  }
}

interface ErrorEnvelope {
  error?: { code?: string; message?: string; request_id?: string | null; details?: unknown };
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const method = (init.method ?? "GET").toUpperCase();
  const mutating = method !== "GET" && method !== "HEAD";
  // The proxy only forwards same-origin JSON mutations (CSRF protection), so every mutation
  // is sent as JSON, with an empty object when there is nothing else to send.
  const body = mutating && init.body === undefined ? "{}" : init.body;
  if (mutating && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  const response = await fetch(`/api${path}`, { ...init, body, headers, cache: "no-store" });
  if (!response.ok) {
    let envelope: ErrorEnvelope = {};
    try {
      envelope = (await response.json()) as ErrorEnvelope;
    } catch {
      // Non-JSON error body; fall through with generic details.
    }
    throw new ApiError(
      response.status,
      envelope.error?.code ?? "http_error",
      envelope.error?.message ?? `Request failed with status ${response.status}`,
      envelope.error?.request_id ?? response.headers.get("x-request-id"),
      envelope.error?.details ?? null,
    );
  }
  return (await response.json()) as T;
}

export interface Health {
  status: "ok";
  version: string;
  /** The API refuses every mutation (REPOLENS_READ_ONLY). */
  read_only: boolean;
  /**
   * Test runs and sandboxed collection are available (false where the host has no Docker, in
   * which case those requests fail with 409 ``sandbox_unavailable``). Absent on older APIs.
   */
  sandbox?: boolean;
}

export interface ReadinessCheck {
  name: string;
  ok: boolean;
  detail: string | null;
}

export interface Readiness {
  status: "ready" | "not_ready";
  checks: ReadinessCheck[];
}

export function postJson<T>(path: string, payload: unknown = {}): Promise<T> {
  return apiFetch<T>(path, { method: "POST", body: JSON.stringify(payload) });
}
