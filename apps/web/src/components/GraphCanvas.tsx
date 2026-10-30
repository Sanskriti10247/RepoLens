"use client";

import {
  memo,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";

import { edgePath, type Layout, type PlacedEdge, type PlacedNode } from "@/lib/moduleLayout";

const PAD = 28;
const MIN_ZOOM = 0.3;
const MAX_ZOOM = 5;
const STEP = 60; // keyboard pan, in viewBox units

interface View {
  k: number;
  x: number;
  y: number;
  /** Animate to this view (buttons, keys, focus) rather than follow a pointer. */
  glide?: boolean;
}
const IDENTITY: View = { k: 1, x: 0, y: 0, glide: true };

export interface Highlight {
  nodes: Set<string>;
  edges: Set<string>;
}

type NodeState = "idle" | "focus" | "path" | "near" | "dim";

/**
 * A pannable, zoomable drawing of a layered graph. Drag the background to pan, drag a node to
 * move it, Ctrl + scroll or pinch to zoom; with the drawing focused, arrow keys pan, + and -
 * zoom and 0 fits. Remount it (change its ``key``) to start from the fitted view again.
 */
export function GraphCanvas({
  layout,
  label,
  lit,
  path,
  focus,
  selected,
  centerOn,
  describe,
  edgeTitle,
  onHover,
  onChoose,
  onOpen,
  onClear,
}: {
  layout: Layout;
  label: string;
  lit: Highlight | null;
  path: Highlight | null;
  focus: string | null;
  selected: string | null;
  /** Bring this node to the middle whenever a new object is passed. */
  centerOn: { key: string } | null;
  describe: (node: PlacedNode) => string;
  edgeTitle: (edge: PlacedEdge) => string;
  onHover: (key: string | null) => void;
  onChoose: (key: string, extend: boolean) => void;
  onOpen?: (key: string) => void;
  onClear: () => void;
}) {
  const ids = useId();
  const [view, setView] = useState<View>(IDENTITY);
  const [pinned, setPinned] = useState<Map<string, { x: number; y: number }>>(new Map());
  const [dragging, setDragging] = useState(false);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const gesture = useRef<{ moved: number; pinch: number | null; node: string | null }>({ moved: 0, pinch: null, node: null });

  const nodes = useMemo(
    () =>
      pinned.size
        ? layout.nodes.map((n) => {
            const p = pinned.get(n.key);
            return p ? { ...n, x: p.x, y: p.y } : n;
          })
        : layout.nodes,
    [layout.nodes, pinned],
  );
  const byKey = useMemo(() => new Map(nodes.map((n) => [n.key, n])), [nodes]);
  const edges = useMemo(
    () =>
      pinned.size
        ? layout.edges.map((e) =>
            pinned.has(e.src) || pinned.has(e.dst) ? { ...e, path: edgePath(byKey.get(e.src)!, byKey.get(e.dst)!) } : e,
          )
        : layout.edges,
    [layout.edges, pinned, byKey],
  );

  // A small graph is drawn at its own size in the middle rather than blown up to fill the frame.
  const contentW = layout.width + PAD * 2;
  const contentH = layout.height + PAD * 2 + 40; // room for same-row arcs under the last row
  const vbW = Math.max(contentW, 600);
  const vbH = Math.max(contentH, 320);
  const ox = -PAD - (vbW - contentW) / 2;
  const oy = -PAD - (vbH - contentH) / 2;

  /** Pointer position in viewBox units. */
  const toLocal = useCallback(
    (clientX: number, clientY: number) => {
      const rect = svgRef.current?.getBoundingClientRect();
      if (!rect || !rect.width || !rect.height) return { x: 0, y: 0, scale: 1 };
      const scale = Math.max(vbW / rect.width, vbH / rect.height);
      const offsetX = (rect.width * scale - vbW) / 2;
      const offsetY = (rect.height * scale - vbH) / 2;
      return { x: (clientX - rect.left) * scale - offsetX + ox, y: (clientY - rect.top) * scale - offsetY + oy, scale };
    },
    [vbW, vbH, ox, oy],
  );

  const zoomAt = useCallback((factor: number, cx: number, cy: number, glide = false) => {
    setView((v) => {
      const k = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, v.k * factor));
      const f = k / v.k;
      return { k, x: cx - (cx - v.x) * f, y: cy - (cy - v.y) * f, glide };
    });
  }, []);
  const middle = { x: ox + vbW / 2, y: oy + vbH / 2 };

  useEffect(() => {
    const node = centerOn ? byKey.get(centerOn.key) : null;
    if (!node) return;
    const cx = ox + vbW / 2;
    const cy = oy + vbH / 2;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setView((v) => {
      const k = Math.max(v.k, Math.min(2, 1 + layout.nodes.length / 60));
      return { k, x: cx - k * node.x, y: cy - k * node.y, glide: true };
    });
    // Only a new request moves the view; node drags and relayouts do not.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [centerOn]);

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

  const keyAt = (target: EventTarget) => (target as Element).closest?.("[data-key]")?.getAttribute("data-key") ?? null;

  function onPointerDown(event: ReactPointerEvent<SVGSVGElement>) {
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    gesture.current = { moved: 0, pinch: null, node: keyAt(event.target) };
    if (pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()];
      gesture.current = { moved: 10, pinch: Math.hypot(a!.x - b!.x, a!.y - b!.y), node: null };
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
      return;
    }
    const dx = current.x - previous.x;
    const dy = current.y - previous.y;
    gesture.current.moved += Math.abs(dx) + Math.abs(dy);
    if (gesture.current.moved <= 4) return;
    if (!dragging) {
      setDragging(true);
      event.currentTarget.setPointerCapture?.(event.pointerId);
    }
    const { scale } = toLocal(0, 0);
    const key = gesture.current.node;
    if (key) {
      const node = byKey.get(key);
      if (!node) return;
      const next = new Map(pinned);
      next.set(key, { x: node.x + (dx * scale) / view.k, y: node.y + (dy * scale) / view.k });
      setPinned(next);
    } else {
      setView((v) => ({ k: v.k, x: v.x + dx * scale, y: v.y + dy * scale }));
    }
  }

  function onPointerUp(event: ReactPointerEvent<SVGSVGElement>) {
    pointers.current.delete(event.pointerId);
    if (pointers.current.size === 0) setDragging(false);
  }

  function onKeyDown(event: ReactKeyboardEvent<SVGSVGElement>) {
    const key = keyAt(event.target);
    if (key && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      onChoose(key, event.shiftKey);
      return;
    }
    const pan: Record<string, [number, number]> = { ArrowLeft: [STEP, 0], ArrowRight: [-STEP, 0], ArrowUp: [0, STEP], ArrowDown: [0, -STEP] };
    if (pan[event.key]) {
      const [x, y] = pan[event.key]!;
      setView((v) => ({ k: v.k, x: v.x + x, y: v.y + y, glide: true }));
    } else if (event.key === "+" || event.key === "=") zoomAt(1.3, middle.x, middle.y, true);
    else if (event.key === "-") zoomAt(1 / 1.3, middle.x, middle.y, true);
    else if (event.key === "0") setView(IDENTITY);
    else if (event.key === "Escape") onClear();
    else return;
    event.preventDefault();
  }

  const scene = useMemo(() => {
    const stateOf = (key: string): NodeState =>
      key === focus ? "focus" : path?.nodes.has(key) ? "path" : lit?.nodes.has(key) ? "near" : lit || path ? "dim" : "idle";
    return (
      <>
        <g className="mg-edges">
          {edges.map((e) => {
            const onPath = path?.edges.has(e.key) ?? false;
            const on = onPath || (lit?.edges.has(e.key) ?? false);
            return (
              <Edge
                key={e.key}
                edge={e}
                state={onPath ? " is-lit is-path" : on ? " is-lit" : lit || path ? " is-dim" : ""}
                marker={`url(#${on ? "lit" : "arrow"}${ids})`}
                title={edgeTitle(e)}
              />
            );
          })}
          {edges
            .filter((e) => path?.edges.has(e.key) || lit?.edges.has(e.key))
            .map((e) => (
              <path key={`flow-${e.key}`} d={e.path} className={`mg-flow${e.cycle ? " is-cycle" : ""}`} />
            ))}
        </g>
        <g className="mg-nodes">
          {nodes.map((n) => (
            <GraphNode key={n.key} node={n} state={stateOf(n.key)} selected={n.key === selected} aria={describe(n)} />
          ))}
        </g>
      </>
    );
  }, [nodes, edges, lit, path, focus, selected, describe, edgeTitle, ids]);

  return (
    <div className={`mg-stage${dragging ? " is-dragging" : ""}`}>
      <svg
        ref={svgRef}
        className="mg-svg"
        viewBox={`${ox} ${oy} ${vbW} ${vbH}`}
        preserveAspectRatio="xMidYMid meet"
        style={{ aspectRatio: `${Math.round(vbW)} / ${Math.round(vbH)}` }}
        role="group"
        tabIndex={0}
        aria-label={label}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onPointerOver={(e) => onHover(keyAt(e.target))}
        onPointerLeave={() => onHover(null)}
        onFocus={(e) => onHover(keyAt(e.target))}
        onBlur={() => onHover(null)}
        onKeyDown={onKeyDown}
        onDoubleClick={(e) => {
          const key = keyAt(e.target);
          if (key && onOpen) onOpen(key);
        }}
        onClick={(e) => {
          if (gesture.current.moved > 4) return; // the end of a drag, not a click
          const key = keyAt(e.target);
          if (key) onChoose(key, e.shiftKey);
          else onClear();
        }}
      >
        <defs>
          <marker id={`arrow${ids}`} viewBox="0 0 8 8" refX="7" refY="4" markerUnits="userSpaceOnUse" markerWidth="9" markerHeight="9" orient="auto-start-reverse">
            <path d="M0,0 L8,4 L0,8 z" className="mg-arrowhead" />
          </marker>
          <marker id={`lit${ids}`} viewBox="0 0 8 8" refX="7" refY="4" markerUnits="userSpaceOnUse" markerWidth="9" markerHeight="9" orient="auto-start-reverse">
            <path d="M0,0 L8,4 L0,8 z" className="mg-arrowhead is-lit" />
          </marker>
        </defs>
        <g
          className={`mg-world${view.glide ? " is-gliding" : ""}`}
          style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.k})` }}
        >
          {scene}
        </g>
      </svg>
      <div className="mg-zoom" role="group" aria-label="Zoom">
        <button type="button" className="secondary" onClick={() => zoomAt(1.3, middle.x, middle.y, true)} aria-label="Zoom in">
          +
        </button>
        <button type="button" className="secondary" onClick={() => zoomAt(1 / 1.3, middle.x, middle.y, true)} aria-label="Zoom out">
          −
        </button>
        <button
          type="button"
          className="secondary"
          onClick={() => {
            setView(IDENTITY);
            setPinned(new Map());
          }}
          title="Fit the drawing and undo moved nodes (0)"
        >
          Fit
        </button>
      </div>
    </div>
  );
}

const Edge = memo(function Edge({ edge, state, marker, title }: { edge: PlacedEdge; state: string; marker: string; title: string }) {
  return (
    <path
      d={edge.path}
      className={`mg-edge${edge.cycle ? " is-cycle" : ""}${state}`}
      style={{ strokeWidth: Math.min(4, 1 + Math.log2(edge.weight) * 0.45) }}
      markerEnd={marker}
    >
      <title>{title}</title>
    </path>
  );
});

const GraphNode = memo(function GraphNode({
  node,
  state,
  selected,
  aria,
}: {
  node: PlacedNode;
  state: NodeState;
  selected: boolean;
  aria: string;
}) {
  const text = node.label.length > 26 ? `…${node.label.slice(-25)}` : node.label;
  return (
    <g
      className={`mg-node is-${state}${node.inCycle ? " is-cycle" : ""}${selected ? " is-selected" : ""}`}
      style={{ transform: `translate(${node.x}px, ${node.y}px)` }}
      data-key={node.key}
      tabIndex={0}
      role="button"
      aria-pressed={selected}
      aria-label={aria}
    >
      <rect x={-node.w / 2} y={-node.h / 2} width={node.w} height={node.h} rx={4} />
      {node.members.length > 1 && <rect className="mg-stack" x={-node.w / 2 + 3} y={node.h / 2} width={node.w - 6} height={3} rx={1} />}
      <text y={4} textAnchor="middle">
        {text}
      </text>
    </g>
  );
});
