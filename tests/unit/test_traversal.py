"""Graph algorithm invariants: termination, minimal depth, limits, determinism."""

from __future__ import annotations

import random

import pytest

from repolens.codegraph.traversal import (
    Direction,
    EdgePolicy,
    GraphEdge,
    InMemoryEdges,
    bounded_traversal,
    shortest_path,
    strongly_connected_components,
)
from repolens.domain.enums import Confidence, EdgeType

CALLS = EdgeType.CALLS
CALLS_ONLY = frozenset({CALLS})


def graph(*pairs: tuple[str, str], edge_type: EdgeType = CALLS) -> InMemoryEdges[str]:
    return InMemoryEdges(GraphEdge(a, b, edge_type) for a, b in pairs)


def test_reverse_traversal_finds_transitive_dependents_at_minimum_depth() -> None:
    # main → service → repo → db ; cli → repo
    edges = graph(("main", "service"), ("service", "repo"), ("repo", "db"), ("cli", "repo"))
    result = bounded_traversal(
        edges, ["db"], EdgePolicy(CALLS_ONLY, Direction.IN), max_depth=10, max_nodes=100
    )
    assert {n: r.depth for n, r in result.reached.items()} == {
        "db": 0,
        "repo": 1,
        "service": 2,
        "cli": 2,
        "main": 3,
    }
    assert not result.truncated
    assert [(e.src, e.dst) for e in result.path_to("main")] == [
        ("repo", "db"),
        ("service", "repo"),
        ("main", "service"),
    ]


def test_cycles_terminate() -> None:
    edges = graph(("a", "b"), ("b", "c"), ("c", "a"), ("c", "c"))
    result = bounded_traversal(
        edges, ["a"], EdgePolicy(CALLS_ONLY, Direction.OUT), max_depth=50, max_nodes=50
    )
    assert set(result.reached) == {"a", "b", "c"}
    assert edges.queries <= 4  # one query per BFS level, not per cycle iteration


def test_depth_limit_reports_truncation() -> None:
    edges = graph(*[(f"n{i}", f"n{i + 1}") for i in range(10)])
    result = bounded_traversal(
        edges, ["n0"], EdgePolicy(CALLS_ONLY, Direction.OUT), max_depth=3, max_nodes=100
    )
    assert set(result.reached) == {"n0", "n1", "n2", "n3"}
    assert (result.truncated, result.truncation_reason) == (True, "max_depth")


def test_depth_limit_without_more_edges_is_not_truncation() -> None:
    edges = graph(("a", "b"))
    result = bounded_traversal(
        edges, ["a"], EdgePolicy(CALLS_ONLY, Direction.OUT), max_depth=1, max_nodes=100
    )
    assert not result.truncated


def test_node_limit_on_wide_graph() -> None:
    edges = graph(*[("hub", f"leaf{i}") for i in range(1000)])
    result = bounded_traversal(
        edges, ["hub"], EdgePolicy(CALLS_ONLY, Direction.OUT), max_depth=5, max_nodes=50
    )
    assert len(result.reached) == 50
    assert (result.truncated, result.truncation_reason) == (True, "max_nodes")


def test_edge_type_and_confidence_policies() -> None:
    edges = InMemoryEdges(
        [
            GraphEdge("a", "b", EdgeType.CALLS),
            GraphEdge("a", "c", EdgeType.IMPORTS),
            GraphEdge("a", "d", EdgeType.CALLS, Confidence.HEURISTIC),
        ]
    )
    exact_calls = EdgePolicy(CALLS_ONLY, Direction.OUT, include_heuristic=False)
    result = bounded_traversal(edges, ["a"], exact_calls, max_depth=3, max_nodes=10)
    assert set(result.reached) == {"a", "b"}


def test_traversal_is_deterministic() -> None:
    pairs = [(f"n{random.Random(i).randint(0, 30)}", f"n{i % 30}") for i in range(200)]
    first = bounded_traversal(
        graph(*pairs), ["n0"], EdgePolicy(CALLS_ONLY, Direction.IN), max_depth=6, max_nodes=25
    )
    shuffled = pairs[:]
    random.Random(7).shuffle(shuffled)
    second = bounded_traversal(
        graph(*shuffled), ["n0"], EdgePolicy(CALLS_ONLY, Direction.IN), max_depth=6, max_nodes=25
    )
    assert {n: (r.depth, r.via) for n, r in first.reached.items()} == {
        n: (r.depth, r.via) for n, r in second.reached.items()
    }


def test_shortest_path() -> None:
    edges = graph(("a", "b"), ("b", "c"), ("a", "x"), ("x", "y"), ("y", "c"))
    path = shortest_path(edges, "a", "c", CALLS_ONLY)
    assert path is not None and [(e.src, e.dst) for e in path] == [("a", "b"), ("b", "c")]
    assert shortest_path(edges, "c", "a", CALLS_ONLY) is None
    assert shortest_path(edges, "a", "a", CALLS_ONLY) == []


@pytest.mark.parametrize(
    ("pairs", "expected"),
    [
        ([("a", "b"), ("b", "a"), ("b", "c")], [["a", "b"]]),
        ([("a", "b"), ("b", "c")], []),
        ([("a", "a")], [["a"]]),
        (
            [("a", "b"), ("b", "c"), ("c", "a"), ("d", "e"), ("e", "d"), ("c", "d")],
            [["a", "b", "c"], ["d", "e"]],
        ),
    ],
)
def test_strongly_connected_components(
    pairs: list[tuple[str, str]], expected: list[list[str]]
) -> None:
    nodes = {n for pair in pairs for n in pair}
    assert strongly_connected_components(nodes, pairs) == expected


def test_scc_handles_deep_chains_without_recursion() -> None:
    n = 20_000
    pairs = [(i, i + 1) for i in range(n)] + [(n, 0)]
    components = strongly_connected_components(range(n + 1), pairs)
    assert len(components) == 1 and len(components[0]) == n + 1
