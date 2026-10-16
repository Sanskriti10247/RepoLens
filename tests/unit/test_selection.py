"""Test selection policy and ranking."""

from __future__ import annotations

from repolens.test_selection.selection import (
    Budget,
    Evidence,
    EvidenceKind,
    Policy,
    Scope,
    TestCase,
    node_id_for,
    select_tests,
)

CATALOG = [
    TestCase("tests/test_a.py::test_direct", "tests/test_a.py", "test_a.test_direct"),
    TestCase("tests/test_a.py::test_deep", "tests/test_a.py", "test_a.test_deep"),
    TestCase("tests/test_b.py::test_heuristic", "tests/test_b.py", "test_b.test_heuristic"),
    TestCase("tests/test_c.py::test_unrelated", "tests/test_c.py", "test_c.test_unrelated"),
    TestCase("tests/test_d.py::test_flaky", "tests/test_d.py", "test_d.test_flaky"),
]
EVIDENCE = {
    "tests/test_a.py::test_direct": [Evidence(EvidenceKind.STATIC_DIRECT, {"seed": "f"}, 1)],
    "tests/test_a.py::test_deep": [Evidence(EvidenceKind.STATIC_TRANSITIVE, {"seed": "f"}, 4)],
    "tests/test_b.py::test_heuristic": [Evidence(EvidenceKind.STATIC_HEURISTIC, {}, 2)],
    "tests/test_d.py::test_flaky": [Evidence(EvidenceKind.PREVIOUSLY_FAILED, {})],
}


def test_conservative_selects_every_candidate_and_explains_exclusions() -> None:
    result = select_tests(CATALOG, EVIDENCE, scope=Scope.TARGETED)
    assert [d.test.node_id for d in result.selected] == [
        "tests/test_a.py::test_direct",
        "tests/test_a.py::test_deep",
        "tests/test_b.py::test_heuristic",
    ]
    reasons = {d.test.node_id: d.exclusion_reason for d in result.decisions if not d.selected}
    assert reasons == {
        "tests/test_c.py::test_unrelated": "NO_EVIDENCE",
        "tests/test_d.py::test_flaky": "HISTORY_ONLY",  # a past failure alone is not evidence
    }
    assert result.fallback_reason is None and not result.budget_limited


def test_scores_reflect_evidence_strength_and_depth() -> None:
    result = select_tests(CATALOG, EVIDENCE, scope=Scope.TARGETED)
    scores = {d.test.node_id: d.score for d in result.decisions}
    assert scores["tests/test_a.py::test_direct"] == 90
    assert scores["tests/test_a.py::test_deep"] == 70 - 15  # depth 4 → penalty 15
    assert scores["tests/test_b.py::test_heuristic"] == 45


def test_full_suite_fallback_selects_everything_with_reason() -> None:
    result = select_tests(
        CATALOG, EVIDENCE, scope=Scope.FULL_SUITE, full_suite_reason="pyproject.toml changed"
    )
    assert len(result.selected) == len(CATALOG)
    unrelated = next(d for d in result.decisions if d.test.node_id.endswith("test_unrelated"))
    assert unrelated.evidence[0].kind is EvidenceKind.FULL_SUITE_FALLBACK
    assert result.fallback_reason == "pyproject.toml changed"


def test_budget_by_count_reports_excluded_tests() -> None:
    result = select_tests(
        CATALOG,
        EVIDENCE,
        scope=Scope.TARGETED,
        policy=Policy.BUDGETED,
        budget=Budget(max_tests=2),
    )
    assert [d.test.node_id for d in result.selected] == [
        "tests/test_a.py::test_direct",
        "tests/test_a.py::test_deep",
    ]
    assert [d.test.node_id for d in result.excluded_by_budget] == [
        "tests/test_b.py::test_heuristic"
    ]
    assert result.budget_limited


def test_budget_by_time_prefers_stronger_then_faster_tests() -> None:
    durations = {
        "tests/test_a.py::test_direct": 5.0,
        "tests/test_a.py::test_deep": 30.0,
        "tests/test_b.py::test_heuristic": 1.0,
    }
    result = select_tests(
        CATALOG,
        EVIDENCE,
        scope=Scope.TARGETED,
        policy=Policy.BUDGETED,
        budget=Budget(max_seconds=10),
        durations=durations,
    )
    assert {d.test.node_id for d in result.selected} == {
        "tests/test_a.py::test_direct",
        "tests/test_b.py::test_heuristic",
    }
    assert result.estimated_seconds == 6.0


def test_budget_never_applies_to_full_suite_fallback() -> None:
    result = select_tests(
        CATALOG,
        EVIDENCE,
        scope=Scope.FULL_SUITE,
        full_suite_reason="config",
        policy=Policy.BUDGETED,
        budget=Budget(max_tests=1),
    )
    assert len(result.selected) == len(CATALOG) and not result.budget_limited


def test_unknown_durations_make_estimate_unknown() -> None:
    result = select_tests(CATALOG, EVIDENCE, scope=Scope.TARGETED)
    assert result.estimated_seconds is None


def test_node_ids() -> None:
    assert node_id_for("tests/test_x.py", "test_x", "test_x.test_a") == "tests/test_x.py::test_a"
    assert (
        node_id_for("tests/test_x.py", "tests.test_x", "tests.test_x.TestK.test_m")
        == "tests/test_x.py::TestK::test_m"
    )
    assert node_id_for("t.py", "t", "t.outer.<locals>.test_inner") is None
    assert node_id_for("t.py", "t", "other.test") is None
