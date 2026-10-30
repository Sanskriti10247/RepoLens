"use client";

import { useEffect, useRef, useState, type CSSProperties } from "react";

import { formatClock, formatDuration, shortSha } from "@/lib/format";
import { useEasedNumber, useNow, useReducedMotion } from "@/lib/motion";
import type { Analysis, AnalysisProgress } from "@/lib/types";

/**
 * The analysis as a pipeline of the stages the worker reports (services/progress.py).
 *
 * While the run is queued or running, the active stage pulses, the connector into the next
 * stage flows, and the counter and ticker show what the worker last reported. Once it
 * finishes, the same row settles and a timeline of measured stage durations appears.
 * Everything shown comes from the analysis record; nothing is estimated.
 */

// Mirrors ANALYSIS_STAGES on the server; used only when a run has no progress report yet.
export const DEFAULT_STAGES = ["fetch", "snapshot", "parse", "resolve", "persist", "tests"];

const STAGE_INFO: Record<string, { title: string; doing: string; blurb: string }> = {
  fetch: { title: "Fetch", doing: "Fetching", blurb: "update the bare mirror, resolve the ref" },
  snapshot: { title: "Snapshot", doing: "Recording the snapshot", blurb: "list and hash every file" },
  parse: { title: "Parse", doing: "Parsing", blurb: "tree-sitter reads each Python file" },
  resolve: { title: "Resolve", doing: "Resolving names", blurb: "names become references and edges" },
  persist: { title: "Persist", doing: "Writing the graph", blurb: "symbols, references, edges" },
  tests: { title: "Tests", doing: "Finding tests", blurb: "pytest tests, found statically" },
};

type StageState = "done" | "active" | "pending" | "failed" | "stopped" | "reused";

export function stageTitle(stage: string): string {
  return STAGE_INFO[stage]?.title ?? stage.charAt(0).toUpperCase() + stage.slice(1);
}

