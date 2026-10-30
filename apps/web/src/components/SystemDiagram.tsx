"use client";

import { useEffect, useRef, useState, type CSSProperties } from "react";

import { useReducedMotion } from "@/lib/motion";
import { useCapabilities } from "@/lib/useReadOnly";

/**
 * How RepoLens is put together (README "Architecture", docs/architecture.md), as a diagram
 * that walks through the three main paths: analysing a repository, impact and test runs,
 * and questions. The sandbox node reflects the connected API: it is dimmed when
 * ``/api/health`` reports ``"sandbox": false``.
 */

type NodeId =
  | "web"
  | "api"
  | "plan"
  | "ask"
  | "model"
  | "pg"
  | "worker"
  | "github"
  | "mirror"
  | "parse"
  | "graph"
  | "impact"
  | "sandbox";

interface NodeSpec {
  title: string;
  sub: string;
  about: string;
  external?: boolean;
}

const NODES: Record<NodeId, NodeSpec> = {
  web: {
    title: "Web app",
    sub: "Next.js · /api proxy",
    about: "Renders what the API returns. Its server-side /api route forwards requests and adds the API token, so the token never reaches the browser.",
  },
  api: {
    title: "API",
    sub: "FastAPI · validates",
    about: "Validates requests, writes rows and enqueues jobs. It never fetches from the network and never runs Docker.",
  },
  plan: {
    title: "Test selection",
    sub: "plans from evidence",
    about: "Runs synchronously in the API: picks tests from the impact report, recording the evidence for every test it includes or leaves out.",
  },
  ask: {
    title: "Questions",
    sub: "evidence, citations",
    about: "Retrieves evidence deterministically from the graph, warnings and recorded runs, then composes an answer with citations.",
  },
  model: {
    title: "Model",
    sub: "optional, off",
    about: "An optional language model may phrase the answer from the same evidence. Analysis, impact and selection never depend on it.",
    external: true,
  },
  pg: {
    title: "PostgreSQL",
    sub: "snapshots · code graph · impact · plans · runs · job queue",
    about: "The single store for results and for the job queue. Workers claim jobs with SELECT … FOR UPDATE SKIP LOCKED and hold them with leases and heartbeats.",
  },
  worker: {
    title: "Worker",
    sub: "leases · heartbeats",
    about: "Does everything slow or untrusted: fetching, parsing, resolving, impact propagation and test runs. Retries with backoff and recovers abandoned jobs.",
  },
  github: {
    title: "GitHub",
    sub: "public, HTTPS",
    about: "Public repositories are fetched over HTTPS only.",
    external: true,
  },
  mirror: {
    title: "Git mirror",
    sub: "hardened fetch",
    about: "A bare mirror fetched with no hooks and no system or global config. The ref is resolved to a full SHA and the file inventory recorded as an immutable snapshot.",
  },
  parse: {
    title: "Parser",
    sub: "tree-sitter",
    about: "Extracts modules, classes, functions, imports, calls and base classes with exact source spans. Unchanged files come from the parse cache.",
  },
  graph: {
    title: "Code graph",
    sub: "resolve · bulk write",
    about: "Every reference gets an explicit status (resolved, ambiguous, external, unresolved or dynamic) and the typed edges are bulk-loaded into PostgreSQL.",
  },
  impact: {
    title: "Impact",
    sub: "diff → propagation",
    about: "Diffs two commits, maps changed lines to symbols and propagates the impact backwards through the graph, with a completeness verdict.",
  },
  sandbox: {
    title: "Sandbox",
    sub: "Docker · no network",
    about: "Selected tests run in a pinned image with no network, a non-root user, a read-only root and source, and memory, CPU, process and time limits.",
  },
};

type Point = [number, number];
interface Layout {
  width: number;
  height: number;
  nodeW: number;
  nodeH: number;
  showSub: boolean;
  at: Record<Exclude<NodeId, "pg">, Point>;
  bus: { x: number; y: number; w: number; h: number };
  /** Height of the worker node when it spans the job lanes. */
  workerH: number | null;
  lanes: { label: string; y: number; x: number }[];
  edges: Record<EdgeId, string>;
}

