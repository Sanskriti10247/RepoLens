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
  if (init.body !== undefined && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  const response = await fetch(`/api${path}`, { ...init, headers, cache: "no-store" });
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

export interface ReadinessCheck {
  name: string;
  ok: boolean;
  detail: string | null;
}

export interface Readiness {
  status: "ready" | "not_ready";
  checks: ReadinessCheck[];
}