function stageStates(analysis: Analysis): StageState[] {
  const progress = analysis.progress ?? null;
  const stages = progress?.stages ?? DEFAULT_STAGES;
  const index = progress ? progress.stage_index : -1;
  switch (analysis.status) {
    case "SUCCEEDED":
    case "PARTIAL":
      return stages.map(() => "done");
    case "REUSED":
      // Only fetching and the snapshot run; the graph belongs to the earlier analysis.
      return stages.map((s) => (s === "fetch" || s === "snapshot" ? "done" : "reused"));
    case "FAILED":
    case "CANCELLED":
      if (!progress) return stages.map(() => "pending");
      return stages.map((_, i) =>
        i < index ? "done" : i === index ? (analysis.status === "FAILED" ? "failed" : "stopped") : "pending",
      );
    case "RUNNING":
      if (!progress) return stages.map(() => "pending");
      return stages.map((_, i) => (i < index ? "done" : i === index ? "active" : "pending"));
    default:
      return stages.map(() => "pending");
  }
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** Measured wall time per stage: the run's recorded timings, then the progress report. */
export function stageDurations(analysis: Analysis): Record<string, number> {
  const timings = analysis.timings_ms ?? {};
  const inner = (analysis.counts?.analysis_timings_ms ?? {}) as Record<string, unknown>;
  const recorded: Record<string, number | null> = {
    fetch: num(timings.fetch_ms),
    snapshot: num(timings.snapshot_ms),
    parse: num(inner.parse_ms),
    resolve: num(inner.resolve_ms),
    persist: num(inner.persist_ms),
    tests: num(inner.tests_ms),
  };
  const reported = analysis.progress?.durations_ms ?? {};
  const out: Record<string, number> = {};
  for (const stage of analysis.progress?.stages ?? DEFAULT_STAGES) {
    const value = recorded[stage] ?? num(reported[stage]);
    if (value !== null) out[stage] = value;
  }
  return out;
}

export function AnalysisPipeline({ analysis }: { analysis: Analysis }) {
  const reduced = useReducedMotion();
  const progress = analysis.progress ?? null;
  const stages = progress?.stages ?? DEFAULT_STAGES;
  const states = stageStates(analysis);
  const durations = stageDurations(analysis);
  const terminal = !["QUEUED", "RUNNING"].includes(analysis.status);

  return (
    <div
      className={`pipeline pipeline-${analysis.status.toLowerCase()}`}
      data-motion={reduced ? "reduced" : "full"}
      style={{ "--stage-count": stages.length } as CSSProperties}
    >
      <ol className="pipe-stages" aria-label="Analysis stages">
        {stages.map((stage, i) => (
          <StageNode
            key={stage}
            stage={stage}
            state={states[i] ?? "pending"}
            last={i === stages.length - 1}
            order={i}
            duration={durations[stage]}
            progress={states[i] === "active" ? progress : null}
          />
        ))}
      </ol>
      {analysis.status === "QUEUED" && <QueuedPanel since={analysis.created_at} />}
      {analysis.status === "RUNNING" && <LivePanel analysis={analysis} />}
      {terminal && <Summary analysis={analysis} durations={durations} stages={stages} />}
    </div>
  );
}

function StageNode({
  stage,
  state,
  last,
  order,
  duration,
  progress,
}: {
  stage: string;
  state: StageState;
  last: boolean;
  order: number;
  duration: number | undefined;
  progress: AnalysisProgress | null;
}) {
  const now = useNow(1000);
  let note: string = STAGE_INFO[stage]?.blurb ?? "";
  if (state === "done" && duration !== undefined) note = formatDuration(duration);
  if (state === "active" && progress?.stage_started_at) {
    note = formatClock(now - new Date(progress.stage_started_at).getTime());
  }
  if (state === "failed") note = "stopped here";
  if (state === "stopped") note = "cancelled here";
  if (state === "reused") note = "reused";
  const spoken = { done: "done", active: "in progress", pending: "not started", failed: "failed", stopped: "cancelled", reused: "reused from an earlier analysis" }[state];
  return (
    <li className={`pipe-stage is-${state}`} style={{ "--order": order } as CSSProperties}>
      <span className="pipe-track" aria-hidden="true">
        <span className="pipe-node">
          {state === "done" && (
            <svg viewBox="0 0 16 16" className="pipe-glyph">
              <path d="M4 8.4l2.6 2.5L12 5.4" />
            </svg>
          )}
          {state === "failed" && (
            <svg viewBox="0 0 16 16" className="pipe-glyph">
              <path d="M5 5l6 6M11 5l-6 6" />
            </svg>
          )}
        </span>
        {!last && (
          <span className="pipe-link">
            <span className="pipe-link-fill" />
          </span>
        )}
      </span>
      <span className="pipe-text">
        <span className="pipe-title">{stageTitle(stage)}</span>
        <span className="pipe-note num">{note}</span>
        <span className="sr-only">{spoken}</span>
      </span>
    </li>
  );
}

function QueuedPanel({ since }: { since: string }) {
  const now = useNow(1000);
  return (
    <div className="pipe-live" role="status">
      <p className="pipe-headline">
        <span className="pipe-beacon" aria-hidden="true" />
        Queued, waiting for a worker to pick it up
        <span className="pipe-clock num">{formatClock(now - new Date(since).getTime())}</span>
      </p>
    </div>
  );
}

function LivePanel({ analysis }: { analysis: Analysis }) {
  const now = useNow(1000);
  const progress = analysis.progress ?? null;
  const since = analysis.started_at ?? analysis.created_at;
  if (!progress || progress.stage === "done") {
    return (
      <div className="pipe-live" role="status">
        <p className="pipe-headline">
          <span className="pipe-beacon" aria-hidden="true" />
          {progress ? "Finishing up" : "Started, waiting for the worker's first report"}
          <span className="pipe-clock num">{formatClock(now - new Date(since).getTime())}</span>
        </p>
      </div>
    );
  }
  const info = STAGE_INFO[progress.stage];
  return (
    <div className="pipe-live">
      <p className="pipe-headline" role="status">
        <span className="pipe-beacon" aria-hidden="true" />
        {info?.doing ?? stageTitle(progress.stage)}
        <span className="pipe-step num">
          stage {progress.stage_index + 1} of {progress.stage_count}
        </span>
        <span className="pipe-clock num" aria-hidden="true">
          {formatClock(now - new Date(since).getTime())}
        </span>
      </p>
      <Counter done={progress.files_done} total={progress.files_total} />
      {progress.current && <Ticker value={progress.current} />}
      {progress.detail && <p className="pipe-detail">{progress.detail}</p>}
    </div>
  );
}

function Counter({ done, total }: { done: number | null; total: number | null }) {
  const shown = useEasedNumber(done ?? 0, 700);
  if (total === null || done === null) {
    return (
      <div className="pipe-bar is-indeterminate" aria-hidden="true">
        <span />
      </div>
    );
  }
  const share = total > 0 ? Math.min(1, done / total) : 1;
  return (
    <div className="pipe-counter">
      <div
        className="pipe-bar"
        role="progressbar"
        aria-label="Files processed in this stage"
        aria-valuemin={0}
        aria-valuemax={total}
        aria-valuenow={done}
      >
        <span style={{ transform: `scaleX(${share})` }} />
      </div>
      <span className="pipe-count num">
        {Math.round(shown).toLocaleString("en-US")}
        <span className="muted"> / {total.toLocaleString("en-US")} files</span>
      </span>
    </div>
  );
}

/** The file or item being processed; a new value slides in over the old one. */
function Ticker({ value }: { value: string }) {
  const [items, setItems] = useState<{ key: number; text: string }[]>([{ key: 0, text: value }]);
  const counter = useRef(0);
  useEffect(() => {
    // Keep the outgoing value for one animation so the swap reads as movement.
    setItems((previous) => {
      if (previous[previous.length - 1]?.text === value) return previous;
      counter.current += 1;
      return [...previous.slice(-1), { key: counter.current, text: value }];
    });
  }, [value]);
  return (
    <p className="pipe-ticker" title={value}>
      <span className="pipe-ticker-label">now</span>
      <span className="pipe-ticker-window">
        {items.map((item, i) => (
          <span
            key={item.key}
            className={i === items.length - 1 ? "pipe-ticker-item is-in" : "pipe-ticker-item is-out"}
            aria-hidden={i !== items.length - 1}
          >
            {item.text}
          </span>
        ))}
      </span>
    </p>
  );
}

function Summary({
  analysis,
  durations,
  stages,
}: {
  analysis: Analysis;
  durations: Record<string, number>;
  stages: string[];
}) {
  const measured = stages.filter((s) => durations[s] !== undefined);
  const total = measured.reduce((sum, s) => sum + (durations[s] ?? 0), 0);
  const counts = (analysis.counts ?? {}) as Record<string, unknown>;
  const outputs = stageOutputs(analysis, counts);
  if (analysis.status === "FAILED" || analysis.status === "CANCELLED") {
    const stopped = analysis.progress && analysis.progress.stage !== "done" ? stageTitle(analysis.progress.stage) : null;
    return (
      <p className="pipe-ending">
        {analysis.status === "FAILED" ? "The analysis failed" : "The analysis was cancelled"}
        {stopped ? ` during ${stopped.toLowerCase()}` : ""}.
      </p>
    );
  }
  if (!measured.length) return null;
  return (
    <div className="pipe-summary">
      <p className="pipe-ending">
        {analysis.status === "REUSED" ? "Fetched and matched in " : "Stages took "}
        <strong className="num">{formatDuration(total)}</strong>
        {analysis.status === "REUSED" && <span className="muted"> · the graph itself was reused</span>}
      </p>
      <div className="timeline" role="img" aria-label={timelineLabel(measured, durations)}>
        {measured.map((stage, i) => (
          <span
            key={stage}
            className={`timeline-seg seg-${i % 6}`}
            style={{ flexGrow: Math.max(durations[stage] ?? 0, total * 0.015), "--order": i } as CSSProperties}
            title={`${stageTitle(stage)}: ${formatDuration(durations[stage])}`}
          />
        ))}
      </div>
      <ul className="timeline-legend">
        {measured.map((stage, i) => (
          <li key={stage} style={{ "--order": i } as CSSProperties}>
            <span className={`timeline-key seg-${i % 6}`} aria-hidden="true" />
            <span className="timeline-name">{stageTitle(stage)}</span>
            <span className="timeline-ms num">{formatDuration(durations[stage])}</span>
            {outputs[stage] && <span className="timeline-out">{outputs[stage]}</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

function timelineLabel(stages: string[], durations: Record<string, number>): string {
  return `Stage durations: ${stages.map((s) => `${stageTitle(s)} ${formatDuration(durations[s])}`).join(", ")}`;
}

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString("en-US")} ${n === 1 ? one : many}`;
}

/** What each stage produced, from the run's recorded counts. */
function stageOutputs(analysis: Analysis, counts: Record<string, unknown>): Record<string, string> {
  const out: Record<string, string> = {};
  if (analysis.snapshot) out.fetch = `commit ${shortSha(analysis.snapshot.commit_sha).slice(0, 7)}`;
  if (analysis.snapshot?.file_count != null) out.snapshot = plural(analysis.snapshot.file_count, "file");
  const python = num(counts.python_files);
  const hits = num(counts.parse_cache_hits);
  if (python !== null) out.parse = plural(python, "Python file") + (hits ? `, ${hits} from cache` : "");
  const references = num(counts.references);
  if (references !== null) out.resolve = plural(references, "reference");
  const symbols = num(counts.symbols);
  const edges = Object.values((counts.edges_by_type ?? {}) as Record<string, unknown>).reduce<number>(
    (sum, v) => sum + (num(v) ?? 0),
    0,
  );
  if (symbols !== null) out.persist = `${plural(symbols, "symbol")}, ${plural(edges, "edge")}`;
  const tests = num(counts.tests_discovered);
  if (tests !== null) out.tests = plural(tests, "test");
  return out;
}
