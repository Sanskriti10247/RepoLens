import { render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import RunsPage from "@/app/repositories/[id]/runs/page";
import TestPlanPage from "@/app/test-plans/[id]/page";
import { formatClock, formatRelative, repoSlug } from "@/lib/format";
import { resetReadOnlyCache, useCapabilities } from "@/lib/useReadOnly";

vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "p1" }),
  useRouter: () => ({ push: vi.fn() }),
}));

const PLAN = {
  id: "p1",
  change_set_id: "c1",
  analysis_run_id: "a1",
  policy: "CONSERVATIVE",
  budget: null,
  scope: "TARGETED",
  fallback_reason: null,
  total_tests: 6,
  selected_tests: 3,
  estimated_seconds: null,
  budget_limited: false,
  evidence_sources: { static_graph: true },
  created_at: "2026-10-27T15:05:14Z",
};

const EMPTY_PAGE = { items: [], page: { total: 0, limit: 100, offset: 0 } };

function stubApi(health: Record<string, unknown>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/health") return Response.json({ status: "ok", version: "0", ...health });
      if (url === "/api/test-plans/p1") return Response.json(PLAN);
      if (url === "/api/impact/c1") return Response.json({ id: "c1", repository_id: "r1" });
      return Response.json(EMPTY_PAGE);
    }),
  );
}

beforeEach(() => resetReadOnlyCache());
afterEach(() => vi.unstubAllGlobals());

describe("sandbox capability", () => {
  it("hides the run button and explains why when the instance has no sandbox", async () => {
    stubApi({ read_only: false, sandbox: false });
    render(<TestPlanPage />);
    expect(await screen.findByRole("note")).toHaveTextContent(/needs the Docker sandbox/);
    expect(screen.queryByRole("button", { name: /run 3 selected tests/i })).toBeNull();
    expect(screen.queryByLabelText(/collect coverage/i)).toBeNull();
  });

  it("offers the run when the sandbox is available", async () => {
    stubApi({ read_only: false, sandbox: true });
    render(<TestPlanPage />);
    const button = await screen.findByRole("button", { name: /run 3 selected tests/i });
    await waitFor(() => expect(button).toBeEnabled());
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("shows only the read-only notice on a read-only instance without a sandbox", async () => {
    stubApi({ read_only: true, sandbox: false });
    render(<TestPlanPage />);
    expect(await screen.findByRole("note")).toHaveTextContent(/read-only demo/);
    expect(screen.getAllByRole("note")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: /run/i })).toBeNull();
  });

  it("tells the runs page that tests cannot run here", async () => {
    stubApi({ read_only: false, sandbox: false });
    render(<RunsPage />);
    expect(await screen.findByRole("note")).toHaveTextContent(/no Docker sandbox/);
    expect(await screen.findByText("No test runs recorded for this repository.")).toBeInTheDocument();
  });

  it("treats an API without the field as having a sandbox", async () => {
    stubApi({ read_only: false });
    const { result } = renderHook(() => useCapabilities());
    await waitFor(() => expect(result.current).toEqual({ readOnly: false, sandbox: true }));
  });

  it("reports nothing until the API answers", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => undefined)));
    const { result } = renderHook(() => useCapabilities());
    expect(result.current).toEqual({ readOnly: null, sandbox: null });
  });
});

describe("list formatting", () => {
  const now = Date.parse("2026-10-28T12:00:00Z");

  it("formats relative times", () => {
    expect(formatRelative("2026-10-28T11:59:40Z", now)).toBe("just now");
    expect(formatRelative("2026-10-28T11:48:00Z", now)).toBe("12 min ago");
    expect(formatRelative("2026-10-28T07:00:00Z", now)).toBe("5 h ago");
    expect(formatRelative("2026-10-27T10:00:00Z", now)).toBe("yesterday");
    expect(formatRelative("2026-10-24T12:00:00Z", now)).toBe("4 days ago");
    expect(formatRelative("2026-08-02T12:00:00Z", now)).toBe("2 Aug");
    expect(formatRelative(null, now)).toBe("—");
  });

  it("formats elapsed time as a clock", () => {
    expect(formatClock(7_000)).toBe("0:07");
    expect(formatClock(282_000)).toBe("4:42");
    expect(formatClock(3_729_000)).toBe("1:02:09");
  });

  it("shortens repository URLs to owner/name", () => {
    expect(repoSlug("https://github.com/mahmoud/boltons")).toBe("mahmoud/boltons");
    expect(repoSlug("https://github.com/a/b.git")).toBe("a/b");
    expect(repoSlug("file:///tmp/fixture")).toBe("file:///tmp/fixture");
  });
});
