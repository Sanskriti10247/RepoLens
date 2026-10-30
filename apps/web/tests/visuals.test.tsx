import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import AskPage from "@/app/repositories/[id]/ask/page";
import { AnalysisPipeline, stageDurations } from "@/components/AnalysisPipeline";
import { GraphCharts } from "@/components/GraphCharts";
import { ModuleArchitecture, ModuleGraphView } from "@/components/ModuleGraph";
import { RetrievalTrace, evidencePlaces } from "@/components/RetrievalTrace";
import { SystemDiagram } from "@/components/SystemDiagram";
import { ApiError } from "@/lib/api";
import { autoGroups, components, expandGroups, groupGraph, grouper, layoutGraph, neighbourhood, shortestPath } from "@/lib/moduleLayout";
import type { Analysis, AnalysisProgress, ModuleGraph, QuestionAnswer, Stats } from "@/lib/types";
import { resetReadOnlyCache } from "@/lib/useReadOnly";

vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "r1" }),
  useRouter: () => ({ push: vi.fn() }),
}));

/** Make ``prefers-reduced-motion`` report ``reduced``. */
function setReducedMotion(reduced: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      matches: reduced && query.includes("reduce"),
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  );
}

beforeEach(() => {
  resetReadOnlyCache();
  setReducedMotion(true);
});
afterEach(() => vi.unstubAllGlobals());

const STAGES = ["fetch", "snapshot", "parse", "resolve", "persist", "tests"];

function progress(overrides: Partial<AnalysisProgress> = {}): AnalysisProgress {
  return {
    stage: "parse",
    stage_index: 2,
    stage_count: 6,
    stages: STAGES,
    files_done: 120,
    files_total: 480,
    current: "pkg/core/models.py",
    detail: null,
    stage_started_at: new Date().toISOString(),
    durations_ms: { fetch: 820, snapshot: 310 },
    updated_at: new Date().toISOString(),
    ...overrides,
  };
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
    progress: progress(),
    ...overrides,
  };
}

function stage(name: string): HTMLElement {
  const list = screen.getByRole("list", { name: "Analysis stages" });
  const item = within(list).getByText(name).closest("li");
  if (!item) throw new Error(`no stage ${name}`);
  return item;
}

