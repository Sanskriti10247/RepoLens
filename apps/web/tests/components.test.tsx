import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Markdownish } from "@/components/Markdownish";
import { SourceView } from "@/components/SourceView";
import { formatBytes, formatDuration, label, shortSha, toneFor } from "@/lib/format";
import { apiFetch } from "@/lib/api";

describe("Markdownish", () => {
  it("renders the answer subset without injecting HTML", () => {
    const { container } = render(
      <Markdownish text={"**Source facts**\n- `pkg.f` is defined [E1]\n- <img src=x onerror=alert(1)>"} />,
    );
    expect(screen.getByText("Source facts").tagName).toBe("STRONG");
    expect(screen.getByText("pkg.f").tagName).toBe("CODE");
    expect(screen.getByText("[E1]")).toHaveAttribute("href", "#E1");
    expect(container.querySelector("img")).toBeNull(); // repository text stays inert
    expect(container.textContent).toContain("<img src=x onerror=alert(1)>");
  });
});

describe("SourceView", () => {
  it("numbers lines and marks the highlighted span", () => {
    const { container } = render(
      <SourceView content={"a = 1\ndef f():\n    return 2\n"} highlight={{ start: 2, end: 3 }} />,
    );
    const marked = container.querySelectorAll("tr.marked");
    expect(marked).toHaveLength(2);
    expect(marked[0]?.textContent).toContain("def f():");
  });

  it("renders HTML-looking source as text", () => {
    const { container } = render(<SourceView content={'html = "<script>x()</script>"'} />);
    expect(container.querySelector("script")).toBeNull();
  });
});

describe("formatting", () => {
  it("formats values", () => {
    expect(shortSha("0123456789abcdef")).toBe("0123456789");
    expect(formatDuration(450)).toBe("450 ms");
    expect(formatDuration(2500)).toBe("2.5 s");
    expect(formatDuration(125_000)).toBe("2 min 5 s");
    expect(formatBytes(2048)).toBe("2.0 KB");
    expect(label("RESOURCE_LIMIT_EXCEEDED")).toBe("Resource limit exceeded");
    expect(toneFor("PASSED")).toBe("ok");
    expect(toneFor("INFRASTRUCTURE_ERROR")).toBe("bad");
    expect(toneFor("SOMETHING_NEW")).toBe("neutral");
  });
});

describe("apiFetch", () => {
  it("sends mutations as JSON even without a body (CSRF rule)", async () => {
    const fetchMock = vi.fn(async () => Response.json({}));
    vi.stubGlobal("fetch", fetchMock);
    await apiFetch("/test-runs/x/cancel", { method: "POST" });
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(new Headers(init.headers).get("content-type")).toBe("application/json");
    expect(init.body).toBe("{}");
    vi.unstubAllGlobals();
  });
});
