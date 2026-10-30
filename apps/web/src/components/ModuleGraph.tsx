"use client";

import Link from "next/link";
import { useCallback, useEffect, useId, useMemo, useState } from "react";

import { GraphCanvas } from "@/components/GraphCanvas";
import { Empty, ErrorPanel, Loading } from "@/components/ui";
import { label } from "@/lib/format";
import { useReducedMotion } from "@/lib/motion";
import {
  childKey,
  displayName,
  expandGroups,
  groupGraph,
  grouper,
  layoutGraph,
  maxDepth,
  neighbourhood,
  shortestPath,
  type Grouping,
  type Layout,
  type PlacedEdge,
  type PlacedNode,
} from "@/lib/moduleLayout";
import type { ModuleGraph as ModuleGraphData } from "@/lib/types";
import { useApi } from "@/lib/useApi";

const EDGE_FILTERS: [string, string, string[] | null][] = [
  ["imports", "Imports", ["IMPORTS"]],
  ["calls", "Calls", ["CALLS"]],
  ["inherits", "Inheritance", ["INHERITS"]],
  ["all", "All", null],
];
const HOPS = [1, 2, 3];

/** Fetches ``/analyses/{id}/modules`` and draws it. */
export function ModuleArchitecture({ analysisId, repoId }: { analysisId: string; repoId: string }) {
  const { data, error, loading } = useApi<ModuleGraphData>(`/analyses/${analysisId}/modules`);
  if (error) return <ErrorPanel error={error} title="Could not load the module graph" />;
  if (loading && !data) return <Loading what="Laying out modules" />;
  if (!data) return null;
  return <ModuleGraphView graph={data} repoId={repoId} />;
}

/** The view as it is kept in the address bar, so a focused picture can be shared. */
interface UrlState {
  grouping: Grouping;
  filter: string;
  hops: number;
  open: string[];
  selected: string | null;
  to: string | null;
}

function readUrl(): UrlState {
  const p = new URLSearchParams(typeof window === "undefined" ? "" : window.location.search);
  const group = p.get("group");
  const hops = Number(p.get("hops"));
  return {
    grouping: !group || group === "auto" || group === "module" ? ((group as Grouping | null) ?? "auto") : Number(group) || "auto",
    filter: EDGE_FILTERS.some(([key]) => key === p.get("edges")) ? p.get("edges")! : "imports",
    hops: HOPS.includes(hops) ? hops : 1,
    open: p.get("open")?.split(",").filter(Boolean) ?? [],
    selected: p.get("focus"),
    to: p.get("to"),
  };
}

function writeUrl(state: UrlState) {
  const url = new URL(window.location.href);
  const set = (name: string, value: string | null, fallback: string) =>
    value && value !== fallback ? url.searchParams.set(name, value) : url.searchParams.delete(name);
  set("group", String(state.grouping), "auto");
  set("edges", state.filter, "imports");
  set("hops", String(state.hops), "1");
  set("open", state.open.join(","), "");
  set("focus", state.selected, "");
  set("to", state.selected ? state.to : null, "");
  if (url.href !== window.location.href) window.history.replaceState(window.history.state, "", url);
}