describe("AnalysisPipeline", () => {
  it("marks finished, active and pending stages from the progress report", () => {
    render(<AnalysisPipeline analysis={analysis()} />);
    expect(stage("Fetch")).toHaveClass("is-done");
    expect(stage("Snapshot")).toHaveClass("is-done");
    expect(stage("Parse")).toHaveClass("is-active");
    expect(stage("Resolve")).toHaveClass("is-pending");
    expect(within(stage("Fetch")).getByText("820 ms")).toBeInTheDocument();
    expect(screen.getByText("stage 3 of 6")).toBeInTheDocument();
  });

  it("shows the live counter and the file being read", () => {
    render(<AnalysisPipeline analysis={analysis()} />);
    const bar = screen.getByRole("progressbar", { name: "Files processed in this stage" });
    expect(bar).toHaveAttribute("aria-valuenow", "120");
    expect(bar).toHaveAttribute("aria-valuemax", "480");
    expect(screen.getByText("120")).toBeInTheDocument(); // reduced motion: no count-up
    expect(screen.getByText("/ 480 files")).toBeInTheDocument();
    expect(screen.getByText("pkg/core/models.py")).toBeInTheDocument();
  });

  it("swaps the ticker when the worker reports a new file", () => {
    const { rerender } = render(<AnalysisPipeline analysis={analysis()} />);
    rerender(<AnalysisPipeline analysis={analysis({ progress: progress({ files_done: 121, current: "pkg/core/views.py" }) })} />);
    const incoming = screen.getByText("pkg/core/views.py");
    expect(incoming).toHaveClass("is-in");
    expect(screen.getByText("pkg/core/models.py")).toHaveClass("is-out");
  });

  it("does not invent progress before the first report", () => {
    render(<AnalysisPipeline analysis={analysis({ progress: null })} />);
    expect(screen.getByText(/waiting for the worker's first report/)).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).toBeNull();
    for (const name of ["Fetch", "Parse", "Tests"]) expect(stage(name)).toHaveClass("is-pending");
  });

  it("shows a queued run as waiting", () => {
    render(<AnalysisPipeline analysis={analysis({ status: "QUEUED", progress: null, started_at: null })} />);
    expect(screen.getByRole("status")).toHaveTextContent(/waiting for a worker/);
  });

  it("turns into a timeline of measured durations when it succeeds", () => {
    const done = analysis({
      status: "SUCCEEDED",
      progress: progress({ stage: "done", stage_index: 6, durations_ms: {} }),
      timings_ms: { fetch_ms: 1200, snapshot_ms: 300, analyze_ms: 2500 },
      counts: {
        python_files: 40,
        symbols: 512,
        references: 1900,
        tests_discovered: 12,
        edges_by_type: { CALLS: 300, IMPORTS: 90 },
        analysis_timings_ms: { parse_ms: 1500, resolve_ms: 400, persist_ms: 500, tests_ms: 100 },
      },
    });
    render(<AnalysisPipeline analysis={done} />);
    for (const name of STAGES) expect(stage(name[0]!.toUpperCase() + name.slice(1))).toHaveClass("is-done");
    expect(screen.getByText("4.0 s")).toBeInTheDocument(); // 1200 + 300 + 1500 + 400 + 500 + 100
    const timeline = screen.getByRole("img", { name: /Stage durations/ });
    expect(timeline.querySelectorAll(".timeline-seg")).toHaveLength(6);
    expect(screen.getByText("512 symbols, 390 edges")).toBeInTheDocument();
    expect(screen.getByText("12 tests")).toBeInTheDocument();
  });

  it("reads durations from timings first and the progress report second", () => {
    const durations = stageDurations(
      analysis({ timings_ms: { fetch_ms: 10 }, progress: progress({ durations_ms: { fetch: 99, snapshot: 20 } }) }),
    );
    expect(durations).toEqual({ fetch: 10, snapshot: 20 });
  });

  it("names the stage a failed run stopped in", () => {
    render(
      <AnalysisPipeline
        analysis={analysis({ status: "FAILED", progress: progress({ stage: "fetch", stage_index: 0 }) })}
      />,
    );
    expect(stage("Fetch")).toHaveClass("is-failed");
    expect(stage("Snapshot")).toHaveClass("is-pending");
    expect(screen.getByText("The analysis failed during fetch.")).toBeInTheDocument();
  });

  it("marks reduced motion so the stylesheet and scripts agree", () => {
    const { container, unmount } = render(<AnalysisPipeline analysis={analysis()} />);
    expect(container.querySelector(".pipeline")).toHaveAttribute("data-motion", "reduced");
    unmount();
    setReducedMotion(false);
    const again = render(<AnalysisPipeline analysis={analysis()} />);
    expect(again.container.querySelector(".pipeline")).toHaveAttribute("data-motion", "full");
  });
});

const GRAPH: ModuleGraph = {
  analysis_id: "a1",
  modules: [
    { id: 1, name: "shop.cli", path: "shop/cli.py" },
    { id: 2, name: "shop.orders", path: "shop/orders.py" },
    { id: 3, name: "shop.pricing", path: "shop/pricing.py" },
    { id: 4, name: "shop.tax", path: "shop/tax.py" },
  ],
  edges: [
    { src_module_id: 1, dst_module_id: 2, edge_type: "IMPORTS", edges: 2, references: 2 },
    { src_module_id: 2, dst_module_id: 3, edge_type: "IMPORTS", edges: 1, references: 1 },
    { src_module_id: 3, dst_module_id: 4, edge_type: "IMPORTS", edges: 1, references: 1 },
    { src_module_id: 4, dst_module_id: 3, edge_type: "IMPORTS", edges: 1, references: 1 },
    { src_module_id: 2, dst_module_id: 3, edge_type: "CALLS", edges: 5, references: 7 },
  ],
  import_cycles: [[3, 4]],
};

describe("module layout", () => {
  it("places importers above what they import and keeps cycles on one layer", () => {
    const grouped = groupGraph(GRAPH, grouper(GRAPH.modules, "module"), new Set(["IMPORTS"]));
    const layout = layoutGraph(grouped.nodes, grouped.edges);
    const y = Object.fromEntries(layout.nodes.map((n) => [n.key, n.y]));
    expect(y["shop.cli"]!).toBeLessThan(y["shop.orders"]!);
    expect(y["shop.orders"]!).toBeLessThan(y["shop.pricing"]!);
    expect(y["shop.pricing"]).toBe(y["shop.tax"]);
    expect(layout.edges.find((e) => e.key === "shop.pricing->shop.tax")?.cycle).toBe(true);
    expect(layout.edges.find((e) => e.key === "shop.cli->shop.orders")?.cycle).toBe(false);
  });

  it("merges modules into packages and sums their edges", () => {
    const grouped = groupGraph(GRAPH, grouper(GRAPH.modules, 1), null);
    expect(grouped.nodes.map((n) => n.key)).toEqual(["shop"]);
    expect(grouped.nodes[0]!.members).toHaveLength(4);
    expect(grouped.nodes[0]!.inCycle).toBe(true);
    expect(grouped.edges).toHaveLength(0); // edges inside a package are not drawn
  });

  it("finds strongly connected components", () => {
    const comp = components(["a", "b", "c"], [
      { src: "a", dst: "b" },
      { src: "b", dst: "a" },
      { src: "b", dst: "c" },
    ]);
    expect(comp.get("a")).toBe(comp.get("b"));
    expect(comp.get("c")).not.toBe(comp.get("a"));
  });

  it("opens large folders and keeps flat ones closed when grouping automatically", () => {
    const modules = [
      ...Array.from({ length: 30 }, (_, i) => ({ name: `app.part${i % 6}.mod${i}`, path: `src/app/part${i % 6}/mod${i}.py` })),
      ...Array.from({ length: 40 }, (_, i) => ({ name: `test_${i}`, path: `tests/test_${i}.py` })),
      ...Array.from({ length: 4 }, (_, i) => ({ name: `t.case${i}`, path: `tests/unit/case${i}.py` })),
      { name: "setup", path: "setup.py" },
    ];
    const keys = autoGroups(modules, 10);
    expect(new Set(keys.values())).toEqual(
      new Set(["src/app/part0/", "src/app/part1/", "src/app/part2/", "src/app/part3/", "src/app/part4/", "src/app/part5/", "tests/", "setup"]),
    );
    expect(autoGroups([{ name: "a.b", path: "a/b.py" }], 10).get("a.b")).toBe("a.b"); // small graphs show modules
  });
});

describe("graph interaction helpers", () => {
  const EDGES = [
    { key: "a->b", src: "a", dst: "b" },
    { key: "b->c", src: "b", dst: "c" },
    { key: "c->d", src: "c", dst: "d" },
    { key: "x->a", src: "x", dst: "a" },
  ];

  it("walks neighbours up to the given number of hops in both directions", () => {
    expect([...neighbourhood("b", EDGES, 1).nodes].sort()).toEqual(["a", "b", "c"]);
    const two = neighbourhood("b", EDGES, 2);
    expect([...two.nodes].sort()).toEqual(["a", "b", "c", "d", "x"]);
    expect(two.edges.has("x->a")).toBe(true);
  });

  it("finds the shortest directed path and nothing against the arrows", () => {
    expect(shortestPath("x", "d", EDGES)).toEqual(["x->a", "a->b", "b->c", "c->d"]);
    expect(shortestPath("d", "x", EDGES)).toBeNull();
    expect(shortestPath("a", "a", EDGES)).toEqual([]);
  });

  it("opens a package one level and remembers where each piece came from", () => {
    const { keyOf, parentOf } = expandGroups(GRAPH.modules, grouper(GRAPH.modules, 1), new Set(["shop"]));
    expect(GRAPH.modules.map((m) => keyOf(m.name))).toEqual(["shop.cli", "shop.orders", "shop.pricing", "shop.tax"]);
    expect(parentOf.get("shop.tax")).toBe("shop");
    const folders = [
      { id: 1, name: "pkg.a.x", path: "src/pkg/a/x.py" },
      { id: 2, name: "pkg.b", path: "src/pkg/b.py" },
    ];
    const opened = expandGroups(folders, () => "src/", new Set(["src/", "src/pkg/"]));
    expect(folders.map((m) => opened.keyOf(m.name))).toEqual(["src/pkg/a/", "pkg.b"]);
  });
});

describe("ModuleGraphView", () => {
  it("draws nodes, highlights the cycle and opens details for a selected module", () => {
    render(<ModuleGraphView graph={GRAPH} repoId="r1" />);
    const orders = screen.getByRole("button", { name: "shop.orders" });
    expect(screen.getByRole("button", { name: "shop.tax, in an import cycle" })).toHaveClass("is-cycle");
    expect(screen.getByText("1 import cycle")).toBeInTheDocument();
    fireEvent.click(orders);
    const details = screen.getByRole("complementary", { name: "Details for shop.orders" });
    expect(within(details).getByRole("link", { name: "Read the source" })).toHaveAttribute(
      "href",
      "/repositories/r1/explore?symbol=2",
    );
    expect(within(details).getByText("Depends on (1)")).toBeInTheDocument();
    expect(within(details).getByText("Used by (1)")).toBeInTheDocument();
    expect(orders).toHaveClass("is-focus");
    expect(screen.getByRole("button", { name: "shop.tax, in an import cycle" })).toHaveClass("is-dim");
  });

  it("switches the dependency type", () => {
    const { container } = render(<ModuleGraphView graph={GRAPH} repoId="r1" />);
    expect(container.querySelectorAll(".mg-edge")).toHaveLength(4);
    fireEvent.click(screen.getByRole("button", { name: "Calls" }));
    expect(container.querySelectorAll(".mg-edge")).toHaveLength(1);
  });

  it("finds a module, traces a path to a second node and keeps the view in the address bar", () => {
    window.history.replaceState(null, "", "/repositories/r1");
    const { container } = render(<ModuleGraphView graph={GRAPH} repoId="r1" />);
    fireEvent.change(screen.getByRole("combobox", { name: "Find a module" }), { target: { value: "shop.cli" } });
    fireEvent.submit(screen.getByRole("search"));
    expect(screen.getByRole("button", { name: "shop.cli" })).toHaveClass("is-focus");
    expect(new URLSearchParams(window.location.search).get("focus")).toBe("shop.cli");
    fireEvent.click(screen.getByRole("button", { name: "shop.tax, in an import cycle" }), { shiftKey: true });
    const route = screen.getByRole("list", { name: "Dependency path" });
    expect(within(route).getAllByRole("button").map((b) => b.textContent)).toEqual(["shop.cli", "shop.orders", "shop.pricing", "shop.tax"]);
    expect(container.querySelector('[data-key="shop.orders"]')).toHaveClass("is-path");
    expect(new URLSearchParams(window.location.search).get("to")).toBe("shop.tax");
    fireEvent.click(screen.getByRole("button", { name: "2 hops" }));
    expect(new URLSearchParams(window.location.search).get("hops")).toBe("2");
  });

  it("restores a shared view and expands and collapses a package", () => {
    window.history.replaceState(null, "", "/repositories/r1?group=1&focus=shop");
    render(<ModuleGraphView graph={GRAPH} repoId="r1" />);
    const details = screen.getByRole("complementary", { name: "Details for shop" });
    fireEvent.click(within(details).getByRole("button", { name: "Expand" }));
    expect(screen.getByRole("button", { name: "shop.orders" })).toBeInTheDocument();
    expect(new URLSearchParams(window.location.search).get("open")).toBe("shop");
    fireEvent.click(screen.getByRole("button", { name: "shop.orders" }));
    fireEvent.click(screen.getByRole("button", { name: "Collapse into shop" }));
    expect(screen.queryByRole("button", { name: "shop.orders" })).toBeNull();
    expect(screen.getByRole("button", { name: "shop, 4 modules, in an import cycle" })).toHaveClass("is-selected");
    window.history.replaceState(null, "", "/");
  });

  it("explains an analysis without modules", () => {
    render(<ModuleGraphView graph={{ ...GRAPH, modules: [], edges: [], import_cycles: [] }} repoId="r1" />);
    expect(screen.getByText(/No modules were recorded/)).toBeInTheDocument();
  });

  it("shows request errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({ error: { code: "not_found", message: "Analysis not found" } }, { status: 404 })),
    );
    render(<ModuleArchitecture analysisId="a1" repoId="r1" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Analysis not found");
  });
});

const STATS: Stats = {
  analysis_id: "a1",
  symbols_by_kind: { FUNCTION: 30, CLASS: 6, MODULE: 4 },
  edges_by_type: { CALLS: 50, IMPORTS: 9 },
  references_by_status: {},
  top_fan_in: [
    {
      symbol: {
        id: 7,
        qualified_name: "shop.pricing.with_tax",
        name: "with_tax",
        kind: "FUNCTION",
        visibility: "PUBLIC",
        path: "shop/pricing.py",
        file_id: 1,
        start_line: 1,
        start_col: 0,
        end_line: 3,
        end_col: 0,
        signature: null,
        doc_summary: null,
        is_test: false,
        has_parse_errors: false,
      },
      count: 4,
    },
  ],
  top_fan_out: [],
};

describe("GraphCharts", () => {
  it("charts the stats with exact values in the tooltips", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json(STATS)));
    render(<GraphCharts analysisId="a1" repoId="r1" />);
    const row = await screen.findByLabelText("Function: 30 symbols (75% of shown)");
    expect(row.querySelector(".chart-track span")).toHaveStyle({ transform: "scaleX(1)" });
    expect(screen.getByRole("link", { name: "shop.pricing.with_tax" })).toHaveAttribute(
      "href",
      "/repositories/r1/explore?symbol=7",
    );
    expect(screen.getByText("No resolved calls between symbols.")).toBeInTheDocument();
  });

  it("has an empty state", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ ...STATS, symbols_by_kind: {} })));
    render(<GraphCharts analysisId="a1" repoId="r1" />);
    expect(await screen.findByText(/recorded no symbols/)).toBeInTheDocument();
  });

  it("shows request errors", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ error: { code: "boom", message: "Exploded" } }, { status: 500 })));
    render(<GraphCharts analysisId="a1" repoId="r1" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Exploded");
  });
});

