/**
 * Layout for the module architecture graph: group modules into packages, then place the
 * groups in layers so that an importer sits above what it imports.
 *
 * Layering is the longest path from the top of the condensation graph (import cycles are
 * collapsed first, so their members share a layer), and the order inside a layer comes from
 * a few barycentre sweeps to keep edges short. Wide layers wrap onto extra rows. This is a
 * small Sugiyama-style layout; it needs no dependency and is deterministic for a given input.
 */

import type { ModuleEdge, ModuleGraph, ModuleNode } from "@/lib/types";

export const SCRIPT_PREFIX = "__file__";

export interface GroupNode {
  key: string;
  label: string;
  members: ModuleNode[];
  inCycle: boolean;
}

export interface GroupEdge {
  key: string;
  src: string;
  dst: string;
  /** Symbol-level edges aggregated into this one. */
  weight: number;
  types: string[];
  /** Some underlying module edge is part of a reported import cycle. */
  cycle: boolean;
}

export interface PlacedNode extends GroupNode {
  x: number;
  y: number;
  w: number;
  h: number;
  layer: number;
  row: number;
  order: number;
}

export interface PlacedEdge extends GroupEdge {
  path: string;
}

export interface Layout {
  nodes: PlacedNode[];
  edges: PlacedEdge[];
  width: number;
  height: number;
}

/** Module name shortened to ``depth`` dotted parts; ``null`` keeps whole modules. */
export function groupKey(name: string, depth: number | null): string {
  if (depth === null) return name;
  return name.split(".").slice(0, depth).join(".");
}

export function displayName(key: string): string {
  if (key.endsWith("/")) return key; // a folder from automatic grouping
  if (key === SCRIPT_PREFIX) return "scripts";
  if (key.startsWith(`${SCRIPT_PREFIX}.`)) return key.slice(SCRIPT_PREFIX.length + 1);
  return key;
}

/** How modules are merged into nodes: adaptively, by a fixed package depth, or not at all. */
export type Grouping = "auto" | "module" | number;

const depthOf = (key: string) => key.split("/").length;
// Folders around the code rather than the code itself; opened only when space is left over.
const AUXILIARY = new Set(["tests", "test", "testing", "docs", "doc", "examples", "example", "benchmarks", "scripts"]);
const auxiliary = (key: string) => AUXILIARY.has(key.split("/")[0] ?? "");
const segmentsOf = (path: string) => path.replace(/\.pyi?$/, "").split("/").filter(Boolean);
const prefix = (parts: string[], depth: number) => parts.slice(0, depth).join("/");

/**
 * Adaptive grouping by folder: start from the top-level folders and keep opening the largest
 * one into its subfolders and files while the picture stays at or under ``limit`` nodes. Source
 * folders are opened before tests, docs and examples, so the package itself gets the detail. Returns a key per
 * module name; folder keys are paths, a module shown on its own keeps its name.
 */
export function autoGroups(modules: { name: string; path: string }[], limit = 36): Map<string, string> {
  if (modules.length <= limit) return new Map(modules.map((m) => [m.name, m.name]));
  const parts = new Map(modules.map((m) => [m.name, segmentsOf(m.path || m.name.replaceAll(".", "/"))]));
  const level = new Map(modules.map((m) => [m.name, 1]));
  const keyOf = (name: string) => {
    const segments = parts.get(name)!;
    const depth = level.get(name)!;
    return depth >= segments.length ? name : `${prefix(segments, depth)}/`;
  };
  const blocked = new Set<string>();
  for (;;) {
    const groups = new Map<string, string[]>();
    for (const m of modules) groups.set(keyOf(m.name), [...(groups.get(keyOf(m.name)) ?? []), m.name]);
    const candidates = [...groups.entries()]
      .filter(([key]) => key.endsWith("/") && !blocked.has(key))
      .sort(
        (a, b) =>
          Number(auxiliary(a[0])) - Number(auxiliary(b[0])) || b[1].length - a[1].length || a[0].localeCompare(b[0]),
      );
    let opened = false;
    for (const [key, inside] of candidates) {
      const depth = depthOf(key.slice(0, -1));
      const children = new Set(
        inside.map((n) => {
          const segments = parts.get(n)!;
          return depth + 1 >= segments.length ? n : prefix(segments, depth + 1);
        }),
      );
      if (groups.size - 1 + children.size <= limit) {
        for (const n of inside) level.set(n, depth + 1);
        opened = true;
        break;
      }
      blocked.add(key);
    }
    if (!opened) return new Map(modules.map((m) => [m.name, keyOf(m.name)]));
  }
}

