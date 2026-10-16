/**
 * Server-side forwarding of `/api/*` to the RepoLens API.
 *
 * The API token lives only in the Next.js server environment and is attached here, so it
 * never reaches the browser. Because the proxy therefore acts with the operator's authority,
 * state-changing requests must be same-origin JSON requests (CSRF protection): cross-site
 * forms cannot send `application/json`, and the Origin check rejects cross-site fetches.
 */

const SAFE_METHODS = new Set(["GET", "HEAD"]);
const SEGMENT = /^[A-Za-z0-9._~-]+$/;
const FORWARDED_REQUEST_HEADERS = ["content-type", "accept", "x-request-id"];
const FORWARDED_RESPONSE_HEADERS = ["content-type", "x-request-id", "cache-control"];

export interface ProxyConfig {
  apiBaseUrl: string;
  apiToken: string | undefined;
  /** Honour X-Forwarded-Host. Enable only behind a reverse proxy that sets it. */
  trustForwardedHost?: boolean;
}

export function proxyConfigFromEnv(): ProxyConfig {
  return {
    apiBaseUrl: process.env.REPOLENS_API_URL ?? "http://127.0.0.1:8100",
    apiToken: process.env.REPOLENS_API_TOKEN || undefined,
    trustForwardedHost: process.env.REPOLENS_TRUST_FORWARDED_HOST === "true",
  };
}

function errorResponse(status: number, code: string, message: string): Response {
  return Response.json({ error: { code, message, request_id: null, details: null } }, { status });
}

export function isSameOrigin(request: Request, trustForwardedHost = false): boolean {
  const origin = request.headers.get("origin");
  const fetchSite = request.headers.get("sec-fetch-site");
  if (fetchSite && fetchSite !== "same-origin") return false;
  if (!origin) return fetchSite === "same-origin";
  const forwarded = trustForwardedHost ? request.headers.get("x-forwarded-host") : null;
  const host = forwarded ?? request.headers.get("host");
  try {
    return new URL(origin).host === host;
  } catch {
    return false;
  }
}

export async function forward(
  request: Request,
  path: string[],
  config: ProxyConfig,
  fetchImpl: typeof fetch = fetch,
): Promise<Response> {
  if (path.length === 0 || !path.every((segment) => SEGMENT.test(segment) && segment !== "..")) {
    return errorResponse(400, "invalid_path", "Invalid API path");
  }
  const method = request.method.toUpperCase();
  const mutating = !SAFE_METHODS.has(method);
  if (mutating) {
    if (!isSameOrigin(request, config.trustForwardedHost)) {
      return errorResponse(403, "cross_origin_blocked", "Cross-origin request rejected");
    }
    const contentType = request.headers.get("content-type") ?? "";
    if (method !== "DELETE" && !contentType.startsWith("application/json")) {
      return errorResponse(415, "unsupported_media_type", "Expected application/json");
    }
  }

  const target = new URL(`/api/${path.join("/")}`, config.apiBaseUrl);
  target.search = new URL(request.url).search;
  const headers = new Headers();
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  if (mutating && config.apiToken) headers.set("authorization", `Bearer ${config.apiToken}`);

  let upstream: Response;
  try {
    upstream = await fetchImpl(target, {
      method,
      headers,
      body: mutating && method !== "DELETE" ? await request.text() : undefined,
      cache: "no-store",
      redirect: "manual",
    });
  } catch {
    return errorResponse(502, "api_unreachable", "RepoLens API is unreachable");
  }
  const responseHeaders = new Headers();
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }
  return new Response(upstream.body, { status: upstream.status, headers: responseHeaders });
}