const ANSWER: QuestionAnswer = {
  question: "Who calls with_tax?",
  intent: "CALLERS",
  analysis_id: "a1",
  commit_sha: "0123456789abcdef0123",
  mode: "deterministic",
  answer_markdown: "**Callers**\n- `shop.orders.total` calls `shop.pricing.with_tax` [E2]",
  model_explanation: null,
  model_note: null,
  insufficient_evidence: false,
  matched_symbols: ["shop.pricing.with_tax", "shop.pricing.without_tax"],
  evidence: [
    {
      id: "E1",
      category: "SOURCE_FACT",
      statement: "`shop.pricing.with_tax` is defined",
      path: "shop/pricing.py",
      start_line: 4,
      end_line: 9,
      symbol_id: 7,
      qualified_name: "shop.pricing.with_tax",
      text: "def with_tax(x): ...",
      commit_sha: "0123456789abcdef0123",
    },
    {
      id: "E2",
      category: "STATIC_RELATIONSHIP",
      statement: "`shop.orders.total` calls `shop.pricing.with_tax`",
      path: "shop/orders.py",
      start_line: 12,
      end_line: 12,
      symbol_id: 8,
      qualified_name: "shop.orders.total",
      text: null,
      commit_sha: "0123456789abcdef0123",
    },
  ],
};

