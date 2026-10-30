"use client";

import Link from "next/link";
import { useEffect, useId, useState, type CSSProperties, type ReactNode } from "react";

import { Ticker } from "@/components/ui";
import { useCountUp, useReducedMotion } from "@/lib/motion";
import type { FindingSeverity, InsightFinding } from "@/lib/types";

/** True one frame after mounting (always true under reduced motion), for grow-in transitions. */
export function useGrown(): boolean {
  const reduced = useReducedMotion();
  const [grown, setGrown] = useState(false);
  useEffect(() => {
    if (reduced || typeof requestAnimationFrame !== "function") {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setGrown(true);
      return;
    }
    const frame = requestAnimationFrame(() => setGrown(true));
    return () => cancelAnimationFrame(frame);
  }, [reduced]);
  return grown || reduced;
}

/* ---- the score dial ------------------------------------------------------------- */

const SWEEP = 270;

function polar(cx: number, cy: number, r: number, degrees: number): [number, number] {
  const rad = ((degrees - 90) * Math.PI) / 180;
  return [cx + r * Math.cos(rad), cy + r * Math.sin(rad)];
}

function arc(cx: number, cy: number, r: number, from: number, to: number): string {
  const [x1, y1] = polar(cx, cy, r, from);
  const [x2, y2] = polar(cx, cy, r, to);
  const large = to - from > 180 ? 1 : 0;
  return `M${x1.toFixed(2)},${y1.toFixed(2)} A${r},${r} 0 ${large} 1 ${x2.toFixed(2)},${y2.toFixed(2)}`;
}

/**
 * The overall score on a 270° gauge with a tick every ten points. The arc and the number
 * count up together; the grade letter sits in the middle. Without a score (no component had
 * data) the dial stays empty and says so instead of showing a zero.
 */
export function ScoreDial({
  score,
  grade,
  size = "large",
}: {
  score: number | null;
  grade: string | null;
  size?: "large" | "small";
}) {
  const scored = score !== null && grade !== null;
  const shown = useCountUp(scored ? Math.max(0, Math.min(100, score)) : 0, 1300);
  const start = -SWEEP / 2;
  const end = start + (SWEEP * shown) / 100;
  const r = 84;
  const [nx, ny] = polar(100, 100, r, end);
  const name = scored ? `Health score ${score} out of 100, grade ${grade}` : "No health score: not enough data";
  return (
    <figure className={`dial dial-${size} ${scored ? `grade-${grade.toLowerCase()}` : "is-unscored"}`}>
      <svg viewBox="0 0 200 200" role="img" aria-label={name}>
        <path d={arc(100, 100, r, start, start + SWEEP)} className="dial-track" />
        {Array.from({ length: 11 }, (_, i) => {
          const angle = start + (SWEEP * i) / 10;
          const [x1, y1] = polar(100, 100, r + 9, angle);
          const [x2, y2] = polar(100, 100, r + (i % 5 === 0 ? 16 : 13), angle);
          return <line key={i} x1={x1} y1={y1} x2={x2} y2={y2} className={i % 5 === 0 ? "dial-tick is-major" : "dial-tick"} />;
        })}
        {[45, 60, 75, 90].map((threshold) => {
          const angle = start + (SWEEP * threshold) / 100;
          const [x, y] = polar(100, 100, r - 13, angle);
          return <circle key={threshold} cx={x} cy={y} r={1.4} className="dial-threshold" />;
        })}
        {scored && shown > 0.5 && <path d={arc(100, 100, r, start, end)} className="dial-value" />}
        {scored && <circle cx={nx} cy={ny} r={5.5} className="dial-needle" />}
        <text
          x="100"
          y={size === "large" ? 112 : 116}
          textAnchor="middle"
          className={!scored || shown >= score * 0.85 ? "dial-grade is-settled" : "dial-grade"}
          aria-hidden="true"
        >
          {scored ? grade : "–"}
        </text>
        <text x="100" y="146" textAnchor="middle" className="dial-score" aria-hidden="true">
          {scored ? (
            <>
              {Math.round(shown)}
              <tspan className="dial-of"> / 100</tspan>
            </>
          ) : (
            "not enough data"
          )}
        </text>
      </svg>
    </figure>
  );
}

