"use client";

import { useParams, usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useState } from "react";

import { GraphCanvas } from "@/components/GraphCanvas";
import { SourceView } from "@/components/SourceView";
import { Badge, Card, ErrorPanel, KeyValues, Loading } from "@/components/ui";
import { label, shortSha } from "@/lib/format";
import { layoutGraph, neighbourhood, type GroupEdge, type PlacedEdge, type PlacedNode } from "@/lib/moduleLayout";
import type { Neighbour, Reference, SourceFile, SymbolDetail, SymbolPage, SymbolSummary } from "@/lib/types";
import { useApi } from "@/lib/useApi";

const KINDS = ["", "MODULE", "CLASS", "FUNCTION", "ASYNC_FUNCTION", "METHOD", "ASYNC_METHOD"];

export default function ExplorePage() {
  return (
    <Suspense fallback={<Loading what="Loading the explorer" rows={6} />}>
      <Explorer />
    </Suspense>
  );
}

function Explorer() {
  const { id } = useParams<{ id: string }>();
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const selected = params.get("symbol");
  const file = params.get("file");
  const line = Number(params.get("line")) || null;
  const [query, setQuery] = useState(params.get("q") ?? "");
  const [kind, setKind] = useState(params.get("kind") ?? "");
  const [testsOnly, setTestsOnly] = useState(false);
  const [debounced, setDebounced] = useState(query);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(query), 250);
    return () => clearTimeout(timer);
  }, [query]);

  const search = new URLSearchParams({ limit: "50" });
  if (debounced) search.set("q", debounced);
  if (kind) search.set("kind", kind);
  if (testsOnly) search.set("is_test", "true");
  const results = useApi<SymbolPage>(`/repositories/${id}/symbols?${search}`);

  function setParams(changes: Record<string, string | null>) {
    const next = new URLSearchParams(params.toString());
    for (const [name, value] of Object.entries(changes)) {
      if (value === null) next.delete(name);
      else next.set(name, value);
    }
    if (query) next.set("q", query);
    router.replace(`${pathname}?${next}`, { scroll: false });
  }

  function select(symbolId: number) {
    setParams({ symbol: String(symbolId), file: null, line: null, to: null });
  }

  return (
    <div className="explorer">
      <aside className="explorer-list">
        <div className="filters">
          <span className="search-field">
            <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" aria-hidden="true">
              <circle cx="7" cy="7" r="4.5" />
              <path d="M10.5 10.5L14 14" />
            </svg>
            <input
              placeholder="Search by name or dotted path"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              aria-label="Search symbols"
              type="search"
              spellCheck={false}
            />
          </span>
          <div className="filter-row">
            <select value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Symbol kind">
              {KINDS.map((k) => (
                <option key={k} value={k}>
                  {k ? label(k) : "All kinds"}
                </option>
              ))}
            </select>
            <label className="checkbox">
              <input type="checkbox" checked={testsOnly} onChange={(e) => setTestsOnly(e.target.checked)} />
              Tests only
            </label>
          </div>
        </div>
        {results.error && <ErrorPanel error={results.error} />}
        {results.loading && !results.data && <Loading what="Searching symbols" rows={8} />}
        {results.data && (
          <>
            <p className="list-meta">
              {results.data.page.total > results.data.items.length
                ? `First ${results.data.items.length} of ${results.data.page.total} symbols`
                : `${results.data.page.total} symbols`}{" "}
              at <span className="sha">{shortSha(results.data.commit_sha)}</span>
            </p>
            {results.data.items.length === 0 && (
              <p className="muted small">Nothing matches. Try part of a name, or clear the kind filter.</p>
            )}
            <ul className="symbol-list">
              {results.data.items.map((s) => (
                <li key={s.id}>
                  <button
                    className={String(s.id) === selected ? "symbol-item active" : "symbol-item"}
                    onClick={() => select(s.id)}
                  >
                    <span className="kind">{kindIcon(s.kind)}</span>
                    <span className="name">{s.qualified_name}</span>
                    {s.is_test && <span className="tag">test</span>}
                  </button>
                </li>
              ))}
            </ul>
          </>
        )}
      </aside>
      <section className="explorer-detail">
        {selected ? (
          <SymbolPanel repoId={id} symbolId={selected} onSelect={select} params={params} setParams={setParams} />
        ) : file ? (
          <FilePanel repoId={id} fileId={file} line={line} />
        ) : (
          <div className="explorer-intro">
            <p className="muted">Pick a symbol to read its source, its callers and callees, and its dependency graph.</p>
            <ul className="kinds-key" aria-label="Symbol kinds">
              <li><b>mod</b>module</li>
              <li><b>cls</b>class</li>
              <li><b>fn</b>function</li>
              <li><b>afn</b>async function</li>
              <li><b>m</b>method</li>
              <li><b>am</b>async method</li>
            </ul>
          </div>
        )}
      </section>
    </div>
  );
}

