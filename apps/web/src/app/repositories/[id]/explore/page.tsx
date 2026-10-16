"use client";

import { useParams, usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

import { SourceView } from "@/components/SourceView";
import { Badge, Card, Empty, ErrorPanel, KeyValues, Loading } from "@/components/ui";
import { label, shortSha } from "@/lib/format";
import type { Neighbour, Reference, SourceFile, SymbolDetail, SymbolPage, SymbolSummary } from "@/lib/types";
import { useApi } from "@/lib/useApi";

const KINDS = ["", "MODULE", "CLASS", "FUNCTION", "ASYNC_FUNCTION", "METHOD", "ASYNC_METHOD"];

export default function ExplorePage() {
  return (
    <Suspense fallback={<Loading />}>
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

  function select(symbolId: number) {
    const next = new URLSearchParams(params.toString());
    next.set("symbol", String(symbolId));
    if (query) next.set("q", query);
    router.replace(`${pathname}?${next}`);
  }

  return (
    <div className="explorer">
      <aside className="explorer-list">
        <div className="filters">
          <input
            placeholder="Search symbols (name or qualified name)"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="Search symbols"
          />
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
        {results.loading && !results.data && <Loading />}
        {results.data && (
          <>
            <p className="muted small">
              {results.data.page.total} symbols · commit <span className="mono">{shortSha(results.data.commit_sha)}</span>
            </p>
            {results.data.items.length === 0 && <Empty>No symbols match.</Empty>}
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
          <SymbolPanel repoId={id} symbolId={selected} onSelect={select} />
        ) : (
          <Empty>Select a symbol to see its source, callers, callees and references.</Empty>
        )}
      </section>
    </div>
  );
}

function kindIcon(kind: string): string {
  return { MODULE: "mod", CLASS: "cls", FUNCTION: "fn", ASYNC_FUNCTION: "afn", METHOD: "m", ASYNC_METHOD: "am" }[kind] ?? "?";
}

function SymbolPanel({
  repoId,
  symbolId,
  onSelect,
}: {
  repoId: string;
  symbolId: string;
  onSelect: (id: number) => void;
}) {
  const detail = useApi<SymbolDetail>(`/repositories/${repoId}/symbols/${symbolId}`);
  const fileId = detail.data?.symbol.file_id;
  const source = useApi<SourceFile>(fileId ? `/repositories/${repoId}/files/${fileId}/source` : null);
  if (detail.error) return <ErrorPanel error={detail.error} />;
  if (!detail.data) return <Loading what="Loading symbol" />;
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
            ["Location", <span key="l" className="mono">{`${s.path}:${s.start_line}-${lastLine}`}</span>],
            ["Signature", s.signature ? <code key="s" className="inline-code">{s.signature}</code> : "—"],
            ["Decorators", d.decorators.length ? d.decorators.map((x) => `@${x}`).join(", ") : "—"],
            ["Docstring", s.doc_summary ?? "—"],
            ["Visibility", label(s.visibility)],
            ["Commit", <span key="c" className="mono">{shortSha(d.commit_sha)}</span>],
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
        {!source.data && !source.error && <Loading what="Loading source" />}
      </Card>
      <div className="grid-2">
        <NeighbourList title={`Callers (${d.callers_total})`} items={d.callers} onSelect={onSelect} empty="No statically resolved callers." />
        <NeighbourList title={`Callees (${d.callees_total})`} items={d.callees} onSelect={onSelect} empty="Calls no resolved symbols." />
      </div>
      <div className="grid-2">
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
      <Neighbourhood repoId={repoId} symbol={s} onSelect={onSelect} />
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
        <p className="muted small">{empty}</p>
      ) : (
        <ul className="plain">
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
        <p className="muted small">No references.</p>
      ) : (
        <table className="table small">
          <thead>
            <tr>
              <th>Line</th>
              <th>Kind</th>
              <th>Expression</th>
              <th>Resolution</th>
              <th>Target / reason</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => (
              <tr key={r.id}>
                <td className="mono">{r.start_line}</td>
                <td>{label(r.ref_kind)}</td>
                <td className="mono">{r.raw_text}</td>
                <td>
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

interface GraphResponse {
  nodes: { depth: number; symbol: SymbolSummary }[];
  edges: { src: number; dst: number; edge_type: string; confidence: string }[];
  truncated: boolean;
  truncation_reason: string | null;
}

function Neighbourhood({
  repoId,
  symbol,
  onSelect,
}: {
  repoId: string;
  symbol: SymbolSummary;
  onSelect: (id: number) => void;
}) {
  const [direction, setDirection] = useState("IN");
  const [depth, setDepth] = useState(2);
  const graph = useApi<GraphResponse>(
    `/repositories/${repoId}/graph?symbol_id=${symbol.id}&direction=${direction}&depth=${depth}&max_nodes=150`,
  );
  const byDepth = new Map<number, SymbolSummary[]>();
  for (const node of graph.data?.nodes ?? []) {
    if (node.depth === 0) continue;
    byDepth.set(node.depth, [...(byDepth.get(node.depth) ?? []), node.symbol]);
  }
  return (
    <Card
      title="Dependency neighbourhood"
      actions={
        <>
          <select value={direction} onChange={(e) => setDirection(e.target.value)} aria-label="Direction">
            <option value="IN">Who depends on it</option>
            <option value="OUT">What it depends on</option>
          </select>
          <select value={depth} onChange={(e) => setDepth(Number(e.target.value))} aria-label="Depth">
            {[1, 2, 3, 4, 5, 6].map((d) => (
              <option key={d} value={d}>
                depth {d}
              </option>
            ))}
          </select>
        </>
      }
    >
      {graph.error && <ErrorPanel error={graph.error} />}
      {graph.data && byDepth.size === 0 && <p className="muted small">No dependencies in this direction.</p>}
      {[...byDepth.entries()].map(([d, symbols]) => (
        <div key={d} className="depth-group">
          <div className="muted small">Distance {d}</div>
          <ul className="plain">
            {symbols.map((s) => (
              <li key={s.id}>
                <button className="link mono" onClick={() => onSelect(s.id)}>
                  {s.qualified_name}
                </button>
                {s.is_test && <span className="tag">test</span>}
              </li>
            ))}
          </ul>
        </div>
      ))}
      {graph.data?.truncated && (
        <p className="warn-text small">Traversal stopped at its limit ({graph.data.truncation_reason}); more symbols exist.</p>
      )}
    </Card>
  );
}