/** The node key for each module name under ``grouping``. */
export function grouper(modules: ModuleNode[], grouping: Grouping): (name: string) => string {
  if (grouping === "module") return (name) => name;
  if (grouping === "auto") {
    const keys = autoGroups(modules);
    return (name) => keys.get(name) ?? name;
  }
  return (name) => groupKey(name, grouping);
}

/**
 * One level finer than ``key`` for module ``m``: a folder opens into its subfolders and files,
 * a dotted package into its subpackages and modules. Returns ``key`` when there is nothing finer.
 */
export function childKey(key: string, m: { name: string; path: string }): string {
  if (key.endsWith("/")) {
    const segments = segmentsOf(m.path || m.name.replaceAll(".", "/"));
    const depth = depthOf(key.slice(0, -1));
    return depth + 1 >= segments.length ? m.name : `${prefix(segments, depth + 1)}/`;
  }
  if (key === m.name) return key;
  return groupKey(m.name, key.split(".").length + 1);
}

/**
 * ``keyOf`` with the groups in ``expanded`` opened one level (repeatedly, for nested opens).
 * ``parentOf`` maps each opened-out key to the group it came from, for collapsing again.
 */
export function expandGroups(
  modules: ModuleNode[],
  keyOf: (name: string) => string,
  expanded: ReadonlySet<string>,
): { keyOf: (name: string) => string; parentOf: Map<string, string> } {
  const keys = new Map<string, string>();
  const parentOf = new Map<string, string>();
  for (const m of modules) {
    let key = keyOf(m.name);
    while (expanded.has(key)) {
      const next = childKey(key, m);
      if (next === key) break;
      parentOf.set(next, key);
      key = next;
    }
    keys.set(m.name, key);
  }
  return { keyOf: (name) => keys.get(name) ?? keyOf(name), parentOf };
}

/** Nodes within ``depth`` hops of ``start`` (either direction) and the edges walked to reach them. */
export function neighbourhood(
  start: string,
  edges: { key: string; src: string; dst: string }[],
  depth: number,
): { nodes: Set<string>; edges: Set<string> } {
  const around = new Map<string, { key: string; other: string }[]>();
  const link = (from: string, entry: { key: string; other: string }) => {
    const list = around.get(from);
    if (list) list.push(entry);
    else around.set(from, [entry]);
  };
  for (const e of edges) {
    link(e.src, { key: e.key, other: e.dst });
    link(e.dst, { key: e.key, other: e.src });
  }
  const nodes = new Set([start]);
  const lit = new Set<string>();
  let frontier = [start];
  for (let hop = 0; hop < depth && frontier.length; hop += 1) {
    const next: string[] = [];
    for (const node of frontier) {
      for (const { key, other } of around.get(node) ?? []) {
        lit.add(key);
        if (!nodes.has(other)) {
          nodes.add(other);
          next.push(other);
        }
      }
    }
    frontier = next;
  }
  return { nodes, edges: lit };
}

/** Shortest path ``from → to`` following edge direction, as edge keys in order; ``null`` if none. */
export function shortestPath(from: string, to: string, edges: { key: string; src: string; dst: string }[]): string[] | null {
  if (from === to) return [];
  const out = new Map<string, { key: string; dst: string }[]>();
  for (const e of edges) {
    const list = out.get(e.src);
    if (list) list.push(e);
    else out.set(e.src, [e]);
  }
  const via = new Map<string, { key: string; from: string }>();
  const queue = [from];
  const seen = new Set([from]);
  while (queue.length) {
    const node = queue.shift()!;
    for (const e of out.get(node) ?? []) {
      if (seen.has(e.dst)) continue;
      seen.add(e.dst);
      via.set(e.dst, { key: e.key, from: node });
      if (e.dst === to) {
        const path: string[] = [];
        for (let at = to; at !== from; at = via.get(at)!.from) path.unshift(via.get(at)!.key);
        return path;
      }
      queue.push(e.dst);
    }
  }
  return null;
}

export function maxDepth(modules: ModuleNode[]): number {
  return Math.max(1, ...modules.map((m) => m.name.split(".").length));
}

/** Modules that belong to a reported import cycle, and the IMPORTS edges inside one. */
export function cycleMembership(graph: ModuleGraph): { modules: Set<number>; component: Map<number, number> } {
  const component = new Map<number, number>();
  graph.import_cycles.forEach((cycle, index) => cycle.forEach((id) => component.set(id, index)));
  return { modules: new Set(component.keys()), component };
}

function isCycleEdge(edge: ModuleEdge, component: Map<number, number>): boolean {
  if (edge.edge_type !== "IMPORTS") return false;
  const a = component.get(edge.src_module_id);
  return a !== undefined && a === component.get(edge.dst_module_id);
}