/* ---- bars, meters and stacks -------------------------------------------------- */

/** A 0-100 score as a thin bar with the number beside it. */
export function ScoreBar({ score, label }: { score: number | null; label: string }) {
  const grown = useGrown();
  if (score === null) {
    return (
      <span className="score-bar is-empty" role="img" aria-label={`${label}: not enough data`}>
        <span className="score-bar-track" />
      </span>
    );
  }
  const tone = score >= 75 ? "ok" : score >= 60 ? "fair" : "low";
  return (
    <span className={`score-bar tone-${tone}`} role="img" aria-label={`${label}: ${score} of 100`}>
      <span className="score-bar-track">
        <span style={{ transform: `scaleX(${grown ? score / 100 : 0})` }} />
        <i style={{ left: "75%" }} />
      </span>
    </span>
  );
}

/** A share as a large percentage over a ruled, segmented gauge. */
export function Meter({
  value,
  caption,
  label,
  figure,
}: {
  value: number | null;
  caption: ReactNode;
  label: string;
  /** Shown instead of the percentage, when a share reads better as a count ("17 of 29"). */
  figure?: ReactNode;
}) {
  const grown = useGrown();
  if (value === null) {
    return (
      <div className="meter is-empty">
        <p className="meter-value muted">n/a</p>
        <p className="meter-caption">{caption}</p>
      </div>
    );
  }
  const percent = Math.round(value * 1000) / 10;
  const filled = Math.round(value * 40);
  return (
    <div className="meter">
      <p className="meter-value">{figure ?? <Ticker value={percent} decimals={percent % 1 ? 1 : 0} suffix="%" />}</p>
      <span className="meter-cells" role="img" aria-label={`${label}: ${percent}%`}>
        {Array.from({ length: 40 }, (_, i) => (
          <span
            key={i}
            className={grown && i < filled ? "is-on" : undefined}
            style={{ "--i": i } as CSSProperties}
          />
        ))}
      </span>
      <p className="meter-caption">{caption}</p>
    </div>
  );
}

export interface StackPart {
  key: string;
  label: string;
  value: number;
}