/** The mirror is fetched again after a restart wiped it; useApi keeps retrying meanwhile. */
function SourceLoading({ retrying }: { retrying: string | null }) {
  return (
    <>
      {retrying === "source_fetching" && <p className="muted small">Fetching the source again. This takes a moment.</p>}
      <Loading what="Loading source" rows={10} />
    </>
  );
}

function kindIcon(kind: string): string {
  return { MODULE: "mod", CLASS: "cls", FUNCTION: "fn", ASYNC_FUNCTION: "afn", METHOD: "m", ASYNC_METHOD: "am" }[kind] ?? "?";
}

/** A whole file, opened from a report or a link that names a file (and optionally a line). */
function FilePanel({ repoId, fileId, line }: { repoId: string; fileId: string; line: number | null }) {
  const source = useApi<SourceFile>(`/repositories/${repoId}/files/${fileId}/source`);
  if (source.error) return <ErrorPanel error={source.error} title="Source unavailable" />;
  if (!source.data) return <SourceLoading retrying={source.retrying} />;
  const f = source.data;
  return (
    <Card
      title={<span className="mono">{f.path}</span>}
      actions={
        <span className="muted small">
          {line ? <>line {line} · </> : null}
          <span className="sha" title={f.commit_sha}>
            {shortSha(f.commit_sha)}
          </span>
        </span>
      }
    >
      <SourceView content={f.content} highlight={line ? { start: line, end: line } : null} maxHeight={720} />
    </Card>
  );
}