export function ModuleGraphView({ graph, repoId }: { graph: ModuleGraphData; repoId: string }) {
  const reduced = useReducedMotion();
  const listId = useId();
  const [initial] = useState(readUrl);
  const [grouping, setGrouping] = useState<Grouping>(initial.grouping);
  const [filter, setFilter] = useState(initial.filter);
  const [hops, setHops] = useState(initial.hops);
  const [open, setOpen] = useState<string[]>(initial.open);
  const [hovered, setHovered] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(initial.selected);
  const [to, setTo] = useState<string | null>(initial.to);
  const [centerOn, setCenterOn] = useState<{ key: string } | null>(initial.selected ? { key: initial.selected } : null);
  const [query, setQuery] = useState("");
  const [missing, setMissing] = useState(false);

  useEffect(() => writeUrl({ grouping, filter, hops, open, selected, to }), [grouping, filter, hops, open, selected, to]);

  const types = EDGE_FILTERS.find(([key]) => key === filter)?.[2] ?? null;
  const base = useMemo(() => grouper(graph.modules, grouping), [graph.modules, grouping]);
  const { keyOf, parentOf } = useMemo(() => expandGroups(graph.modules, base, new Set(open)), [graph.modules, base, open]);
  const layout = useMemo(() => {
    const grouped = groupGraph(graph, keyOf, types ? new Set(types) : null);
    return layoutGraph(grouped.nodes, grouped.edges);
  }, [graph, keyOf, types]);
  const byKey = useMemo(() => new Map(layout.nodes.map((n) => [n.key, n])), [layout.nodes]);

  // A key from the URL or an earlier picture that is no longer drawn selects nothing.
  const current = selected && byKey.has(selected) ? selected : null;
  const target = current && to && byKey.has(to) && to !== current ? to : null;
  const focus = current ?? hovered;
  const lit = useMemo(() => (focus ? neighbourhood(focus, layout.edges, hops) : null), [focus, layout.edges, hops]);
  const route = useMemo(() => {
    if (!current || !target) return null;
    const forward = shortestPath(current, target, layout.edges);
    const keys = forward ?? shortestPath(target, current, layout.edges);
    if (!keys) return { found: false as const, from: current, to: target };
    const edges = keys.map((k) => layout.edges.find((e) => e.key === k)!);
    const nodes = [edges[0]!.src, ...edges.map((e) => e.dst)];
    return { found: true as const, from: forward ? current : target, nodes, highlight: { nodes: new Set(nodes), edges: new Set(keys) } };
  }, [current, target, layout.edges]);

  function regroup(next: () => void) {
    next();
    setOpen([]);
    setSelected(null);
    setTo(null);
  }

  function choose(key: string, extend: boolean) {
    if (extend && current && key !== current) {
      setTo((t) => (t === key ? null : key));
      return;
    }
    setTo(null);
    setSelected((s) => (s === key ? null : key));
  }

  function expand(key: string) {
    const node = byKey.get(key);
    if (!node || node.members.length < 2) return;
    setOpen((o) => (o.includes(key) ? o : [...o, key]));
    setSelected(null);
  }

  function collapse(key: string) {
    const parent = parentOf.get(key);
    if (!parent) return;
    setOpen((o) => o.filter((k) => k !== parent && !k.startsWith(parent)));
    setSelected(parent);
    setCenterOn({ key: parent });
  }

  function select(key: string) {
    setSelected(key);
    setTo(null);
    setCenterOn({ key });
  }

  /** Find a module or group by name, opening the groups around a module so it is drawn on its own. */
  function search(text: string) {
    const wanted = text.trim();
    if (!wanted) return;
    const node = layout.nodes.find((n) => n.label === wanted || n.key === wanted);
    if (node) {
      setMissing(false);
      select(node.key);
      return;
    }
    const mod =
      graph.modules.find((m) => m.name === wanted || displayName(m.name) === wanted) ??
      graph.modules.find((m) => m.name.endsWith(`.${wanted}`));
    if (!mod) {
      setMissing(true);
      return;
    }
    setMissing(false);
    const opened = [...open];
    for (let key = keyOf(mod.name); key !== mod.name; ) {
      const next = childKey(key, mod);
      if (next === key) break;
      opened.push(key);
      key = next;
    }
    setOpen(opened);
    select(mod.name);
  }

  const describe = useCallback(
    (n: PlacedNode) =>
      `${n.label}${n.members.length > 1 ? `, ${n.members.length} modules` : ""}${n.inCycle ? ", in an import cycle" : ""}`,
    [],
  );
  const edgeTitle = useCallback(
    (e: PlacedEdge) =>
      `${displayName(e.src)} → ${displayName(e.dst)}: ${e.weight} ${e.weight === 1 ? "link" : "links"} (${e.types.map((t) => label(t).toLowerCase()).join(", ")})`,
    [],
  );

  const depthLimit = maxDepth(graph.modules);
  const selectedNode = current ? byKey.get(current)! : null;
  const names = useMemo(
    () => [...new Set([...layout.nodes.map((n) => n.label), ...graph.modules.map((m) => displayName(m.name))])].sort(),
    [layout.nodes, graph.modules],
  );

  if (!graph.modules.length) {
    return <Empty>No modules were recorded for this analysis, so there is no architecture to draw.</Empty>;
  }

  return (
    <div className="mg" data-motion={reduced ? "reduced" : "full"}>
      <div className="mg-toolbar">
        <form
          className="mg-search"
          role="search"
          onSubmit={(e) => {
            e.preventDefault();
            search(query);
          }}
        >
          <input
            type="search"
            list={listId}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setMissing(false);
            }}
            placeholder="Find a module"
            aria-label="Find a module"
            aria-invalid={missing || undefined}
            spellCheck={false}
          />
          <datalist id={listId}>
            {names.map((n) => (
              <option key={n} value={n} />
            ))}
          </datalist>
        </form>
        <div className="segmented" role="group" aria-label="Dependency type">
          {EDGE_FILTERS.map(([key, name]) => (
            <button
              key={key}
              type="button"
              className={filter === key ? "is-on" : undefined}
              aria-pressed={filter === key}
              onClick={() => regroup(() => setFilter(key))}
            >
              {name}
            </button>
          ))}
        </div>
        <label className="mg-depth">
          <span className="label">Group</span>
          <select
            value={String(grouping)}
            onChange={(e) => {
              const value = e.target.value;
              regroup(() => setGrouping(value === "auto" || value === "module" ? value : Number(value)));
            }}
            aria-label="Grouping level"
          >
            <option value="auto">Automatic</option>
            {Array.from({ length: depthLimit - 1 }, (_, i) => i + 1).map((d) => (
              <option key={d} value={d}>
                {d === 1 ? "Top-level package" : `Package, ${d} levels`}
              </option>
            ))}
            <option value="module">Module</option>
          </select>
        </label>
        <div className="segmented" role="group" aria-label="Neighbour depth">
          {HOPS.map((h) => (
            <button key={h} type="button" className={hops === h ? "is-on" : undefined} aria-pressed={hops === h} onClick={() => setHops(h)}>
              {h} {h === 1 ? "hop" : "hops"}
            </button>
          ))}
        </div>
      </div>
      {missing && <p className="small warn-text mg-missing">No module or group named “{query.trim()}”.</p>}

      {layout.nodes.length === 0 ? (
        <p className="mg-empty muted">Nothing to draw at this grouping.</p>
      ) : (
        <GraphCanvas
          key={`${grouping}-${filter}`}
          layout={layout}
          label={`Module graph: ${layout.nodes.length} nodes, ${layout.edges.length} links`}
          lit={route?.found ? null : lit}
          path={route?.found ? route.highlight : null}
          focus={focus}
          selected={current}
          centerOn={centerOn}
          describe={describe}
          edgeTitle={edgeTitle}
          onHover={setHovered}
          onChoose={choose}
          onOpen={expand}
          onClear={() => {
            setSelected(null);
            setTo(null);
          }}
        />
      )}

      <div className="mg-legend small muted">
        <span>
          <span className="mg-key mg-key-edge" aria-hidden="true" />
          depends on
        </span>
        {graph.import_cycles.length > 0 ? (
          <span>
            <span className="mg-key mg-key-cycle" aria-hidden="true" />
            {graph.import_cycles.length === 1 ? "1 import cycle" : `${graph.import_cycles.length} import cycles`}
          </span>
        ) : (
          <span>No import cycles between modules</span>
        )}
        <span className="num">
          {graph.modules.length.toLocaleString("en-US")} modules · {graph.edges.length.toLocaleString("en-US")} module links
        </span>
        <span className="mg-keys">
          <kbd>Shift</kbd>-click: path · double-click: open group · <kbd>←</kbd>
          <kbd>→</kbd> <kbd>+</kbd> <kbd>−</kbd> <kbd>0</kbd>
        </span>
      </div>

      {selectedNode && (
        <NodeDetails
          node={selectedNode}
          layout={layout}
          repoId={repoId}
          parent={parentOf.get(selectedNode.key) ?? null}
          route={route}
          to={target}
          names={layout.nodes.filter((n) => n.key !== selectedNode.key)}
          onSelect={select}
          onPathTo={setTo}
          onExpand={() => expand(selectedNode.key)}
          onCollapse={() => collapse(selectedNode.key)}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  );
}

