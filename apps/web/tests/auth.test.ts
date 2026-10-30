// @vitest-environment node
import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { forward } from "@/lib/proxy";

const claims = vi.hoisted(() => ({ value: null as Record<string, unknown> | null }));

vi.mock("@supabase/ssr", () => ({
  createServerClient: () => ({
    auth: { getClaims: async () => ({ data: claims.value ? { claims: claims.value } : null, error: null }) },
  }),
  createBrowserClient: () => ({}),
}));

const { proxy } = await import("@/proxy");

function upstreamOk() {
  return vi.fn(async () => Response.json({ ok: true }));
}

function sentAuthorization(fetchImpl: ReturnType<typeof upstreamOk>): string | null {
  const [, init] = fetchImpl.mock.calls[0] as unknown as [URL, RequestInit];
  return new Headers(init.headers).get("authorization");
}

describe("sign-in redirect", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_SUPABASE_URL", "https://ref.supabase.co");
    vi.stubEnv("NEXT_PUBLIC_SUPABASE_ANON_KEY", "anon-key");
    claims.value = null;
  });
  afterEach(() => vi.unstubAllEnvs());

  it("sends signed-out visitors to /login, keeping where they were going", async () => {
    const response = await proxy(new NextRequest("http://localhost:3100/repositories/abc?tab=graph"));
    expect(response.status).toBe(307);
    const location = new URL(response.headers.get("location")!);
    expect(location.pathname).toBe("/login");
    expect(location.searchParams.get("next")).toBe("/repositories/abc?tab=graph");
  });

  it("lets signed-out visitors reach /login, the callback and the API proxy", async () => {
    for (const path of ["/login", "/auth/callback?code=x", "/api/repositories"]) {
      const response = await proxy(new NextRequest(`http://localhost:3100${path}`));
      expect(response.headers.get("location"), path).toBeNull();
    }
  });

  it("lets signed-in visitors through", async () => {
    claims.value = { sub: "user-1" };
    const response = await proxy(new NextRequest("http://localhost:3100/queue"));
    expect(response.headers.get("location")).toBeNull();
  });

  it("does nothing without Supabase configured", async () => {
    vi.unstubAllEnvs();
    vi.stubEnv("NEXT_PUBLIC_SUPABASE_URL", "");
    const response = await proxy(new NextRequest("http://localhost:3100/queue"));
    expect(response.headers.get("location")).toBeNull();
  });
});

describe("forward in user mode", () => {
  const config = { apiBaseUrl: "http://api.internal:8100", apiToken: "server-secret", userMode: true };

  it("sends the user's access token on reads and writes, never the static token", async () => {
    const read = upstreamOk();
    await forward(new Request("http://localhost:3100/api/jobs"), ["jobs"], { ...config, accessToken: "user-jwt" }, read);
    expect(sentAuthorization(read)).toBe("Bearer user-jwt");

    const write = upstreamOk();
    const post = new Request("http://localhost:3100/api/repositories", {
      method: "POST",
      headers: { "content-type": "application/json", origin: "http://localhost:3100", host: "localhost:3100" },
      body: "{}",
    });
    await forward(post, ["repositories"], { ...config, accessToken: "user-jwt" }, write);
    expect(sentAuthorization(write)).toBe("Bearer user-jwt");
  });

  it("sends no token when signed out, and passes the API's 401 through", async () => {
    const fetchImpl = vi.fn(async () => Response.json({ error: { code: "unauthorized" } }, { status: 401 }));
    const post = new Request("http://localhost:3100/api/repositories", {
      method: "POST",
      headers: { "content-type": "application/json", origin: "http://localhost:3100", host: "localhost:3100" },
      body: "{}",
    });
    const response = await forward(post, ["repositories"], config, fetchImpl);
    expect(sentAuthorization(fetchImpl as never)).toBeNull();
    expect(response.status).toBe(401);
    expect((await response.json()).error.code).toBe("unauthorized");
  });
});