/** Parts of a whole as one bar plus a legend with counts and shares. */
export function StackedBar({ parts, unit, label }: { parts: StackPart[]; unit: string; label: string }) {
  const grown = useGrown();
  const total = parts.reduce((sum, p) => sum + p.value, 0);
  const shown = collapse(parts.filter((p) => p.value > 0));
  if (!total) return <p className="muted small">Nothing recorded.</p>;
  return (
    <div className="stack">
      <span className="stack-bar" role="img" aria-label={`${label}: ${shown.map((p) => `${p.label} ${p.value}`).join(", ")}`}>
        {shown.map((p, i) => (
          <span
            key={p.key}
            className={`stack-seg seg-${i % 6}`}
            style={{ flexGrow: grown ? p.value : 0, "--order": i } as CSSProperties}
            title={`${p.label}: ${p.value.toLocaleString("en-US")} ${unit} (${share(p.value, total)})`}
          />
        ))}
      </span>
      <ul className="stack-legend">
        {shown.map((p, i) => (
          <li key={p.key}>
            <span className={`stack-key seg-${i % 6}`} aria-hidden="true" />
            <span className="stack-name">{p.label}</span>
            <span className="stack-count num">{p.value.toLocaleString("en-US")}</span>
            <span className="stack-share num">{share(p.value, total)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Six shades are told apart reliably; past that the smallest parts are summed as "Other". */
function collapse(parts: StackPart[], keep = 6): StackPart[] {
  if (parts.length <= keep) return parts;
  const head = parts.slice(0, keep - 1);
  const rest = parts.slice(keep - 1);
  return [
    ...head,
    {
      key: "__rest",
      label: `Other (${rest.map((p) => p.label).join(", ")})`,
      value: rest.reduce((sum, p) => sum + p.value, 0),
    },
  ];
}

function share(value: number, total: number): string {
  if (!total) return "0%";
  const pct = (100 * value) / total;
  return pct > 0 && pct < 1 ? "<1%" : `${Math.round(pct)}%`;
}

/* ---- the complexity histogram -------------------------------------------------- */

/** Functions per complexity bucket. Buckets past 10 are drawn in the accent: they are the risk. */
export function Histogram({ buckets, unit }: { buckets: { bucket: string; count: number }[]; unit: string }) {
  const grown = useGrown();
  const max = Math.max(1, ...buckets.map((b) => b.count));
  const total = buckets.reduce((s, b) => s + b.count, 0);
  return (
    <figure className="histogram">
      <ol className="histogram-bars">
        {buckets.map((b, i) => {
          const risky = /^(\d+)/.exec(b.bucket) && Number(/^(\d+)/.exec(b.bucket)![1]) > 10;
          const tip = `Complexity ${b.bucket}: ${b.count.toLocaleString("en-US")} ${unit} (${share(b.count, total)})`;
          return (
            <li
              key={b.bucket}
              className={risky ? "is-risky" : undefined}
              tabIndex={0}
              aria-label={tip}
              style={{ "--order": i, "--h": grown ? Math.max(b.count / max, b.count ? 0.015 : 0) : 0 } as CSSProperties}
            >
              <span className="histogram-col" aria-hidden="true">
                <span className="histogram-fill" />
                <span className="histogram-count num">{b.count.toLocaleString("en-US")}</span>
              </span>
              <span className="histogram-label mono" aria-hidden="true">
                {b.bucket}
              </span>
              <span className="chart-tip" role="tooltip" aria-hidden="true">
                {tip}
              </span>
            </li>
          );
        })}
      </ol>
      <figcaption className="histogram-axis">
        <span>cyclomatic complexity per function</span>
        <span className="num">n = {total.toLocaleString("en-US")}</span>
      </figcaption>
    </figure>
  );
}

/* ---- the coupling scatter -------------------------------------------------------- */

export interface ScatterPoint {
  key: string;
  label: string;
  x: number;
  y: number;
  flagged: boolean;
  note: string;
}

const W = 520;
const H = 340;
const M = { top: 16, right: 18, bottom: 44, left: 48 };

function niceMax(value: number): number {
  if (value <= 5) return 5;
  const step = Math.pow(10, Math.floor(Math.log10(value)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * step >= value) return m * step;
  return 10 * step;
}

/**
 * Modules by fan-in (x: how many modules depend on it) and fan-out (y: how many it depends
 * on). Bottom right is stable and widely used; top left depends on much and is used by little.
 */
export function CouplingScatter({ points, links = [] }: { points: ScatterPoint[]; links?: [string, string][] }) {
  const grown = useGrown();
  const id = useId();
  const [active, setActive] = useState<string | null>(null);
  const maxX = niceMax(Math.max(1, ...points.map((p) => p.x)));
  const maxY = niceMax(Math.max(1, ...points.map((p) => p.y)));
  const sx = (v: number) => M.left + ((W - M.left - M.right) * v) / maxX;
  const sy = (v: number) => H - M.bottom - ((H - M.top - M.bottom) * v) / maxY;
  const ticks = (max: number) => [0, 0.25, 0.5, 0.75, 1].map((f) => Math.round(max * f * 10) / 10);
  const labelled = new Set([
    ...[...points].sort((a, b) => b.x + b.y - (a.x + a.y)).slice(0, 4).map((p) => p.key),
    ...points.filter((p) => p.flagged).map((p) => p.key),
  ]);
  const byKey = new Map(points.map((p) => [p.key, p]));
  const current = points.find((p) => p.key === active) ?? null;
  return (
    <figure className="scatter">
      <svg viewBox={`0 0 ${W} ${H}`} role="group" aria-labelledby={`${id}-title`}>
        <title id={`${id}-title`}>{`Module coupling: ${points.length} modules by fan-in and fan-out`}</title>
        {ticks(maxX).map((t) => (
          <g key={`x${t}`} className="scatter-grid">
            <line x1={sx(t)} x2={sx(t)} y1={M.top} y2={H - M.bottom} />
            <text x={sx(t)} y={H - M.bottom + 16} textAnchor="middle">
              {t}
            </text>
          </g>
        ))}
        {ticks(maxY).map((t) => (
          <g key={`y${t}`} className="scatter-grid">
            <line x1={M.left} x2={W - M.right} y1={sy(t)} y2={sy(t)} />
            <text x={M.left - 8} y={sy(t) + 4} textAnchor="end">
              {t}
            </text>
          </g>
        ))}
        <text x={(W + M.left) / 2} y={H - 6} textAnchor="middle" className="scatter-axis">
          fan-in · modules that depend on it →
        </text>
        <text x={14} y={(H - M.bottom + M.top) / 2} textAnchor="middle" className="scatter-axis" transform={`rotate(-90 14 ${(H - M.bottom + M.top) / 2})`}>
          fan-out · modules it depends on →
        </text>
        {links.map(([a, b]) => {
          const from = byKey.get(a);
          const to = byKey.get(b);
          if (!from || !to) return null;
          return (
            <line
              key={`${a}-${b}`}
              x1={sx(from.x)}
              y1={sy(from.y)}
              x2={sx(to.x)}
              y2={sy(to.y)}
              className="scatter-link"
              aria-hidden="true"
            />
          );
        })}
        {points.map((p, i) => (
          <g
            key={p.key}
            className={`scatter-point${p.flagged ? " is-flagged" : ""}${active === p.key ? " is-active" : ""}${active && active !== p.key ? " is-dim" : ""}`}
            transform={`translate(${sx(p.x)}, ${sy(p.y)})`}
            style={{ "--order": i } as CSSProperties}
            tabIndex={0}
            role="img"
            aria-label={`${p.label}: fan-in ${p.x}, fan-out ${p.y}. ${p.note}`}
            onPointerEnter={() => setActive(p.key)}
            onPointerLeave={() => setActive((a) => (a === p.key ? null : a))}
            onFocus={() => setActive(p.key)}
            onBlur={() => setActive((a) => (a === p.key ? null : a))}
          >
            <circle r={grown ? 6 : 0} />
            {labelled.has(p.key) && (
              <text
                x={sx(p.x) > W * 0.72 ? -9 : 9}
                y={sx(p.x) > W * 0.72 ? 18 : -8}
                textAnchor={sx(p.x) > W * 0.72 ? "end" : "start"}
                className="scatter-label"
              >
                {short(p.label)}
              </text>
            )}
          </g>
        ))}
      </svg>
      <div className="scatter-readout" aria-hidden="true">
        {current ? (
          <>
            <span className="mono">{current.label}</span>
            <span className="num">in {current.x}</span>
            <span className="num">out {current.y}</span>
            <span>{current.note}</span>
          </>
        ) : (
          <span className="muted">Point at a module to read its numbers.</span>
        )}
      </div>
    </figure>
  );
}

function short(name: string): string {
  const parts = name.split(".");
  return parts.length > 2 ? `…${parts.slice(-2).join(".")}` : name;
}

/* ---- findings ------------------------------------------------------------------- */

export const SEVERITIES: FindingSeverity[] = ["high", "medium", "low", "info"];
export const SEVERITY_TITLE: Record<FindingSeverity, string> = {
  high: "High",
  medium: "Medium",
  low: "Low",
  info: "Note",
};

/** Explore link for a finding's evidence: the symbol when known, else a search for its module. */
export function evidenceHref(
  repoId: string,
  e: { path: string | null; symbol_id?: number | null; file_id?: number | null; line?: number | null },
): string | null {
  if (e.symbol_id) return `/repositories/${repoId}/explore?symbol=${e.symbol_id}`;
  if (e.file_id) return `/repositories/${repoId}/explore?file=${e.file_id}${e.line ? `&line=${e.line}` : ""}`;
  if (e.path) return `/repositories/${repoId}/explore?q=${encodeURIComponent(moduleFromPath(e.path))}`;
  return null;
}

export function moduleFromPath(path: string): string {
  return path
    .replace(/\.pyi?$/, "")
    .replace(/\//g, ".")
    .replace(/^src\./, "")
    .replace(/\.__init__$/, "");
}

export function FindingItem({ finding, repoId, order }: { finding: InsightFinding; repoId: string; order: number }) {
  const [open, setOpen] = useState(false);
  const shown = open ? finding.evidence : finding.evidence.slice(0, 4);
  return (
    <li className={`finding sev-${finding.severity}`} style={{ "--order": order } as CSSProperties}>
      <span className="finding-mark" aria-hidden="true" />
      <div className="finding-body">
        <p className="finding-meta">
          <span className="finding-sev">{SEVERITY_TITLE[finding.severity]}</span>
          <span className="finding-cat">{finding.category}</span>
        </p>
        <h3 className="finding-title">{finding.title}</h3>
        <p className="finding-detail">{finding.detail}</p>
        {finding.evidence.length > 0 && (
          <ul className="finding-evidence" aria-label={`Evidence for: ${finding.title}`}>
            {shown.map((e, i) => {
              const href = evidenceHref(repoId, e);
              const where = e.path ? `${e.path}${e.line ? `:${e.line}` : ""}` : null;
              return (
                <li key={`${e.label}-${i}`}>
                  {href ? (
                    <Link href={href} className="evidence-chip" title={where ?? undefined}>
                      {e.label}
                    </Link>
                  ) : (
                    <span className="evidence-chip" title={where ?? undefined}>
                      {e.label}
                    </span>
                  )}
                </li>
              );
            })}
            {finding.evidence.length > 4 && (
              <li>
                <button type="button" className="link small" onClick={() => setOpen(!open)} aria-expanded={open}>
                  {open ? "fewer" : `${finding.evidence.length - 4} more`}
                </button>
              </li>
            )}
          </ul>
        )}
      </div>
    </li>
  );
}

/* ---- ranked rows ---------------------------------------------------------------- */

/** A ranked list where each row carries a bar for its value, relative to the first row. */
export function RankList<T>({
  rows,
  value,
  name,
  sub,
  unit,
  empty,
}: {
  rows: T[];
  value: (row: T) => number;
  name: (row: T) => ReactNode;
  sub?: (row: T) => ReactNode;
  /** A plain unit, or [singular, plural]. */
  unit: string | [string, string];
  empty: string;
}) {
  const grown = useGrown();
  const unitFor = (n: number) => (typeof unit === "string" ? unit : n === 1 ? unit[0] : unit[1]);
  if (!rows.length) return <p className="muted small">{empty}</p>;
  const max = Math.max(1, ...rows.map(value));
  return (
    <ol className="rank">
      {rows.map((row, i) => (
        <li key={i} style={{ "--order": i } as CSSProperties}>
          <span className="rank-n num" aria-hidden="true">
            {i + 1}
          </span>
          <span className="rank-main">
            <span className="rank-name">{name(row)}</span>
            {sub && <span className="rank-sub">{sub(row)}</span>}
          </span>
          <span className="rank-value num">
            {value(row).toLocaleString("en-US")}
            <span className="rank-unit"> {unitFor(value(row))}</span>
          </span>
          <span className="rank-bar" aria-hidden="true">
            <span style={{ transform: `scaleX(${grown ? value(row) / max : 0})` }} />
          </span>
        </li>
      ))}
    </ol>
  );
}
