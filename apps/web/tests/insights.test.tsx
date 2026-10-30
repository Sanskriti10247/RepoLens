import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ExplorePage from "@/app/repositories/[id]/explore/page";
import InsightsPage from "@/app/repositories/[id]/insights/page";
import { HealthCard } from "@/components/HealthCard";
import { evidenceHref, Histogram, StackedBar } from "@/components/Insights";
import type { Analysis, Insights } from "@/lib/types";
import { resetReadOnlyCache } from "@/lib/useReadOnly";

import CLICK from "./fixtures/insights-click.json";

const search = { value: new URLSearchParams("analysis=a1") };

vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "r1" }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/repositories/r1/insights",
  useSearchParams: () => search.value,
}));

const REPORT = CLICK as unknown as Insights;

function clone(): Insights {
  return structuredClone(REPORT);
}

function analysis(overrides: Partial<Analysis> = {}): Analysis {
  return {
    id: "a1",
    repository_id: "r1",
    status: "RUNNING",
    requested_ref: "HEAD",
    resolved_ref: "refs/heads/main",
    resolved_ref_kind: "BRANCH",
    reused_run_id: null,
    results_run_id: "a1",
    parser_version: "p",
    engine_version: "1",
    config_fingerprint: "c",
    counts: null,
    timings_ms: null,
    error: null,
    created_at: new Date().toISOString(),
    started_at: new Date().toISOString(),
    finished_at: null,
    snapshot: null,
    job: null,
    progress: null,
    ...overrides,
  };
}

const NOT_READY = { error: { code: "analysis_not_ready", message: "Analysis is not finished" } };

