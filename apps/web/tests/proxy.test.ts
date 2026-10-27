import { describe, expect, it, vi } from "vitest";

import { forward, isSameOrigin } from "@/lib/proxy";

const config = { apiBaseUrl: "http://api.internal:8100", apiToken: "server-secret" };

function upstreamOk() {
  return vi.fn(async () => Response.json({ ok: true }, { headers: { "x-request-id": "r1" } }));
}

describe("forward", () => {
  it("forwards GET without attaching the token", async () => {
    const fetchImpl = upstreamOk();
    const request = new Request("http://localhost:3100/api/repositories?page=2");
    const response = await forward(request, ["repositories"], config, fetchImpl);
    expect(response.status).toBe(200);
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [URL, RequestInit];
    expect(url.toString()).toBe("http://api.internal:8100/api/repositories?page=2");
    expect(new Headers(init.headers).get("authorization")).toBeNull();
    expect(response.headers.get("x-request-id")).toBe("r1");
  });

  it("attaches the token to same-origin JSON mutations", async () => {
    const fetchImpl = upstreamOk();
    const request = new Request("http://localhost:3100/api/repositories", {
      method: "POST",
      headers: { "content-type": "application/json", origin: "http://localhost:3100", host: "localhost:3100" },
      body: JSON.stringify({ url: "https://github.com/a/b" }),
    });
    const response = await forward(request, ["repositories"], config, fetchImpl);
    expect(response.status).toBe(200);
    const [, init] = fetchImpl.mock.calls[0] as unknown as [URL, RequestInit];
    expect(new Headers(init.headers).get("authorization")).toBe("Bearer server-secret");
  });

  it("rejects cross-origin mutations", async () => {
    const fetchImpl = upstreamOk();
    const request = new Request("http://localhost:3100/api/repositories", {
      method: "POST",
      headers: { "content-type": "application/json", origin: "https://evil.example", host: "localhost:3100" },
      body: "{}",
    });
    const response = await forward(request, ["repositories"], config, fetchImpl);
    expect(response.status).toBe(403);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("rejects non-JSON mutations (form-based CSRF)", async () => {
    const fetchImpl = upstreamOk();
    const request = new Request("http://localhost:3100/api/repositories", {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded", origin: "http://localhost:3100", host: "localhost:3100" },
      body: "url=x",
    });
    expect((await forward(request, ["repositories"], config, fetchImpl)).status).toBe(415);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("rejects path traversal segments", async () => {
    const fetchImpl = upstreamOk();
    const request = new Request("http://localhost:3100/api/x");
    expect((await forward(request, ["..", "admin"], config, fetchImpl)).status).toBe(400);
    expect((await forward(request, ["a%2Fb"], config, fetchImpl)).status).toBe(400);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("maps upstream connection failure to 502", async () => {
    const fetchImpl = vi.fn(async () => {
      throw new TypeError("fetch failed");
    });
    const response = await forward(new Request("http://localhost:3100/api/health"), ["health"], config, fetchImpl);
    expect(response.status).toBe(502);
    expect((await response.json()).error.code).toBe("api_unreachable");
  });
});

describe("isSameOrigin", () => {
  it("trusts sec-fetch-site when present", () => {
    const request = new Request("http://localhost:3100/api/x", {
      headers: { "sec-fetch-site": "cross-site", origin: "http://localhost:3100", host: "localhost:3100" },
    });
    expect(isSameOrigin(request)).toBe(false);
  });

  it("ignores X-Forwarded-Host unless trusted", () => {
    const headers = { origin: "http://evil.example", host: "localhost:3100", "x-forwarded-host": "evil.example" };
    const request = () => new Request("http://localhost:3100/api/x", { method: "POST", headers });
    expect(isSameOrigin(request())).toBe(false);
    expect(isSameOrigin(request(), true)).toBe(true);
  });

  it("accepts a same-origin request behind a proxy that forwards Host unchanged", () => {
    // Vercel passes the public Host through and also sets X-Forwarded-Host to it, so the
    // default (untrusted) check already matches Origin.
    const headers = {
      origin: "https://repolens.vercel.app",
      host: "repolens.vercel.app",
      "x-forwarded-host": "repolens.vercel.app",
      "sec-fetch-site": "same-origin",
    };
    expect(isSameOrigin(new Request("https://repolens.vercel.app/api/x", { method: "POST", headers }))).toBe(true);
  });

  it("rejects a foreign Origin even when X-Forwarded-Host is spoofed to match it", () => {
    const headers = { origin: "https://evil.example", host: "repolens.vercel.app", "x-forwarded-host": "evil.example" };
    expect(isSameOrigin(new Request("https://repolens.vercel.app/api/x", { method: "POST", headers }))).toBe(false);
  });

  it("rejects requests without any origin information", () => {
    expect(isSameOrigin(new Request("http://localhost:3100/api/x", { method: "POST" }))).toBe(false);
  });
});