type EdgeId =
  | "web-api"
  | "api-plan"
  | "api-ask"
  | "ask-model"
  | "api-pg"
  | "plan-pg"
  | "ask-pg"
  | "pg-worker"
  | "github-mirror"
  | "worker-mirror"
  | "mirror-parse"
  | "parse-graph"
  | "graph-pg"
  | "worker-impact"
  | "impact-pg"
  | "worker-sandbox";

const WIDE: Layout = {
  width: 1040,
  height: 590,
  nodeW: 150,
  nodeH: 48,
  showSub: true,
  at: {
    web: [95, 78],
    api: [300, 78],
    plan: [510, 78],
    ask: [720, 78],
    model: [930, 78],
    worker: [110, 428],
    github: [330, 272],
    mirror: [330, 350],
    parse: [530, 350],
    graph: [730, 350],
    impact: [930, 445],
    sandbox: [530, 540],
  },
  bus: { x: 20, y: 172, w: 1000, h: 46 },
  workerH: 276,
  // Job kinds the worker runs, one lane each, labelled inside the worker node.
  lanes: [
    { label: "analyze repository", x: 176, y: 354 },
    { label: "analyze change", x: 176, y: 449 },
    { label: "run tests", x: 176, y: 544 },
  ],
  edges: {
    "web-api": "M170,78 L225,78",
    "api-plan": "M375,78 L435,78",
    "api-ask": "M300,54 C300,14 720,14 720,54",
    "ask-model": "M795,78 L855,78",
    "api-pg": "M300,102 L300,172",
    "plan-pg": "M510,102 L510,172",
    "ask-pg": "M720,102 L720,172",
    "pg-worker": "M110,218 L110,290",
    "github-mirror": "M330,296 L330,326",
    "worker-mirror": "M185,350 L255,350",
    "mirror-parse": "M405,350 L455,350",
    "parse-graph": "M605,350 L655,350",
    "graph-pg": "M730,326 L730,218",
    "worker-impact": "M185,445 L855,445",
    "impact-pg": "M930,421 L930,218",
    "worker-sandbox": "M185,540 L455,540",
  },
};

const TALL: Layout = {
  width: 360,
  height: 700,
  nodeW: 104,
  nodeH: 40,
  showSub: false,
  at: {
    web: [180, 34],
    api: [180, 106],
    plan: [80, 178],
    ask: [280, 178],
    model: [280, 246],
    worker: [180, 392],
    github: [64, 392],
    mirror: [70, 474],
    parse: [70, 548],
    graph: [70, 622],
    impact: [180, 474],
    sandbox: [290, 474],
  },
  bus: { x: 14, y: 300, w: 332, h: 40 },
  workerH: null,
  lanes: [],
  edges: {
    "web-api": "M180,54 L180,86",
    "api-plan": "M150,126 C150,150 80,140 80,158",
    "api-ask": "M210,126 C210,150 280,140 280,158",
    "ask-model": "M280,198 L280,226",
    "api-pg": "M180,126 L180,300",
    "plan-pg": "M80,198 L80,300",
    "ask-pg": "M250,266 L250,300",
    "pg-worker": "M180,340 L180,372",
    "github-mirror": "M64,412 L68,454",
    "worker-mirror": "M150,412 C150,440 100,430 100,454",
    "mirror-parse": "M70,494 L70,528",
    "parse-graph": "M70,568 L70,602",
    "graph-pg": "M18,622 C4,622 4,560 4,480 L4,340 C4,330 14,322 22,320",
    "worker-impact": "M180,412 L180,454",
    "impact-pg": "M232,474 C246,474 250,452 250,430 L250,340",
    "worker-sandbox": "M210,412 C210,440 290,430 290,454",
  },
};

type ScenarioId = "analysis" | "change" | "questions";