/** Answer the report request with ``insights`` and the analysis request with ``state``. */
function serve(insights: Response | (() => Response), state: Analysis = analysis()) {
  const fetch = vi.fn(async (url: string) => {
    if (url.endsWith("/insights")) return typeof insights === "function" ? insights() : insights.clone();
    if (url.startsWith("/api/analyses/")) return Response.json(state);
    return Response.json({ status: "ok", version: "0", read_only: false, sandbox: true });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

beforeEach(() => {
  resetReadOnlyCache();
  search.value = new URLSearchParams("analysis=a1");
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({ matches: query.includes("reduce"), media: query, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
  );
});
afterEach(() => vi.unstubAllGlobals());

describe("health report page", () => {
  it("shows the grade, the weighted components and the method", async () => {
    serve(Response.json(REPORT));
    render(<InsightsPage />);
    expect(await screen.findByRole("img", { name: "Health score 71 out of 100, grade C" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: /Grade C\./ })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Complexity: 80 of 100" })).toBeInTheDocument();
    expect(screen.getByText("29 of 675 functions above complexity 10 (5 above 20)")).toBeInTheDocument();
    const method = screen.getByRole("link", { name: /How the score is worked out/ });
    expect(method).toHaveAttribute("href", "https://github.com/Sanskriti10247/RepoLens/blob/main/docs/insights.md");
  });

  it("groups findings by severity and links evidence into the explorer", async () => {
    serve(Response.json(REPORT));
    render(<InsightsPage />);
    const strip = await screen.findByRole("list", { name: "Findings by severity" });
    expect(within(strip).getByText("high").closest("li")).toHaveTextContent("1 high");
    expect(within(strip).getByText("medium").closest("li")).toHaveTextContent("3 medium");
    const evidence = screen.getByRole("list", { name: "Evidence for: 5 functions are very complex (21+)" });
    expect(within(evidence).getByRole("link", { name: "click.core.Option.get_help_extra (35)" })).toHaveAttribute(
      "href",
      "/repositories/r1/explore?symbol=404",
    );
    // Evidence with no location is shown, not linked.
    const resolution = screen.getByRole("list", { name: /Evidence for: Only 64%/ });
    expect(within(resolution).queryByRole("link")).toBeNull();
    expect(within(resolution).getByText("1911 unresolved")).toBeInTheDocument();
  });

  it("calls test reach what it is, not coverage", async () => {
    serve(Response.json(REPORT));
    render(<InsightsPage />);
    expect(await screen.findByText("Modules referenced by tests")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Source modules referenced by tests: 58.6%" })).toBeInTheDocument();
    expect(screen.getByText(/This is not line coverage/)).toBeInTheDocument();
    expect(screen.queryByText(/test coverage/i)).toBeNull();
  });

  it("highlights modules in import cycles and shows the dead code caveat", async () => {
    serve(Response.json(REPORT));
    render(<InsightsPage />);
    const cycleRow = (await screen.findAllByText("click._compat")).find((el) => el.closest("tr"));
    expect(cycleRow?.closest("tr")).toHaveClass("in-cycle");
    expect(screen.getByText(REPORT.dead_code.note)).toBeInTheDocument();
    expect(screen.getByText("Every function and class is referenced from somewhere in the graph.")).toBeInTheDocument();
  });

  it("explains how the resolution rate is counted", async () => {
    serve(Response.json(REPORT));
    render(<InsightsPage />);
    const term = await screen.findByRole("button", { name: "How this is counted" });
    const tip = document.getElementById(term.getAttribute("aria-describedby")!);
    expect(tip).toHaveTextContent("external and left out");
    expect(screen.getByText("Dynamic")).toBeInTheDocument();
  });

  it("says when there is not enough data to grade", async () => {
    const report = clone();
    report.score.overall = null;
    report.score.grade = null;
    for (const c of report.score.components) c.score = null;
    serve(Response.json(report));
    render(<InsightsPage />);
    expect(await screen.findByRole("img", { name: "No health score: not enough data" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: /Not enough data to grade/ })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Complexity: not enough data" })).toBeInTheDocument();
  });

  it("notes when complexity was not measured", async () => {
    const report = clone();
    report.complexity.available = false;
    report.score.components = report.score.components.filter((c) => c.key !== "complexity");
    serve(Response.json(report));
    render(<InsightsPage />);
    expect(await screen.findByText(/parsed before RepoLens measured complexity/)).toBeInTheDocument();
    expect(screen.getByText(/the other weights are scaled up/)).toBeInTheDocument();
  });

  it("waits on the live pipeline while the analysis runs", async () => {
    serve(Response.json(NOT_READY, { status: 409 }), analysis({ status: "RUNNING" }));
    render(<InsightsPage />);
    expect(await screen.findByText(/This one is still running/)).toBeInTheDocument();
    expect(await screen.findByRole("list", { name: "Analysis stages" })).toBeInTheDocument();
  });

  it("does not wait on an analysis that failed", async () => {
    serve(Response.json(NOT_READY, { status: 409 }), analysis({ status: "FAILED", finished_at: new Date().toISOString() }));
    render(<InsightsPage />);
    expect(await screen.findByText(/The analysis did not finish/)).toBeInTheDocument();
    expect(screen.queryByText(/This one is still running/)).toBeNull();
  });

  it("loads the report once a running analysis finishes", async () => {
    let ready = false;
    const fetch = serve(
      () => (ready ? Response.json(REPORT) : Response.json(NOT_READY, { status: 409 })),
      analysis({ status: "SUCCEEDED", finished_at: new Date().toISOString() }),
    );
    ready = false;
    render(<InsightsPage />);
    ready = true;
    expect(await screen.findByRole("img", { name: "Health score 71 out of 100, grade C" })).toBeInTheDocument();
    expect(fetch.mock.calls.filter(([url]) => String(url).endsWith("/insights")).length).toBeGreaterThan(1);
  });

  it("keeps quiet on an API without reports", async () => {
    serve(Response.json({ error: { code: "not_found", message: "Not Found" } }, { status: 404 }));
    render(<InsightsPage />);
    expect(await screen.findByText(/does not produce health reports yet/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("uses the latest analysis when none is named", async () => {
    search.value = new URLSearchParams();
    const fetch = vi.fn(async (url: string) => {
      if (url.includes("/analyses?")) {
        return Response.json({ items: [{ id: "a9", status: "SUCCEEDED" }], page: { total: 1, limit: 20, offset: 0 } });
      }
      return Response.json(REPORT);
    });
    vi.stubGlobal("fetch", fetch);
    render(<InsightsPage />);
    await screen.findByRole("img", { name: "Health score 71 out of 100, grade C" });
    expect(fetch.mock.calls.some(([url]) => url === "/api/analyses/a9/insights")).toBe(true);
  });
});

describe("health report while a newer analysis runs", () => {
  it("keeps the last finished report and says a newer one is running", async () => {
    search.value = new URLSearchParams();
    const fetch = vi.fn(async (url: string) => {
      if (url.includes("/analyses?")) {
        return Response.json({
          items: [
            { id: "a2", status: "RUNNING", requested_ref: "main" },
            { id: "a1", status: "SUCCEEDED", requested_ref: "main" },
          ],
          page: { total: 2, limit: 20, offset: 0 },
        });
      }
      return Response.json(REPORT);
    });
    vi.stubGlobal("fetch", fetch);
    render(<InsightsPage />);
    await screen.findByRole("img", { name: "Health score 71 out of 100, grade C" });
    expect(fetch.mock.calls.some(([url]) => url === "/api/analyses/a1/insights")).toBe(true);
    expect(screen.getByText(/A newer analysis of/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Watch it run" })).toHaveAttribute("href", "/repositories/r1/insights?analysis=a2");
  });
});

describe("HealthCard", () => {
  it("links the score to the full report", async () => {
    serve(Response.json(REPORT));
    render(<HealthCard analysisId="a1" repoId="r1" />);
    const link = await screen.findByRole("link", { name: "Health report: grade C, 71 of 100. Open the full report" });
    expect(link).toHaveAttribute("href", "/repositories/r1/insights?analysis=a1");
  });

  it("stays out of the way when the API has no reports", async () => {
    const fetch = serve(Response.json({ error: { code: "not_found", message: "Not Found" } }, { status: 404 }));
    const { container } = render(<HealthCard analysisId="a1" repoId="r1" />);
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });
});

describe("report charts", () => {
  it("labels each histogram bucket with its count and share", () => {
    render(<Histogram buckets={REPORT.complexity.distribution} unit="functions" />);
    expect(screen.getByLabelText("Complexity 21+: 5 functions (<1%)")).toBeInTheDocument();
    expect(screen.getByLabelText("Complexity 1-5: 581 functions (86%)")).toBeInTheDocument();
    expect(screen.getByText((_, el) => el?.textContent === "n = 675")).toBeInTheDocument();
  });

  it("folds the smallest parts of a stack into Other", () => {
    render(
      <StackedBar
        label="Files by language"
        unit="files"
        parts={REPORT.overview.languages.map((l) => ({ key: l.language, label: l.language, value: l.files }))}
      />,
    );
    expect(screen.getByText("Other (shell, ini)")).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(6);
  });

  it("links symbols to the explorer and bare files to the source view", () => {
    expect(evidenceHref("r1", { path: "a.py", symbol_id: 7, file_id: 3, line: 4 })).toBe("/repositories/r1/explore?symbol=7");
    expect(evidenceHref("r1", { path: "a.py", symbol_id: null, file_id: 3, line: 4 })).toBe(
      "/repositories/r1/explore?file=3&line=4",
    );
    expect(evidenceHref("r1", { path: null, symbol_id: null, file_id: null, line: null })).toBeNull();
  });
});

describe("explorer file view", () => {
  it("opens a file from a report link with the line marked", async () => {
    search.value = new URLSearchParams("file=3&line=2");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url.includes("/files/3/source")) {
          return Response.json({ file_id: 3, path: "pkg/a.py", language: "python", commit_sha: "abc1234def", blob_sha: "b", content: "x = 1\ny = 2\n" });
        }
        return Response.json({ items: [], page: { total: 0, limit: 50, offset: 0 }, commit_sha: "abc1234def" });
      }),
    );
    render(<ExplorePage />);
    expect(await screen.findByRole("heading", { level: 2, name: "pkg/a.py" })).toBeInTheDocument();
    expect(document.querySelector('tr[data-line="2"]')).toHaveClass("marked");
    expect(document.querySelector('tr[data-line="1"]')).not.toHaveClass("marked");
  });
});