type Route = { found: false; from: string; to: string } | { found: true; from: string; nodes: string[] } | null;

function NodeDetails({
  node,
  layout,
  repoId,
  parent,
  route,
  to,
  names,
  onSelect,
  onPathTo,
  onExpand,
  onCollapse,
  onClose,
}: {
  node: PlacedNode;
  layout: Layout;
  repoId: string;
  parent: string | null;
  route: Route;
  to: string | null;
  names: PlacedNode[];
  onSelect: (key: string) => void;
  onPathTo: (key: string | null) => void;
  onExpand: () => void;
  onCollapse: () => void;
  onClose: () => void;
}) {
  const out = layout.edges.filter((e) => e.src === node.key).sort((a, b) => b.weight - a.weight);
  const incoming = layout.edges.filter((e) => e.dst === node.key).sort((a, b) => b.weight - a.weight);
  const explorer = (id: number) => `/repositories/${repoId}/explore?symbol=${id}`;
  return (
    <aside className="mg-details" aria-label={`Details for ${node.label}`}>
      <header>
        <h3 className="mg-details-title mono">{node.label}</h3>
        <span className="mg-details-actions">
          {node.members.length > 1 && (
            <button type="button" className="link" onClick={onExpand}>
              Expand
            </button>
          )}
          {parent && (
            <button type="button" className="link" onClick={onCollapse}>
              Collapse into {displayName(parent)}
            </button>
          )}
          <button type="button" className="link" onClick={onClose}>
            Close
          </button>
        </span>
      </header>
      {node.inCycle && <p className="warn-text small">Part of an import cycle: these modules import each other.</p>}
      {node.members.length === 1 ? (
        <p className="small">
          <span className="mono muted">{node.members[0]!.path}</span>{" "}
          <Link href={explorer(node.members[0]!.id)}>Read the source</Link>
        </p>
      ) : (
        <details className="mg-members">
          <summary>{node.members.length} modules in this group</summary>
          <ul className="plain small">
            {node.members.slice(0, 80).map((m) => (
              <li key={m.id}>
                <Link href={explorer(m.id)} className="mono">
                  {displayName(m.name)}
                </Link>
              </li>
            ))}
            {node.members.length > 80 && <li className="muted">and {node.members.length - 80} more</li>}
          </ul>
        </details>
      )}
      <div className="mg-details-cols">
        <NeighbourList title={`Depends on (${out.length})`} items={out.map((e) => [e.dst, e.weight])} onSelect={onSelect} />
        <NeighbourList title={`Used by (${incoming.length})`} items={incoming.map((e) => [e.src, e.weight])} onSelect={onSelect} />
      </div>
      <div className="mg-path">
        <label className="mg-depth">
          <span className="label">Path to</span>
          <select value={to ?? ""} onChange={(e) => onPathTo(e.target.value || null)} aria-label="Path to">
            <option value="">—</option>
            {names.map((n) => (
              <option key={n.key} value={n.key}>
                {n.label}
              </option>
            ))}
          </select>
        </label>
        {route && !route.found && <p className="small muted">No dependency path between them in either direction.</p>}
        {route?.found && (
          <ol className="mg-route small mono" aria-label="Dependency path">
            {route.nodes.map((key) => (
              <li key={key}>
                <button type="button" className="link mono" onClick={() => onSelect(key)}>
                  {displayName(key)}
                </button>
              </li>
            ))}
          </ol>
        )}
      </div>
    </aside>
  );
}

function NeighbourList({
  title,
  items,
  onSelect,
}: {
  title: string;
  items: [string, number][];
  onSelect: (key: string) => void;
}) {
  return (
    <div>
      <h3>{title}</h3>
      {items.length === 0 ? (
        <p className="muted small">None.</p>
      ) : (
        <ul className="plain small">
          {items.slice(0, 12).map(([key, weight]) => (
            <li key={key}>
              <button type="button" className="link mono" onClick={() => onSelect(key)}>
                {displayName(key)}
              </button>{" "}
              <span className="muted num">×{weight}</span>
            </li>
          ))}
          {items.length > 12 && <li className="muted">and {items.length - 12} more</li>}
        </ul>
      )}
    </div>
  );
}