const SCENARIOS: Record<ScenarioId, { title: string; lede: string; nodes: NodeId[]; edges: EdgeId[] }> = {
  analysis: {
    title: "Analyze a repository",
    lede: "Register a repository: the API enqueues a job, a worker fetches the mirror, parses every Python file and writes the code graph.",
    nodes: ["web", "api", "pg", "worker", "github", "mirror", "parse", "graph"],
    edges: ["web-api", "api-pg", "pg-worker", "github-mirror", "worker-mirror", "mirror-parse", "parse-graph", "graph-pg"],
  },
  change: {
    title: "Change impact and tests",
    lede: "Compare two refs: a worker maps the diff to symbols and propagates impact; the API selects tests from it; a run job executes them in the sandbox.",
    nodes: ["web", "api", "plan", "pg", "worker", "impact", "sandbox"],
    edges: ["web-api", "api-pg", "api-plan", "plan-pg", "pg-worker", "worker-impact", "impact-pg", "worker-sandbox"],
  },
  questions: {
    title: "Answer a question",
    lede: "Ask in plain words: the API retrieves evidence from the graph and source and answers with citations; a model is optional.",
    nodes: ["web", "api", "ask", "pg", "model"],
    edges: ["web-api", "api-ask", "ask-pg", "ask-model"],
  },
};
const ORDER: ScenarioId[] = ["analysis", "change", "questions"];
const CYCLE_MS = 7000;

