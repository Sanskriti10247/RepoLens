"""Impact propagation rules."""

from __future__ import annotations

from repolens.codegraph.traversal import GraphEdge, InMemoryEdges
from repolens.domain.enums import Confidence, EdgeType
from repolens.impact.propagation import Seed, propagate

CALLS, IMPORTS, INHERITS, REFS = (
    EdgeType.CALLS,
    EdgeType.IMPORTS,
    EdgeType.INHERITS,
    EdgeType.REFERENCES,
)
HEURISTIC = Confidence.HEURISTIC


def edges(*items: tuple[str, str, EdgeType] | tuple[str, str, EdgeType, Confidence]):
    return InMemoryEdges(GraphEdge(*item) for item in items)  # type: ignore[arg-type]


def test_transitive_callers_with_paths() -> None:
    graph = edges(
        ("service", "repo", CALLS),
        ("api", "service", CALLS),
        ("test_api", "api", CALLS),
        ("unrelated", "other", CALLS),
    )
    result = propagate(graph, [Seed("repo", "BODY_CHANGED")], max_depth=10, max_nodes=100)
    assert {n: i.depth for n, i in result.impacts.items()} == {
        "repo": 0,
        "service": 1,
        "api": 2,
        "test_api": 3,
    }
    path = result.impacts["test_api"].path
    assert [(e.src, e.dst) for e in path] == [
        ("service", "repo"),
        ("api", "service"),
        ("test_api", "api"),
    ]
    assert not any(i.heuristic for i in result.impacts.values())


def test_imports_are_followed_one_hop_only() -> None:
    graph = edges(
        ("consumer_module", "changed_fn", IMPORTS),
        ("other_module", "consumer_module", IMPORTS),
        ("caller", "consumer_module", CALLS),
    )
    result = propagate(graph, [Seed("changed_fn", "BODY_CHANGED")], max_depth=10, max_nodes=100)
    assert set(result.impacts) == {"changed_fn", "consumer_module"}
    assert result.impacts["consumer_module"].via_import_only


def test_inheritance_and_references_propagate() -> None:
    graph = edges(
        ("Child", "Base", INHERITS),
        ("GrandChild", "Child", INHERITS),
        ("register", "handler", REFS),
    )
    result = propagate(
        graph,
        [Seed("Base", "BODY_CHANGED"), Seed("handler", "SIGNATURE_CHANGED")],
        max_depth=10,
        max_nodes=100,
    )
    assert result.impacts["GrandChild"].depth == 2
    assert result.impacts["register"].seed_reason == "SIGNATURE_CHANGED"


def test_heuristic_edges_and_seeds_mark_paths_heuristic() -> None:
    graph = edges(
        ("override_caller", "base_method", CALLS, HEURISTIC),
        ("instantiator", "Klass", CALLS),
        ("downstream", "override_caller", CALLS),
    )
    result = propagate(
        graph,
        [Seed("base_method", "BODY_CHANGED"), Seed("Klass", "CONTAINER_OF_CHANGE", heuristic=True)],
        max_depth=10,
        max_nodes=100,
    )
    assert result.impacts["override_caller"].heuristic
    assert result.impacts["downstream"].heuristic  # inherits heuristic from its path
    assert result.impacts["instantiator"].heuristic  # reached from a heuristic seed
    assert not result.impacts["base_method"].heuristic


def test_exact_path_preferred_over_heuristic_at_same_depth() -> None:
    graph = edges(("caller", "a", CALLS, HEURISTIC), ("caller", "b", CALLS))
    result = propagate(
        graph, [Seed("a", "BODY_CHANGED"), Seed("b", "BODY_CHANGED")], max_depth=5, max_nodes=10
    )
    assert not result.impacts["caller"].heuristic
    assert result.impacts["caller"].seed == "b"


def test_cycles_and_limits() -> None:
    cyclic = edges(("a", "b", CALLS), ("b", "a", CALLS), ("c", "b", CALLS))
    result = propagate(cyclic, [Seed("a", "BODY_CHANGED")], max_depth=10, max_nodes=10)
    assert set(result.impacts) == {"a", "b", "c"} and not result.truncated

    chain = edges(*[(f"n{i + 1}", f"n{i}", CALLS) for i in range(20)])
    deep = propagate(chain, [Seed("n0", "BODY_CHANGED")], max_depth=3, max_nodes=100)
    assert len(deep.impacts) == 4 and deep.truncation_reason == "max_depth"

    exact = propagate(chain, [Seed("n17", "BODY_CHANGED")], max_depth=3, max_nodes=100)
    assert len(exact.impacts) == 4 and not exact.truncated  # n17..n20: nothing beyond

    wide = edges(*[(f"c{i}", "hub", CALLS) for i in range(100)])
    capped = propagate(wide, [Seed("hub", "BODY_CHANGED")], max_depth=3, max_nodes=10)
    assert len(capped.impacts) == 10 and capped.truncation_reason == "max_nodes"