export function groupGraph(
  graph: ModuleGraph,
  keyOf: (name: string) => string,
  edgeTypes: ReadonlySet<string> | null,
): { nodes: GroupNode[]; edges: GroupEdge[] } {
  const { modules: cycleModules, component } = cycleMembership(graph);
  const byId = new Map(graph.modules.map((m) => [m.id, m]));
  const groups = new Map<string, GroupNode>();
  for (const mod of graph.modules) {
    const key = keyOf(mod.name);
    let group = groups.get(key);
    if (!group) {
      group = { key, label: displayName(key), members: [], inCycle: false };
      groups.set(key, group);
    }
    group.members.push(mod);
    if (cycleModules.has(mod.id)) group.inCycle = true;
  }
  const edges = new Map<string, GroupEdge>();
  for (const edge of graph.edges) {
    if (edgeTypes && !edgeTypes.has(edge.edge_type)) continue;
    const src = byId.get(edge.src_module_id);
    const dst = byId.get(edge.dst_module_id);
    if (!src || !dst) continue;
    const a = keyOf(src.name);
    const b = keyOf(dst.name);
    if (a === b) continue;
    const key = `${a}->${b}`;
    let merged = edges.get(key);
    if (!merged) {
      merged = { key, src: a, dst: b, weight: 0, types: [], cycle: false };
      edges.set(key, merged);
    }
    merged.weight += edge.edges;
    if (!merged.types.includes(edge.edge_type)) merged.types.push(edge.edge_type);
    if (isCycleEdge(edge, component)) merged.cycle = true;
  }
  const nodes = [...groups.values()].sort((x, y) => x.key.localeCompare(y.key));
  return { nodes, edges: [...edges.values()].sort((x, y) => x.key.localeCompare(y.key)) };
}

/** Strongly connected components (Tarjan, iterative); returns a component index per node. */
export function components(keys: string[], edges: { src: string; dst: string }[]): Map<string, number> {
  const adjacency = new Map<string, string[]>(keys.map((k) => [k, []]));
  for (const e of edges) adjacency.get(e.src)?.push(e.dst);
  const index = new Map<string, number>();
  const low = new Map<string, number>();
  const onStack = new Set<string>();
  const stack: string[] = [];
  const result = new Map<string, number>();
  let counter = 0;
  let next = 0;
  for (const root of keys) {
    if (index.has(root)) continue;
    const work: [string, number][] = [[root, 0]];
    index.set(root, counter);
    low.set(root, counter);
    counter += 1;
    stack.push(root);
    onStack.add(root);
    while (work.length) {
      const frame = work[work.length - 1]!;
      const [node, i] = frame;
      const targets = adjacency.get(node) ?? [];
      if (i < targets.length) {
        frame[1] = i + 1;
        const target = targets[i]!;
        if (!index.has(target)) {
          index.set(target, counter);
          low.set(target, counter);
          counter += 1;
          stack.push(target);
          onStack.add(target);
          work.push([target, 0]);
        } else if (onStack.has(target)) {
          low.set(node, Math.min(low.get(node)!, index.get(target)!));
        }
        continue;
      }
      work.pop();
      const parent = work[work.length - 1];
      if (parent) low.set(parent[0], Math.min(low.get(parent[0])!, low.get(node)!));
      if (low.get(node) === index.get(node)) {
        let member: string | undefined;
        do {
          member = stack.pop()!;
          onStack.delete(member);
          result.set(member, next);
        } while (member !== node);
        next += 1;
      }
    }
  }
  return result;
}

const NODE_H = 28;
const GAP_X = 18;
const ROW_GAP = 64;
const MAX_PER_ROW = 7;
const CHAR_W = 6.7;

export function nodeWidth(label: string): number {
  return Math.round(Math.min(184, Math.max(60, label.length * CHAR_W + 26)));
}

