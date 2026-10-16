"""Bounded graph algorithms over an abstract edge source.

The edge source answers "edges leaving / entering these nodes", one frontier at a time, so
the same algorithms run against an in-memory graph (tests) or indexed SQL queries (production)
without ever loading a whole graph. Every traversal has a visited set and explicit depth and
node limits, and reports whether a limit cut it short.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from repolens.domain.enums import Confidence, EdgeType


class Direction(StrEnum):
    OUT = "OUT"  # follow src → dst ("what does X depend on")
    IN = "IN"  # follow dst → src ("what depends on X")


@dataclass(frozen=True, slots=True)
class GraphEdge[N: Hashable]:
    src: N
    dst: N
    edge_type: EdgeType
    confidence: Confidence = Confidence.EXACT


class EdgeSource[N: Hashable](Protocol):
    def out_edges(self, nodes: Sequence[N], types: frozenset[EdgeType]) -> list[GraphEdge[N]]: ...

    def in_edges(self, nodes: Sequence[N], types: frozenset[EdgeType]) -> list[GraphEdge[N]]: ...


@dataclass(frozen=True, slots=True)
class EdgePolicy:
    types: frozenset[EdgeType]
    direction: Direction = Direction.IN
    include_heuristic: bool = True

    def allows(self, edge: GraphEdge[Hashable]) -> bool:
        return self.include_heuristic or edge.confidence is Confidence.EXACT


@dataclass(frozen=True, slots=True)
class Reached[N: Hashable]:
    node: N
    depth: int
    via: GraphEdge[N] | None  # edge used to reach this node (None for start nodes)


@dataclass
class TraversalResult[N: Hashable]:
    reached: dict[N, Reached[N]] = field(default_factory=dict)
    truncated: bool = False
    truncation_reason: str | None = None
    edges_examined: int = 0

    def path_to(self, node: N) -> list[GraphEdge[N]]:
        """Edges from a start node to ``node`` (in traversal order)."""
        path: list[GraphEdge[N]] = []
        current = self.reached.get(node)
        guard = 0
        while current is not None and current.via is not None and guard <= len(self.reached):
            path.append(current.via)
            previous = current.via.src if current.via.dst == current.node else current.via.dst
            current = self.reached.get(previous)
            guard += 1
        path.reverse()
        return path


def bounded_traversal[N: Hashable](
    source: EdgeSource[N],
    start: Iterable[N],
    policy: EdgePolicy,
    *,
    max_depth: int,
    max_nodes: int,
) -> TraversalResult[N]:
    """Breadth-first traversal from ``start``; each node is reported at its minimum depth.

    Terminates on cycles (visited set) and on large graphs (``max_depth``, ``max_nodes``).
    Ties are broken deterministically by sorting each frontier.
    """
    result: TraversalResult[N] = TraversalResult()
    frontier: list[N] = []
    for node in start:
        if node not in result.reached:
            result.reached[node] = Reached(node, 0, None)
            frontier.append(node)
    depth = 0
    while frontier:
        if depth >= max_depth:
            if _has_more(source, frontier, policy):
                result.truncated, result.truncation_reason = True, "max_depth"
            break
        frontier = sorted(frontier, key=repr)
        if policy.direction is Direction.OUT:
            edges = source.out_edges(frontier, policy.types)
        else:
            edges = source.in_edges(frontier, policy.types)
        result.edges_examined += len(edges)
        next_frontier: list[N] = []
        for edge in sorted(edges, key=lambda e: (repr(e.src), repr(e.dst), e.edge_type.value)):
            if not policy.allows(edge):
                continue
            neighbour = edge.dst if policy.direction is Direction.OUT else edge.src
            if neighbour in result.reached:
                continue
            if len(result.reached) >= max_nodes:
                result.truncated, result.truncation_reason = True, "max_nodes"
                return result
            result.reached[neighbour] = Reached(neighbour, depth + 1, edge)
            next_frontier.append(neighbour)
        frontier = next_frontier
        depth += 1
    return result


def _has_more[N: Hashable](source: EdgeSource[N], frontier: list[N], policy: EdgePolicy) -> bool:
    edges = (
        source.out_edges(frontier, policy.types)
        if policy.direction is Direction.OUT
        else source.in_edges(frontier, policy.types)
    )
    return any(policy.allows(e) for e in edges)


def shortest_path[N: Hashable](
    source: EdgeSource[N],
    start: N,
    goal: N,
    types: frozenset[EdgeType],
    *,
    max_depth: int = 10,
    max_nodes: int = 10_000,
) -> list[GraphEdge[N]] | None:
    """Shortest directed path ``start → goal`` following edges forward, or None."""
    if start == goal:
        return []
    policy = EdgePolicy(types=types, direction=Direction.OUT)
    result = bounded_traversal(source, [start], policy, max_depth=max_depth, max_nodes=max_nodes)
    if goal not in result.reached:
        return None
    return result.path_to(goal)


def strongly_connected_components[N: Hashable](
    nodes: Iterable[N], edges: Iterable[tuple[N, N]]
) -> list[list[N]]:
    """Dependency cycles: strongly connected components of size > 1, plus self-loops.

    Tarjan's algorithm, iterative so deep graphs cannot hit the recursion limit. Members
    are sorted and components ordered by first member, so output is deterministic.
    """
    adjacency: dict[N, list[N]] = {node: [] for node in nodes}
    self_loops: set[N] = set()
    for src, dst in edges:
        adjacency.setdefault(src, []).append(dst)
        adjacency.setdefault(dst, [])
        if src == dst:
            self_loops.add(src)
    tarjan = _Tarjan({node: sorted(targets, key=repr) for node, targets in adjacency.items()})
    for root in sorted(adjacency, key=repr):
        if root not in tarjan.index:
            tarjan.visit(root)
    cycles = [c for c in tarjan.components if len(c) > 1 or c[0] in self_loops]
    return sorted((sorted(c, key=repr) for c in cycles), key=lambda c: repr(c[0]))


class _Tarjan[N: Hashable]:
    def __init__(self, adjacency: dict[N, list[N]]) -> None:
        self.adjacency = adjacency
        self.index: dict[N, int] = {}
        self.lowlink: dict[N, int] = {}
        self.on_stack: set[N] = set()
        self.stack: list[N] = []
        self.components: list[list[N]] = []

    def visit(self, root: N) -> None:
        work: list[tuple[N, int]] = [(root, 0)]
        while work:
            node, child_index = work.pop()
            if child_index == 0:
                self._enter(node)
            descended = self._descend(node, child_index, work)
            if descended:
                continue
            if self.lowlink[node] == self.index[node]:
                self._pop_component(node)
            if work:
                parent = work[-1][0]
                self.lowlink[parent] = min(self.lowlink[parent], self.lowlink[node])

    def _enter(self, node: N) -> None:
        self.index[node] = self.lowlink[node] = len(self.index)
        self.stack.append(node)
        self.on_stack.add(node)

    def _descend(self, node: N, start: int, work: list[tuple[N, int]]) -> bool:
        neighbours = self.adjacency[node]
        for i in range(start, len(neighbours)):
            neighbour = neighbours[i]
            if neighbour not in self.index:
                work.append((node, i + 1))
                work.append((neighbour, 0))
                return True
            if neighbour in self.on_stack:
                self.lowlink[node] = min(self.lowlink[node], self.index[neighbour])
        return False

    def _pop_component(self, node: N) -> None:
        component: list[N] = []
        while True:
            member = self.stack.pop()
            self.on_stack.discard(member)
            component.append(member)
            if member == node:
                break
        self.components.append(component)


class InMemoryEdges[N: Hashable]:
    """An ``EdgeSource`` over a list of edges (tests, small graphs)."""

    def __init__(self, edges: Iterable[GraphEdge[N]]) -> None:
        self._out: dict[N, list[GraphEdge[N]]] = {}
        self._in: dict[N, list[GraphEdge[N]]] = {}
        for edge in edges:
            self._out.setdefault(edge.src, []).append(edge)
            self._in.setdefault(edge.dst, []).append(edge)
        self.queries = 0

    def out_edges(self, nodes: Sequence[N], types: frozenset[EdgeType]) -> list[GraphEdge[N]]:
        self.queries += 1
        return [e for n in nodes for e in self._out.get(n, []) if e.edge_type in types]

    def in_edges(self, nodes: Sequence[N], types: frozenset[EdgeType]) -> list[GraphEdge[N]]:
        self.queries += 1
        return [e for n in nodes for e in self._in.get(n, []) if e.edge_type in types]