export function SystemDiagram() {
  const reduced = useReducedMotion();
  const { sandbox } = useCapabilities();
  const [narrow, setNarrow] = useState(false);
  const [scenario, setScenario] = useState<ScenarioId>("analysis");
  const [pinned, setPinned] = useState(false);
  const [about, setAbout] = useState<NodeId | null>(null);
  const box = useRef<HTMLDivElement | null>(null);

  // Pick the layout from the space the diagram actually has, not the window.
  useEffect(() => {
    const element = box.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => setNarrow((entry?.contentRect.width ?? 1000) < 620));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  // Walk through the paths on a timer until the visitor picks one.
  useEffect(() => {
    if (reduced || pinned) return;
    const timer = setInterval(() => {
      setScenario((s) => ORDER[(ORDER.indexOf(s) + 1) % ORDER.length]!);
    }, CYCLE_MS);
    return () => clearInterval(timer);
  }, [reduced, pinned]);

  const layout = narrow ? TALL : WIDE;
  const active = SCENARIOS[scenario];
  const sandboxOff = sandbox === false;
  const isOn = (id: NodeId) => active.nodes.includes(id);
  const shown = about ? NODES[about] : null;

  return (
    <div ref={box} className={`sysdiagram${narrow ? " is-tall" : ""}`} data-motion={reduced ? "reduced" : "full"}>
      <div className="segmented sys-tabs" role="group" aria-label="Path through the system">
        {ORDER.map((id) => (
          <button
            key={id}
            type="button"
            className={scenario === id ? "is-on" : undefined}
            aria-pressed={scenario === id}
            onClick={() => {
              setScenario(id);
              setPinned(true);
            }}
          >
            {SCENARIOS[id].title}
          </button>
        ))}
      </div>
      <p className="sys-lede" aria-live="polite">
        {active.lede}
      </p>
      <svg
        className="sys-svg"
        viewBox={`0 0 ${layout.width} ${layout.height}`}
        role="group"
        aria-labelledby="sys-title"
        key={narrow ? "tall" : "wide"}
      >
        <title id="sys-title">
          RepoLens architecture: web app, API, PostgreSQL with the job queue, workers, Git mirrors, parser, code graph,
          impact analysis and the Docker sandbox{sandboxOff ? " (switched off on this instance)" : ""}.
        </title>
        <defs>
          <marker id="sys-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerUnits="userSpaceOnUse" markerWidth="8" markerHeight="8" orient="auto">
            <path d="M0,0 L8,4 L0,8 z" className="sys-arrowhead" />
          </marker>
          <marker id="sys-arrow-lit" viewBox="0 0 8 8" refX="7" refY="4" markerUnits="userSpaceOnUse" markerWidth="8" markerHeight="8" orient="auto">
            <path d="M0,0 L8,4 L0,8 z" className="sys-arrowhead is-lit" />
          </marker>
        </defs>
        <g className="sys-edges">
          {(Object.keys(layout.edges) as EdgeId[]).map((id, i) => {
            const lit = active.edges.includes(id);
            const off = sandboxOff && id === "worker-sandbox";
            const optional = id === "ask-model";
            return (
              <g key={id} className={`sys-edge${lit ? " is-lit" : ""}${off ? " is-off" : ""}${optional ? " is-optional" : ""}`}>
                <path
                  d={layout.edges[id]}
                  className="sys-edge-base"
                  style={{ "--i": i } as CSSProperties}
                  markerEnd={lit && !off ? "url(#sys-arrow-lit)" : "url(#sys-arrow)"}
                />
                {lit && !off && (
                  <path
                    d={layout.edges[id]}
                    className="sys-edge-flow"
                    style={{ "--i": active.edges.indexOf(id) } as CSSProperties}
                  />
                )}
              </g>
            );
          })}
        </g>
        <g
          className={`sys-node sys-bus${isOn("pg") ? " is-lit" : ""}${about === "pg" ? " is-about" : ""}`}
          tabIndex={0}
          role="button"
          aria-label="PostgreSQL"
          onPointerEnter={() => setAbout("pg")}
          onFocus={() => setAbout("pg")}
          onClick={() => setAbout("pg")}
        >
          <rect x={layout.bus.x} y={layout.bus.y} width={layout.bus.w} height={layout.bus.h} rx={6} />
          <text x={layout.bus.x + 16} y={layout.bus.y + layout.bus.h / 2 + (layout.showSub ? -2 : 4)} className="sys-title">
            PostgreSQL
          </text>
          {layout.showSub && (
            <text x={layout.bus.x + 16} y={layout.bus.y + layout.bus.h / 2 + 13} className="sys-sub">
              {NODES.pg.sub}
            </text>
          )}
          {!layout.showSub && (
            <text x={layout.bus.x + layout.bus.w - 12} y={layout.bus.y + layout.bus.h / 2 + 4} className="sys-sub" textAnchor="end">
              data · job queue
            </text>
          )}
        </g>
        {(Object.keys(layout.at) as Exclude<NodeId, "pg">[]).map((id) => {
          const [x, y] = layout.at[id];
          const spec = NODES[id];
          const off = id === "sandbox" && sandboxOff;
          const step = active.nodes.indexOf(id);
          const w = id === "github" && narrow ? 80 : layout.nodeW;
          const h = id === "worker" && layout.workerH ? layout.workerH : layout.nodeH;
          const spans = h !== layout.nodeH;
          return (
            <g
              key={id}
              className={`sys-node${step >= 0 ? " is-lit" : ""}${spec.external ? " is-external" : ""}${off ? " is-off" : ""}${about === id ? " is-about" : ""}`}
              transform={`translate(${x}, ${y})`}
              style={{ "--step": Math.max(0, step) } as CSSProperties}
              tabIndex={0}
              role="button"
              aria-label={off ? `${spec.title}, switched off on this instance` : spec.title}
              onPointerEnter={() => setAbout(id)}
              onFocus={() => setAbout(id)}
              onClick={() => setAbout(id)}
            >
              <rect x={-w / 2} y={-h / 2} width={w} height={h} rx={5} />
              <text y={spans ? -h / 2 + 22 : layout.showSub ? -3 : 4} textAnchor="middle" className="sys-title">
                {spec.title}
              </text>
              {layout.showSub && (
                <text y={spans ? -h / 2 + 38 : 13} textAnchor="middle" className="sys-sub">
                  {off ? "off on this instance" : spec.sub}
                </text>
              )}
              {spans &&
                layout.lanes.map((lane) => (
                  <text key={lane.label} x={lane.x - x} y={lane.y - y} textAnchor="end" className="sys-lane">
                    {lane.label}
                  </text>
                ))}
              {off && !layout.showSub && (
                <text y={layout.nodeH / 2 + 13} textAnchor="middle" className="sys-sub sys-off-note">
                  off here
                </text>
              )}
            </g>
          );
        })}
      </svg>
      <p className="sys-caption" aria-live="polite">
        {shown ? (
          <>
            <strong>{shown.title}.</strong> {shown.about}
          </>
        ) : (
          "Point at or tap a part of the diagram to read what it does."
        )}
        {sandboxOff && (about === "sandbox" || scenario === "change") && (
          <span className="sys-off">
            {" "}
            This instance has no Docker daemon, so the sandbox is switched off: impact analysis and test plans work,
            test runs are refused.
          </span>
        )}
      </p>
    </div>
  );
}