describe("RetrievalTrace", () => {
  it("shows only the request while it is in flight", () => {
    render(<RetrievalTrace state={{ kind: "pending", question: "q", since: Date.now() }} />);
    expect(screen.getByText("Asking the API")).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
  });

  it("reveals the recorded steps once the answer arrives", () => {
    render(<RetrievalTrace state={{ kind: "answered", answer: ANSWER }} />);
    expect(screen.getByText("Callers")).toBeInTheDocument();
    expect(screen.getByText("shop.pricing.with_tax")).toHaveClass("is-primary");
    expect(screen.getByText("shop/orders.py")).toBeInTheDocument();
    expect(screen.getByText("lines 4–9")).toBeInTheDocument();
    expect(screen.getByText("deterministic")).toBeInTheDocument();
  });

  it("reports a failed request without inventing steps", () => {
    const error = new ApiError(409, "not_analyzed", "No analysis", null);
    render(<RetrievalTrace state={{ kind: "failed", question: "q", error }} />);
    expect(screen.getByText("Request failed")).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
  });

  it("collects cited files and line ranges", () => {
    expect(evidencePlaces(ANSWER.evidence)).toEqual([
      { path: "shop/pricing.py", lines: "lines 4–9" },
      { path: "shop/orders.py", lines: "lines 12" },
    ]);
  });
});