export function layoutGraph(nodes: GroupNode[], edges: GroupEdge[]): Layout {
  if (!nodes.length) return { nodes: [], edges: [], width: 0, height: 0 };
  const keys = nodes.map((n) => n.key);
  const comp = components(keys, edges);
  // Longest path over the condensation: importers above what they import.
  const compEdges = new Map<number, Set<number>>();
  const indegree = new Map<number, number>();
  for (const c of comp.values()) {
    compEdges.set(c, compEdges.get(c) ?? new Set());
    indegree.set(c, indegree.get(c) ?? 0);
  }
  for (const e of edges) {
    const a = comp.get(e.src)!;
    const b = comp.get(e.dst)!;
    if (a === b || compEdges.get(a)!.has(b)) continue;
    compEdges.get(a)!.add(b);
    indegree.set(b, (indegree.get(b) ?? 0) + 1);
  }
  const level = new Map<number, number>();
  const queue = [...indegree.entries()].filter(([, d]) => d === 0).map(([c]) => c).sort((x, y) => x - y);
  for (const c of queue) level.set(c, 0);
  while (queue.length) {
    const c = queue.shift()!;
    for (const d of compEdges.get(c) ?? []) {
      level.set(d, Math.max(level.get(d) ?? 0, (level.get(c) ?? 0) + 1));
      indegree.set(d, indegree.get(d)! - 1);
      if (indegree.get(d) === 0) queue.push(d);
    }
  }
  const layerOf = (key: string) => level.get(comp.get(key)!) ?? 0;
  const layerCount = Math.max(...keys.map(layerOf)) + 1;
  const layers: string[][] = Array.from({ length: layerCount }, () => []);
  for (const key of keys) layers[layerOf(key)]!.push(key);

  // Barycentre sweeps, down then up, to reduce crossings.
  const neighbours = new Map<string, string[]>(keys.map((k) => [k, []]));
  for (const e of edges) {
    neighbours.get(e.src)!.push(e.dst);
    neighbours.get(e.dst)!.push(e.src);
  }
  const position = new Map<string, number>();
  layers.forEach((layer) => layer.forEach((k, i) => position.set(k, i / Math.max(1, layer.length - 1))));
  for (let sweep = 0; sweep < 4; sweep += 1) {
    const order = sweep % 2 === 0 ? layers : [...layers].reverse();
    for (const layer of order) {
      const score = new Map<string, number>();
      for (const k of layer) {
        const around = neighbours.get(k)!.filter((n) => layerOf(n) !== layerOf(k));
        score.set(k, around.length ? around.reduce((s, n) => s + position.get(n)!, 0) / around.length : position.get(k)!);
      }
      layer.sort((a, b) => score.get(a)! - score.get(b)! || a.localeCompare(b));
      layer.forEach((k, i) => position.set(k, i / Math.max(1, layer.length - 1)));
    }
  }

  // Rows: each layer wraps after MAX_PER_ROW nodes.
  const byKey = new Map(nodes.map((n) => [n.key, n]));
  const rows: { layer: number; keys: string[] }[] = [];
  layers.forEach((layer, l) => {
    for (let i = 0; i < layer.length; i += MAX_PER_ROW) rows.push({ layer: l, keys: layer.slice(i, i + MAX_PER_ROW) });
  });
  const rowWidth = (r: { keys: string[] }) =>
    r.keys.reduce((s, k) => s + nodeWidth(byKey.get(k)!.label), 0) + GAP_X * (r.keys.length - 1);
  const width = Math.max(...rows.map(rowWidth));
  const placed = new Map<string, PlacedNode>();
  rows.forEach((row, r) => {
    let x = (width - rowWidth(row)) / 2;
    row.keys.forEach((key, order) => {
      const node = byKey.get(key)!;
      const w = nodeWidth(node.label);
      placed.set(key, { ...node, x: x + w / 2, y: r * (NODE_H + ROW_GAP) + NODE_H / 2, w, h: NODE_H, layer: row.layer, row: r, order });
      x += w + GAP_X;
    });
  });
  const height = rows.length * NODE_H + (rows.length - 1) * ROW_GAP;
  const placedEdges = edges.map((e) => ({ ...e, path: edgePath(placed.get(e.src)!, placed.get(e.dst)!) }));
  return { nodes: [...placed.values()], edges: placedEdges, width, height };
}

/** A cubic curve from the importer to the imported node, leaving and entering vertically. */
export function edgePath(a: PlacedNode, b: PlacedNode): string {
  const r = (v: number) => Math.round(v * 10) / 10;
  if (a.row === b.row) {
    const y = a.y + a.h / 2;
    const bend = 26 + Math.min(60, Math.abs(b.x - a.x) / 6);
    return `M${r(a.x)},${r(y)} C${r(a.x)},${r(y + bend)} ${r(b.x)},${r(y + bend)} ${r(b.x)},${r(y)}`;
  }
  const down = b.y > a.y;
  const y1 = down ? a.y + a.h / 2 : a.y - a.h / 2;
  const y2 = down ? b.y - b.h / 2 : b.y + b.h / 2;
  const pull = Math.max(24, Math.abs(y2 - y1) / 2);
  const c1 = down ? y1 + pull : y1 - pull;
  const c2 = down ? y2 - pull : y2 + pull;
  // Spread the ends along each node's edge by direction, so many edges into one node fan in
  // instead of meeting at a single point.
  const spread = (from: number, to: number, w: number) => Math.max(-w / 2 + 8, Math.min(w / 2 - 8, (to - from) * 0.18));
  const x1 = a.x + spread(a.x, b.x, a.w);
  const x2 = b.x + spread(b.x, a.x, b.w);
  return `M${r(x1)},${r(y1)} C${r(x1)},${r(c1)} ${r(x2)},${r(c2)} ${r(x2)},${r(y2)}`;
}
