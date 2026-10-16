"""Impact propagation: who may be affected by a set of changed symbols, and why.

Reverse dependencies are followed breadth-first from the changed symbols ("seeds"):

- ``CALLS``, ``REFERENCES`` and ``INHERITS`` propagate transitively: if ``a`` calls ``b`` and
  ``b`` changed, ``a`` may behave differently, and so may everything that calls ``a``.
- ``IMPORTS`` is followed only one hop from a seed: importing a changed function does not
  execute it, so importers are reported but not used to propagate further.
- Paths through heuristic edges (self-dispatch, constructors, ambiguous candidates) or from
  heuristic seeds (containing classes, module-level changes) are marked heuristic.

Each impacted symbol keeps one shortest explanatory path, preferring exact edges among paths
of equal length. Traversal is bounded by depth and node count and reports truncation.
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from dataclasses import dataclass, field

from repolens.codegraph.traversal import EdgeSource, GraphEdge
from repolens.domain.enums import Confidence, EdgeType

PROPAGATING = frozenset({EdgeType.CALLS, EdgeType.REFERENCES, EdgeType.INHERITS})
ALL_DEPENDENCIES = PROPAGATING | {EdgeType.IMPORTS}


@dataclass(frozen=True, slots=True)
class Seed[N: Hashable]:
    node: N
    reason: str  # e.g. BODY_CHANGED, SIGNATURE_CHANGED, REMOVED, CONTAINER_OF_CHANGE
    heuristic: bool = False


@dataclass(frozen=True, slots=True)
class Impact[N: Hashable]:
    node: N
    depth: int
    seed: N
    seed_reason: str
    path: tuple[GraphEdge[N], ...]  # from the seed outwards: path[0].dst == seed
    heuristic: bool

    @property
    def via_import_only(self) -> bool:
        return bool(self.path) and self.path[-1].edge_type is EdgeType.IMPORTS


@dataclass
class PropagationResult[N: Hashable]:
    impacts: dict[N, Impact[N]] = field(default_factory=dict)
    truncated: bool = False
    truncation_reason: str | None = None
    queries: int = 0


def propagate[N: Hashable](
    source: EdgeSource[N],
    seeds: Sequence[Seed[N]],
    *,
    max_depth: int,
    max_nodes: int,
) -> PropagationResult[N]:
    result: PropagationResult[N] = PropagationResult()
    # Exact seeds first so a symbol that is both changed and a container stays exact.
    for seed in sorted(seeds, key=lambda s: (s.heuristic, repr(s.node), s.reason)):
        if seed.node not in result.impacts:
            result.impacts[seed.node] = Impact(
                seed.node, 0, seed.node, seed.reason, (), seed.heuristic
            )
    frontier = sorted(result.impacts, key=repr)
    depth = 0
    while frontier:
        if depth >= max_depth:
            # Only a cut-off if something new lies beyond the limit.
            beyond = source.in_edges(frontier, PROPAGATING)
            result.queries += 1
            if any(e.src not in result.impacts for e in beyond):
                result.truncated, result.truncation_reason = True, "max_depth"
            break
        types = ALL_DEPENDENCIES if depth == 0 else PROPAGATING
        edges = source.in_edges(frontier, types)
        result.queries += 1
        next_frontier: list[N] = []
        ordered = sorted(
            edges,
            key=lambda e: (
                e.confidence is not Confidence.EXACT,
                e.edge_type is EdgeType.IMPORTS,
                repr(e.dst),
                repr(e.src),
                e.edge_type.value,
            ),
        )
        for edge in ordered:
            parent = result.impacts[edge.dst]
            if edge.edge_type is EdgeType.IMPORTS and parent.depth != 0:
                continue
            if edge.src in result.impacts:
                continue
            if len(result.impacts) >= max_nodes:
                result.truncated, result.truncation_reason = True, "max_nodes"
                return result
            heuristic = parent.heuristic or edge.confidence is not Confidence.EXACT
            result.impacts[edge.src] = Impact(
                edge.src,
                depth + 1,
                parent.seed,
                parent.seed_reason,
                (*parent.path, edge),
                heuristic,
            )
            if edge.edge_type is not EdgeType.IMPORTS:
                next_frontier.append(edge.src)
        frontier = sorted(next_frontier, key=repr)
        depth += 1
    return result