describe("Ask page", () => {
  it("shows the pending request, then the steps the answer reports", async () => {
    let answer: (value: Response) => void = () => undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url === "/api/health") return Promise.resolve(Response.json({ status: "ok", version: "0", read_only: false }));
        return new Promise<Response>((resolve) => {
          answer = resolve;
        });
      }),
    );
    render(<AskPage />);
    fireEvent.change(screen.getByLabelText("Question"), { target: { value: "Who calls with_tax?" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Ask" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText("Asking the API")).toBeInTheDocument();
    await act(async () => answer(Response.json(ANSWER)));
    expect(await screen.findByText("Matched symbols")).toBeInTheDocument();
    expect(screen.queryByText("Asking the API")).toBeNull();
  });
});

describe("SystemDiagram", () => {
  it("dims the sandbox when the instance has none", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "ok", version: "0", read_only: false, sandbox: false })));
    render(<SystemDiagram />);
    const sandbox = await screen.findByRole("button", { name: "Sandbox, switched off on this instance" });
    expect(sandbox).toHaveClass("is-off");
    fireEvent.click(sandbox);
    expect(screen.getByText(/no Docker daemon, so the sandbox is switched off/)).toBeInTheDocument();
  });

  it("keeps the sandbox lit when the API has one", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "ok", version: "0", read_only: false, sandbox: true })));
    render(<SystemDiagram />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Sandbox" })).not.toHaveClass("is-off"));
  });

  it("switches paths on request and stays still under reduced motion", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => undefined)));
    const { container } = render(<SystemDiagram />);
    expect(container.querySelector(".sysdiagram")).toHaveAttribute("data-motion", "reduced");
    fireEvent.click(screen.getByRole("button", { name: "Answer a question" }));
    expect(screen.getByRole("button", { name: "Answer a question", pressed: true })).toBeInTheDocument();
    expect(screen.getByText(/retrieves evidence from the graph and source/)).toBeInTheDocument();
  });
});
