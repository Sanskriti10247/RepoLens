"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from "react";

import { Empty, ErrorPanel, Loading } from "@/components/ui";
import { label } from "@/lib/format";
import { useReducedMotion } from "@/lib/motion";
import {
  displayName,
  groupGraph,
  grouper,
  layoutGraph,
  maxDepth,
  type Grouping,
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
const PAD = 28;
const MIN_ZOOM = 0.4;
const MAX_ZOOM = 4;

interface View {
  k: number;
  x: number;
  y: number;
}
const IDENTITY: View = { k: 1, x: 0, y: 0 };

/** Fetches ``/analyses/{id}/modules`` and draws it. */
export function ModuleArchitecture({ analysisId, repoId }: { analysisId: string; repoId: string }) {
  const { data, error, loading } = useApi<ModuleGraphData>(`/analyses/${analysisId}/modules`);
  if (error) return <ErrorPanel error={error} title="Could not load the module graph" />;
  if (loading && !data) return <Loading what="Laying out modules" />;
  if (!data) return null;
  return <ModuleGraphView graph={data} repoId={repoId} />;
}

export function ModuleGraphView({ graph, repoId }: { graph: ModuleGraphData; repoId: string }) {
  const reduced = useReducedMotion();
  const [grouping, setGrouping] = useState<Grouping>("auto");
  const [filter, setFilter] = useState("imports");
  const [hovered, setHovered] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [view, setView] = useState<View>(IDENTITY);
  const [dragging, setDragging] = useState(false);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const gesture = useRef<{ moved: number; pinch: number | null }>({ moved: 0, pinch: null });

  const types = EDGE_FILTERS.find(([key]) => key === filter)?.[2] ?? null;
  const layout = useMemo(() => {
    const grouped = groupGraph(graph, grouper(graph.modules, grouping), types ? new Set(types) : null);
    return layoutGraph(grouped.nodes, grouped.edges);
  }, [graph, grouping, types]);

  // A new grouping or filter is a new picture: start from the fitted view.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setView(IDENTITY);
    setSelected(null);
  }, [grouping, filter]);

  const focus = selected ?? hovered;
  const lit = useMemo(() => {
    if (!focus) return null;
    const nodes = new Set([focus]);
    const edges = new Set<string>();
    for (const e of layout.edges) {
      if (e.src === focus || e.dst === focus) {
        nodes.add(e.src);
        nodes.add(e.dst);
        edges.add(e.key);
      }
    }
    return { nodes, edges };
  }, [focus, layout.edges]);

  const vbW = layout.width + PAD * 2;
  const vbH = layout.height + PAD * 2 + 40; // room for same-row arcs under the last row

  /** Pointer position in viewBox units. */
  const toLocal = useCallback(
    (clientX: number, clientY: number) => {
      const svg = svgRef.current;
      const rect = svg?.getBoundingClientRect();
      if (!svg || !rect || !rect.width || !rect.height) return { x: 0, y: 0, scale: 1 };
      const scale = Math.max(vbW / rect.width, vbH / rect.height);
      const offsetX = (rect.width * scale - vbW) / 2;
      const offsetY = (rect.height * scale - vbH) / 2;
      return { x: (clientX - rect.left) * scale - offsetX, y: (clientY - rect.top) * scale - offsetY, scale };
    },
    [vbW, vbH],
  );

  const zoomAt = useCallback((factor: number, cx: number, cy: number) => {
    setView((v) => {
      const k = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, v.k * factor));
      const f = k / v.k;
      return { k, x: cx - (cx - v.x) * f, y: cy - (cy - v.y) * f };
    });
  }, []);

  // Wheel zoom needs a non-passive listener; plain scrolling keeps scrolling the page.
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const onWheel = (event: WheelEvent) => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      const p = toLocal(event.clientX, event.clientY);
      zoomAt(Math.exp(-event.deltaY * 0.0025), p.x, p.y);
    };
    svg.addEventListener("wheel", onWheel, { passive: false });
    return () => svg.removeEventListener("wheel", onWheel);
  }, [toLocal, zoomAt]);

  function onPointerDown(event: ReactPointerEvent<SVGSVGElement>) {
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    gesture.current = { moved: 0, pinch: null };
    if (pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()];
      gesture.current.pinch = Math.hypot(a!.x - b!.x, a!.y - b!.y);
    }
  }

  function onPointerMove(event: ReactPointerEvent<SVGSVGElement>) {
    const previous = pointers.current.get(event.pointerId);
    if (!previous) return;
    const current = { x: event.clientX, y: event.clientY };
    pointers.current.set(event.pointerId, current);
    if (pointers.current.size === 2 && gesture.current.pinch) {
      const [a, b] = [...pointers.current.values()];
      const distance = Math.hypot(a!.x - b!.x, a!.y - b!.y);
      const mid = toLocal((a!.x + b!.x) / 2, (a!.y + b!.y) / 2);
      zoomAt(distance / gesture.current.pinch, mid.x, mid.y);
      gesture.current.pinch = distance;
      gesture.current.moved += 10;
      return;
    }
    const dx = current.x - previous.x;
    const dy = current.y - previous.y;
    gesture.current.moved += Math.abs(dx) + Math.abs(dy);
    if (gesture.current.moved > 4) {
      if (!dragging) {
        setDragging(true);
        event.currentTarget.setPointerCapture?.(event.pointerId);
      }
      const { scale } = toLocal(0, 0);
      setView((v) => ({ ...v, x: v.x + dx * scale, y: v.y + dy * scale }));
    }
  }

  function onPointerUp(event: ReactPointerEvent<SVGSVGElement>) {
    pointers.current.delete(event.pointerId);
    if (pointers.current.size === 0) setDragging(false);
  }

  function choose(key: string) {
    if (gesture.current.moved > 4) return; // the end of a drag, not a click
    setSelected((current) => (current === key ? null : key));
  }

  const rowOf = useMemo(() => new Map(layout.nodes.map((n) => [n.key, n.row])), [layout.nodes]);
  const depthLimit = maxDepth(graph.modules);
  const selectedNode = layout.nodes.find((n) => n.key === selected) ?? null;

  if (!graph.modules.length) {
    return <Empty>No Python modules were recorded for this analysis, so there is no architecture to draw.</Empty>;
  }

  return (
    <div className="mg" data-motion={reduced ? "reduced" : "full"}>
      <div className="mg-toolbar">
        <div className="segmented" role="group" aria-label="Dependency type">
          {EDGE_FILTERS.map(([key, name]) => (
            <button
              key={key}
              type="button"
              className={filter === key ? "is-on" : undefined}
              aria-pressed={filter === key}
              onClick={() => setFilter(key)}
            >
              {name}
            </button>
          ))}
        </div>
        <label className="mg-depth">
          <span className="label">Group by</span>
          <select
            value={String(grouping)}
            onChange={(e) => {
              const value = e.target.value;
              setGrouping(value === "auto" || value === "module" ? value : Number(value));
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
        <div className="mg-zoom" role="group" aria-label="Zoom">
          <button type="button" className="secondary" onClick={() => zoomAt(1.3, vbW / 2, vbH / 2)} aria-label="Zoom in">
            +
          </button>
          <button type="button" className="secondary" onClick={() => zoomAt(1 / 1.3, vbW / 2, vbH / 2)} aria-label="Zoom out">
            −
          </button>
          <button type="button" className="secondary" onClick={() => setView(IDENTITY)}>
            Fit
          </button>
        </div>
      </div>

      <div className={`mg-stage${dragging ? " is-dragging" : ""}`}>
        {layout.nodes.length === 0 ? (
          <p className="mg-empty muted">Nothing to draw at this grouping.</p>
        ) : (
          <svg
            ref={svgRef}
            className="mg-svg"
            viewBox={`${-PAD} ${-PAD} ${vbW} ${vbH}`}
            preserveAspectRatio="xMidYMid meet"
            role="group"
            aria-label={`Module graph: ${layout.nodes.length} nodes, ${layout.edges.length} links`}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerCancel={onPointerUp}
            onClick={(e) => {
              if (e.target === e.currentTarget && gesture.current.moved <= 4) setSelected(null);
            }}
          >
            <defs>
              <marker id="mg-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerUnits="userSpaceOnUse" markerWidth="9" markerHeight="9" orient="auto-start-reverse">
                <path d="M0,0 L8,4 L0,8 z" className="mg-arrowhead" />
              </marker>
              <marker id="mg-arrow-lit" viewBox="0 0 8 8" refX="7" refY="4" markerUnits="userSpaceOnUse" markerWidth="9" markerHeight="9" orient="auto-start-reverse">
                <path d="M0,0 L8,4 L0,8 z" className="mg-arrowhead is-lit" />
              </marker>
            </defs>
            <g
              className="mg-world"
              style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.k})` }}
            >
              <g className="mg-edges">
                {layout.edges.map((e) => {
                  const on = lit?.edges.has(e.key) ?? false;
                  const layer = rowOf.get(e.src) ?? 0;
                  return (
                    <path
                      key={`${filter}-${grouping}-${e.key}`}
                      d={e.path}
                      pathLength={1}
                      className={`mg-edge${e.cycle ? " is-cycle" : ""}${on ? " is-lit" : ""}${lit && !on ? " is-dim" : ""}`}
                      style={{ strokeWidth: Math.min(4, 1 + Math.log2(e.weight) * 0.45), "--delay": `${120 + layer * 70}ms` } as CSSProperties}
                      markerEnd={on ? "url(#mg-arrow-lit)" : "url(#mg-arrow)"}
                    >
                      <title>{`${displayName(e.src)} → ${displayName(e.dst)}: ${e.weight} ${e.weight === 1 ? "link" : "links"} (${e.types.map((t) => label(t).toLowerCase()).join(", ")})`}</title>
                    </path>
                  );
                })}
                {lit &&
                  layout.edges
                    .filter((e) => lit.edges.has(e.key))
                    .map((e) => <path key={`flow-${e.key}`} d={e.path} className={`mg-flow${e.cycle ? " is-cycle" : ""}`} />)}
              </g>
              <g className="mg-nodes">
                {layout.nodes.map((n) => (
                  <GraphNode
                    key={`${grouping}-${n.key}`}
                    node={n}
                    state={!lit ? "idle" : n.key === focus ? "focus" : lit.nodes.has(n.key) ? "near" : "dim"}
                    selected={n.key === selected}
                    onEnter={() => setHovered(n.key)}
                    onLeave={() => setHovered((h) => (h === n.key ? null : h))}
                    onChoose={() => choose(n.key)}
                  />
                ))}
              </g>
            </g>
          </svg>
        )}
        <p className="mg-hint muted">Drag to pan · pinch or Ctrl + scroll to zoom · select a node for details</p>
      </div>

      <div className="mg-legend small muted">
        <span>
          <span className="mg-key mg-key-edge" aria-hidden="true" />
          depends on (arrow points at the dependency)
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
      </div>

      {selectedNode && (
        <NodeDetails
          node={selectedNode}
          layout={layout}
          repoId={repoId}
          onSelect={setSelected}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  );
}

function GraphNode({
  node,
  state,
  selected,
  onEnter,
  onLeave,
  onChoose,
}: {
  node: PlacedNode;
  state: "idle" | "focus" | "near" | "dim";
  selected: boolean;
  onEnter: () => void;
  onLeave: () => void;
  onChoose: () => void;
}) {
  const text = node.label.length > 26 ? `…${node.label.slice(-25)}` : node.label;
  const group = node.members.length > 1;
  return (
    <g
      className={`mg-node is-${state}${node.inCycle ? " is-cycle" : ""}${selected ? " is-selected" : ""}`}
      transform={`translate(${node.x}, ${node.y})`}
      style={{ "--delay": `${node.row * 70 + node.order * 22}ms` } as CSSProperties}
      tabIndex={0}
      role="button"
      aria-pressed={selected}
      aria-label={`${node.label}${group ? `, ${node.members.length} modules` : ""}${node.inCycle ? ", in an import cycle" : ""}`}
      onPointerEnter={onEnter}
      onPointerLeave={onLeave}
      onFocus={onEnter}
      onBlur={onLeave}
      onClick={onChoose}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onChoose();
        }
      }}
    >
      <g className="mg-node-body">
        <rect x={-node.w / 2} y={-node.h / 2} width={node.w} height={node.h} rx={4} />
        {group && <rect className="mg-stack" x={-node.w / 2 + 3} y={node.h / 2} width={node.w - 6} height={3} rx={1} />}
        <text y={4} textAnchor="middle">
          {text}
        </text>
      </g>
    </g>
  );
}

function NodeDetails({
  node,
  layout,
  repoId,
  onSelect,
  onClose,
}: {
  node: PlacedNode;
  layout: ReturnType<typeof layoutGraph>;
  repoId: string;
  onSelect: (key: string) => void;
  onClose: () => void;
}) {
  const out = layout.edges.filter((e) => e.src === node.key).sort((a, b) => b.weight - a.weight);
  const incoming = layout.edges.filter((e) => e.dst === node.key).sort((a, b) => b.weight - a.weight);
  const explorer = (id: number) => `/repositories/${repoId}/explore?symbol=${id}`;
  return (
    <aside className="mg-details" aria-label={`Details for ${node.label}`}>
      <header>
        <h3 className="mg-details-title mono">{node.label}</h3>
        <button type="button" className="link" onClick={onClose}>
          Close
        </button>
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
