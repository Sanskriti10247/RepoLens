"use client";

import Link from "next/link";
import { useEffect, useState, type CSSProperties, type ReactNode } from "react";

import { Empty, ErrorPanel, Loading } from "@/components/ui";
import { label } from "@/lib/format";
import { useReducedMotion } from "@/lib/motion";
import type { Stats } from "@/lib/types";
import { useApi } from "@/lib/useApi";

export interface BarItem {
  key: string;
  name: ReactNode;
  value: number;
  /** Plain-text name for the tooltip and the accessible label. */
  text: string;
}

/**
 * Horizontal bars that grow in one after another. Hovering or focusing a row shows its exact
 * value and share of the total.
 */
export function BarChart({ title, items, unit, empty }: { title: string; items: BarItem[]; unit: string; empty: string }) {
  const reduced = useReducedMotion();
  const [grown, setGrown] = useState(reduced);
  useEffect(() => {
    if (reduced || typeof requestAnimationFrame !== "function") {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setGrown(true);
      return;
    }
    const frame = requestAnimationFrame(() => setGrown(true));
    return () => cancelAnimationFrame(frame);
  }, [reduced]);
  const max = Math.max(0, ...items.map((i) => i.value));
  const total = items.reduce((s, i) => s + i.value, 0);
  return (
    <figure className="chart" data-motion={reduced ? "reduced" : "full"}>
      <figcaption className="label">{title}</figcaption>
      {items.length === 0 ? (
        <p className="muted small">{empty}</p>
      ) : (
        <ul className="chart-bars">
          {items.map((item, i) => {
            const share = total ? Math.round((100 * item.value) / total) : 0;
            const tip = `${item.text}: ${item.value.toLocaleString("en-US")} ${unit}${items.length > 1 ? ` (${share}% of shown)` : ""}`;
            return (
              <li key={item.key} tabIndex={0} aria-label={tip} style={{ "--order": i } as CSSProperties}>
                <span className="chart-name">{item.name}</span>
                <span className="chart-track" aria-hidden="true">
                  <span style={{ transform: `scaleX(${grown && max ? item.value / max : 0})` }} />
                </span>
                <span className="chart-value num" aria-hidden="true">
                  {item.value.toLocaleString("en-US")}
                </span>
                <span className="chart-tip" role="tooltip" aria-hidden="true">
                  {tip}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </figure>
  );
}

function fromRecord(values: Record<string, number>): BarItem[] {
  return Object.entries(values)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([key, value]) => ({ key, name: label(key), text: label(key), value }));
}

/** Symbols by kind, edges by type and the most connected functions, from ``/stats``. */
export function GraphCharts({ analysisId, repoId }: { analysisId: string; repoId: string }) {
  const { data, error, loading } = useApi<Stats>(`/analyses/${analysisId}/stats`);
  if (error) return <ErrorPanel error={error} title="Could not load graph statistics" />;
  if (loading && !data) return <Loading what="Counting the graph" />;
  if (!data) return null;
  const totalSymbols = Object.values(data.symbols_by_kind).reduce((s, n) => s + n, 0);
  if (!totalSymbols) return <Empty>The analysis recorded no symbols, so there is nothing to chart.</Empty>;
  const symbolLink = (id: number, name: string) => (
    <Link href={`/repositories/${repoId}/explore?symbol=${id}`} className="mono" title={name}>
      {name}
    </Link>
  );
  const degree = (rows: Stats["top_fan_in"]): BarItem[] =>
    rows.map((row) => ({
      key: String(row.symbol.id),
      name: symbolLink(row.symbol.id, row.symbol.qualified_name),
      text: row.symbol.qualified_name,
      value: row.count,
    }));
  return (
    <div className="charts">
      <BarChart title="Symbols by kind" items={fromRecord(data.symbols_by_kind)} unit="symbols" empty="No symbols." />
      <BarChart title="Edges by type" items={fromRecord(data.edges_by_type)} unit="edges" empty="No dependency edges." />
      <BarChart
        title="Most called"
        items={degree(data.top_fan_in)}
        unit="resolved callers"
        empty="No resolved calls between symbols."
      />
      <BarChart
        title="Calls the most"
        items={degree(data.top_fan_out)}
        unit="resolved callees"
        empty="No resolved calls between symbols."
      />
    </div>
  );
}