function SymbolPanel({
  repoId,
  symbolId,
  onSelect,
  params,
  setParams,
}: {
  repoId: string;
  symbolId: string;
  onSelect: (id: number) => void;
  params: URLSearchParams;
  setParams: (changes: Record<string, string | null>) => void;
}) {
  const detail = useApi<SymbolDetail>(`/repositories/${repoId}/symbols/${symbolId}`);
  const fileId = detail.data?.symbol.file_id;
  const source = useApi<SourceFile>(fileId ? `/repositories/${repoId}/files/${fileId}/source` : null);
  if (detail.error) return <ErrorPanel error={detail.error} />;
  if (!detail.data) return <Loading what="Loading symbol" shape="block" />;
  const d = detail.data;
  const s = d.symbol;
  const lastLine = s.end_col === 0 && s.end_line > s.start_line ? s.end_line - 1 : s.end_line;
  return (
    <>
      <Card
        title={
          <>
            <span className="mono">{s.qualified_name}</span> <Badge value={s.kind} text={label(s.kind)} />
            {s.is_test && <span className="tag">test</span>}
          </>
        }
      >
        <KeyValues
          items={[
            ["Location", <span key="l" className="mono">{`${s.path}:${s.start_line}–${lastLine}`}</span>],
            ["Signature", s.signature ? <code key="s" className="inline-code">{s.signature}</code> : "—"],
            ["Decorators", d.decorators.length ? d.decorators.map((x) => `@${x}`).join(", ") : "—"],
            ["Docstring", s.doc_summary ?? "—"],
            ["Visibility", label(s.visibility)],
            ["Commit", <span key="c" className="sha" title={d.commit_sha}>{shortSha(d.commit_sha)}</span>],
          ]}
        />
        {s.has_parse_errors && (
          <p className="warn-text small">This symbol overlaps a syntax error; its extracted details may be incomplete.</p>
        )}
        {d.parent && (
          <p className="small">
            Defined in{" "}
            <button className="link mono" onClick={() => onSelect(d.parent!.id)}>
              {d.parent.qualified_name}
            </button>
          </p>
        )}
      </Card>
      <Card title="Source">
        {source.error && <ErrorPanel error={source.error} title="Source unavailable" />}
        {source.data && <SourceView content={source.data.content} highlight={{ start: s.start_line, end: lastLine }} />}
        {!source.data && !source.error && <SourceLoading retrying={source.retrying} />}
      </Card>
      <div className="columns">
        <NeighbourList title={`Callers (${d.callers_total})`} items={d.callers} onSelect={onSelect} empty="No statically resolved callers." />
        <NeighbourList title={`Callees (${d.callees_total})`} items={d.callees} onSelect={onSelect} empty="Calls no resolved symbols." />
      </div>
      <div className="columns">
        <NeighbourList title="Dependents (imports, references, subclasses)" items={d.dependents} onSelect={onSelect} empty="None." />
        <NeighbourList title="Dependencies (imports, references, bases)" items={d.dependencies} onSelect={onSelect} empty="None." />
      </div>
      {d.children.length > 0 && (
        <Card title="Contains">
          <ul className="plain">
            {d.children.map((c) => (
              <li key={c.id}>
                <button className="link mono" onClick={() => onSelect(c.id)}>
                  {c.name}
                </button>{" "}
                <span className="muted small">{label(c.kind)}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}
      <ReferencesCard references={d.references} counts={d.reference_counts} onSelect={onSelect} />
      <Neighbourhood repoId={repoId} symbol={s} params={params} setParams={setParams} />
    </>
  );
}

function NeighbourList({
  title,
  items,
  onSelect,
  empty,
}: {
  title: string;
  items: Neighbour[];
  onSelect: (id: number) => void;
  empty: string;
}) {
  return (
    <Card title={title}>
      {items.length === 0 ? (
        <p className="muted">{empty}</p>
      ) : (
        <ul className="rules">
          {items.map((n) => (
            <li key={`${n.edge_type}-${n.symbol.id}`}>
              <button className="link mono" onClick={() => onSelect(n.symbol.id)}>
                {n.symbol.qualified_name}
              </button>{" "}
              <span className="muted small">{label(n.edge_type)}</span>
              {n.confidence !== "EXACT" && (
                <Badge value="HEURISTIC" text={n.rules.length ? `heuristic: ${n.rules.join(", ")}` : "heuristic"} />
              )}
              {n.reference_count > 1 && <span className="muted small"> ×{n.reference_count}</span>}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function ReferencesCard({
  references,
  counts,
  onSelect,
}: {
  references: Reference[];
  counts: Record<string, number>;
  onSelect: (id: number) => void;
}) {
  const [filter, setFilter] = useState<string>("");
  const shown = filter ? references.filter((r) => r.resolution_status === filter) : references;
  return (
    <Card
      title="Outgoing references"
      actions={
        <select value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Resolution status">
          <option value="">All ({references.length})</option>
          {Object.entries(counts).map(([status, n]) => (
            <option key={status} value={status}>
              {label(status)} ({n})
            </option>
          ))}
        </select>
      }
    >
      {shown.length === 0 ? (
        <p className="muted">No outgoing references.</p>
      ) : (
        <table className="table small">
          <thead>
            <tr>
              <th>Line</th>
              <th className="hide-sm">Kind</th>
              <th>Expression</th>
              <th>Resolution</th>
              <th>Target / reason</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => (
              <tr key={r.id}>
                <td className="mono">{r.start_line}</td>
                <td className="hide-sm">{label(r.ref_kind)}</td>
                <td className="mono">{r.raw_text}</td>
                <td className="nowrap">
                  <Badge value={r.resolution_status} />
                </td>
                <td className="mono">
                  {r.target_symbol_id ? (
                    <button className="link mono" onClick={() => onSelect(r.target_symbol_id!)}>
                      {r.target_qualified_name}
                    </button>
                  ) : (
                    r.external_target ?? r.resolver_rule ?? "—"
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

interface GraphEdgeOut {
  src: number;
  dst: number;
  edge_type: string;
  confidence: string;
}

interface GraphResponse {
  nodes: { depth: number; symbol: SymbolSummary }[];
  edges: GraphEdgeOut[];
  truncated: boolean;
  truncation_reason: string | null;
}

interface PathResponse {
  found: boolean;
  edges: GraphEdgeOut[];
  symbols: SymbolSummary[];
}

const DIRECTIONS: [string, string][] = [
  ["IN", "Used by"],
  ["OUT", "Uses"],
  ["BOTH", "Both"],
];

/** The symbols and edges as a layered drawing: dependents above what they depend on. */
function symbolLayout(symbols: SymbolSummary[], edges: GraphEdgeOut[]) {
  const nodes = symbols.map((s) => ({
    key: String(s.id),
    label: s.qualified_name.split(".").slice(-2).join("."),
    members: [],
    inCycle: false,
  }));
  const merged = new Map<string, GroupEdge>();
  for (const e of edges) {
    const key = `${e.src}->${e.dst}`;
    const edge = merged.get(key) ?? { key, src: String(e.src), dst: String(e.dst), weight: 0, types: [], cycle: false };
    edge.weight += 1;
    if (!edge.types.includes(e.edge_type)) edge.types.push(e.edge_type);
    merged.set(key, edge);
  }
  return layoutGraph(nodes, [...merged.values()].filter((e) => e.src !== e.dst));
}

function Neighbourhood({
  repoId,
  symbol,
  params,
  setParams,
}: {
  repoId: string;
  symbol: SymbolSummary;
  params: URLSearchParams;
  setParams: (changes: Record<string, string | null>) => void;
}) {
  const direction = DIRECTIONS.some(([d]) => d === params.get("dir")) ? params.get("dir")! : "IN";
  const depth = [1, 2, 3].includes(Number(params.get("depth"))) ? Number(params.get("depth")) : 2;
  const to = params.get("to");
  const [hovered, setHovered] = useState<string | null>(null);
  const [target, setTarget] = useState("");
  const [debounced, setDebounced] = useState("");
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(target.trim()), 250);
    return () => clearTimeout(timer);
  }, [target]);
  const graph = useApi<GraphResponse>(
    to ? null : `/repositories/${repoId}/graph?symbol_id=${symbol.id}&direction=${direction}&depth=${depth}&max_nodes=150`,
  );
  const path = useApi<PathResponse>(to ? `/repositories/${repoId}/paths?from=${symbol.id}&to=${to}` : null);
  const candidates = useApi<SymbolPage>(debounced ? `/repositories/${repoId}/symbols?q=${encodeURIComponent(debounced)}&limit=20` : null);

  const { symbols, layout } = useMemo(() => {
    const found = to ? (path.data?.found ? path.data.symbols : []) : (graph.data?.nodes.map((n) => n.symbol) ?? []);
    return { symbols: found, layout: symbolLayout(found, to ? (path.data?.edges ?? []) : (graph.data?.edges ?? [])) };
  }, [to, path.data, graph.data]);
  const byKey = useMemo(() => new Map(symbols.map((s) => [String(s.id), s])), [symbols]);
  const root = String(symbol.id);
  const lit = useMemo(() => (hovered ? neighbourhood(hovered, layout.edges, 1) : null), [hovered, layout.edges]);
  const describe = useCallback((n: PlacedNode) => byKey.get(n.key)?.qualified_name ?? n.label, [byKey]);
  const edgeTitle = useCallback(
    (e: PlacedEdge) =>
      `${byKey.get(e.src)?.qualified_name} → ${byKey.get(e.dst)?.qualified_name} (${e.types.map((t) => label(t).toLowerCase()).join(", ")})`,
    [byKey],
  );

  function tracePath(event: React.FormEvent) {
    event.preventDefault();
    const items = candidates.data?.items ?? [];
    const match = items.find((s) => s.qualified_name === target.trim()) ?? items[0];
    if (match) setParams({ to: String(match.id) });
  }

  const loading = to ? path.loading && !path.data : graph.loading && !graph.data;
  const error = to ? path.error : graph.error;
  const toName = to ? (path.data?.symbols.find((s) => String(s.id) === to)?.qualified_name ?? `symbol ${to}`) : null;

  return (
    <Card
      title={to ? "Dependency path" : "Dependency graph"}
      actions={
        to ? (
          <button type="button" className="secondary" onClick={() => setParams({ to: null })}>
            Back to the neighbourhood
          </button>
        ) : (
          <>
            <div className="segmented" role="group" aria-label="Direction">
              {DIRECTIONS.map(([d, text]) => (
                <button key={d} type="button" className={d === direction ? "is-on" : undefined} aria-pressed={d === direction} onClick={() => setParams({ dir: d })}>
                  {text}
                </button>
              ))}
            </div>
            <div className="segmented" role="group" aria-label="Depth">
              {[1, 2, 3].map((d) => (
                <button key={d} type="button" className={d === depth ? "is-on" : undefined} aria-pressed={d === depth} onClick={() => setParams({ depth: String(d) })}>
                  {d} {d === 1 ? "hop" : "hops"}
                </button>
              ))}
            </div>
          </>
        )
      }
    >
      {!to && (
        <form className="path-form" onSubmit={tracePath}>
          <input
            type="search"
            list="path-targets"
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            placeholder="Path to another symbol"
            aria-label="Path to symbol"
            spellCheck={false}
          />
          <datalist id="path-targets">
            {candidates.data?.items.map((s) => <option key={s.id} value={s.qualified_name} />)}
          </datalist>
          <button type="submit" className="secondary" disabled={!candidates.data?.items.length}>
            Trace
          </button>
        </form>
      )}
      {error && <ErrorPanel error={error} />}
      {loading && <Loading what="Loading the graph" shape="block" />}
      {to && path.data && !path.data.found && (
        <p className="muted">
          No dependency path from <span className="mono">{symbol.qualified_name}</span> to{" "}
          <span className="mono">{toName}</span> within 8 steps.{" "}
          <button type="button" className="link" onClick={() => setParams({ symbol: to, to: String(symbol.id) })}>
            Try the other direction
          </button>
        </p>
      )}
      {!to && graph.data && layout.nodes.length <= 1 && <p className="muted">Nothing in this direction.</p>}
      {layout.nodes.length > 1 && (
        <GraphCanvas
          key={`${symbol.id}-${direction}-${depth}-${to}`}
          layout={layout}
          label={`Dependency ${to ? "path" : "graph"} of ${symbol.qualified_name}: ${layout.nodes.length} symbols`}
          lit={lit}
          path={null}
          focus={hovered}
          selected={root}
          centerOn={null}
          describe={describe}
          edgeTitle={edgeTitle}
          onHover={setHovered}
          onChoose={(key, extend) => setParams(extend ? { to: key } : { symbol: key, to: null })}
          onClear={() => setHovered(null)}
        />
      )}
      {!to && graph.data?.truncated && (
        <p className="warn-text small">Stopped at {graph.data.nodes.length} symbols ({graph.data.truncation_reason}); more exist.</p>
      )}
    </Card>
  );
}
