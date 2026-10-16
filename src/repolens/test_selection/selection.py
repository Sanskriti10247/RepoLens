"""Deterministic, explainable test selection.

Every candidate test carries the evidence that made it a candidate; selection ranks candidates
by their strongest evidence and applies a policy:

- ``CONSERVATIVE`` (default): run every candidate. Prefers coverage of the change over runtime.
- ``BUDGETED``: rank candidates and keep those within a test-count or time budget. Anything cut
  is reported as excluded *by budget*, never dropped silently.

When the impact analysis cannot vouch for a targeted run (recommended scope FULL_SUITE, unknown
completeness), every test is selected and the fallback reason is recorded.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any


class EvidenceKind(StrEnum):
    TEST_CHANGED = "TEST_CHANGED"  # the test's own code changed
    STATIC_DIRECT = "STATIC_DIRECT"  # the test calls/references changed code directly
    COVERAGE = "COVERAGE"  # the test executed changed lines in a recorded run
    STATIC_TRANSITIVE = "STATIC_TRANSITIVE"  # dependency path of length > 1
    STATIC_IMPORT = "STATIC_IMPORT"  # the test module imports changed code
    STATIC_HEURISTIC = "STATIC_HEURISTIC"  # path through heuristic edges or seeds
    MODULE_DEPENDENCY = "MODULE_DEPENDENCY"  # broad scope: module-level dependency
    PREVIOUSLY_FAILED = "PREVIOUSLY_FAILED"  # failed in its most recent recorded run
    FULL_SUITE_FALLBACK = "FULL_SUITE_FALLBACK"


WEIGHTS: dict[EvidenceKind, int] = {
    EvidenceKind.TEST_CHANGED: 100,
    EvidenceKind.STATIC_DIRECT: 90,
    EvidenceKind.COVERAGE: 85,
    EvidenceKind.STATIC_TRANSITIVE: 70,
    EvidenceKind.STATIC_IMPORT: 55,
    EvidenceKind.STATIC_HEURISTIC: 45,
    EvidenceKind.MODULE_DEPENDENCY: 30,
    EvidenceKind.PREVIOUSLY_FAILED: 20,
    EvidenceKind.FULL_SUITE_FALLBACK: 10,
}


class Policy(StrEnum):
    CONSERVATIVE = "CONSERVATIVE"
    BUDGETED = "BUDGETED"


class Scope(StrEnum):
    NONE = "NONE"
    TARGETED = "TARGETED"
    BROAD = "BROAD"
    FULL_SUITE = "FULL_SUITE"


@dataclass(frozen=True, slots=True)
class TestCase:
    __test__ = False  # not a pytest test class

    node_id: str  # pytest node id relative to the repository root
    path: str
    qualified_name: str
    symbol_id: int | None = None


@dataclass(frozen=True, slots=True)
class Evidence:
    kind: EvidenceKind
    detail: Mapping[str, Any] = field(default_factory=dict)
    depth: int = 0

    @property
    def weight(self) -> int:
        # Longer dependency paths are weaker evidence than short ones.
        penalty = 5 * max(self.depth - 1, 0) if self.kind is EvidenceKind.STATIC_TRANSITIVE else 0
        return WEIGHTS[self.kind] - min(penalty, 25)


@dataclass(frozen=True, slots=True)
class Budget:
    max_tests: int | None = None
    max_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    test: TestCase
    selected: bool
    score: int
    evidence: tuple[Evidence, ...]
    estimated_seconds: float | None
    exclusion_reason: str | None = None


@dataclass
class SelectionResult:
    decisions: list[Decision]
    policy: Policy
    scope: Scope
    fallback_reason: str | None
    total_tests: int
    budget_limited: bool = False

    @property
    def selected(self) -> list[Decision]:
        return [d for d in self.decisions if d.selected]

    @property
    def excluded_by_budget(self) -> list[Decision]:
        return [d for d in self.decisions if d.exclusion_reason == "BUDGET"]

    @property
    def estimated_seconds(self) -> float | None:
        known = [d.estimated_seconds for d in self.selected if d.estimated_seconds is not None]
        if len(known) != len(self.selected):
            return None
        return round(sum(known), 3)


def select_tests(
    catalog: Iterable[TestCase],
    evidence: Mapping[str, list[Evidence]],
    *,
    scope: Scope,
    full_suite_reason: str | None = None,
    policy: Policy = Policy.CONSERVATIVE,
    budget: Budget | None = None,
    durations: Mapping[str, float] | None = None,
) -> SelectionResult:
    tests = sorted(catalog, key=lambda t: t.node_id)
    durations = durations or {}
    fallback = full_suite_reason if scope is Scope.FULL_SUITE else None
    decisions: list[Decision] = []
    for test in tests:
        found = tuple(sorted(evidence.get(test.node_id, []), key=lambda e: (-e.weight, e.kind)))
        if fallback is not None and not _counts_for_selection(found):
            found = (*found, Evidence(EvidenceKind.FULL_SUITE_FALLBACK, {"reason": fallback}))
        score = max((e.weight for e in found), default=0)
        decisions.append(
            Decision(
                test=test,
                selected=bool(found) and _counts_for_selection(found),
                score=score,
                evidence=found,
                estimated_seconds=durations.get(test.node_id),
                exclusion_reason=None if found else "NO_EVIDENCE",
            )
        )
    decisions = [
        d if d.selected or d.exclusion_reason else replace(d, exclusion_reason="HISTORY_ONLY")
        for d in decisions
    ]
    result = SelectionResult(decisions, policy, scope, fallback, total_tests=len(tests))
    if policy is Policy.BUDGETED and budget is not None and fallback is None:
        _apply_budget(result, budget)
    result.decisions.sort(key=lambda d: (not d.selected, -d.score, d.test.node_id))
    return result


def _counts_for_selection(found: tuple[Evidence, ...]) -> bool:
    """History alone (a past failure) is not a reason to run a test for *this* change."""
    return any(e.kind is not EvidenceKind.PREVIOUSLY_FAILED for e in found)


def _apply_budget(result: SelectionResult, budget: Budget) -> None:
    ranked = sorted(
        (d for d in result.decisions if d.selected),
        key=lambda d: (
            -d.score,
            d.estimated_seconds if d.estimated_seconds is not None else float("inf"),
            d.test.node_id,
        ),
    )
    keep: set[str] = set()
    seconds = 0.0
    for decision in ranked:
        over_count = budget.max_tests is not None and len(keep) >= budget.max_tests
        cost = decision.estimated_seconds or 0.0
        over_time = budget.max_seconds is not None and seconds + cost > budget.max_seconds
        if over_count or (over_time and keep):
            continue
        keep.add(decision.test.node_id)
        seconds += cost
    for index, decision in enumerate(result.decisions):
        if decision.selected and decision.test.node_id not in keep:
            result.decisions[index] = replace(decision, selected=False, exclusion_reason="BUDGET")
            result.budget_limited = True


def node_id_for(path: str, module: str, qualified_name: str) -> str | None:
    """pytest node id for a test symbol, or None if pytest would not collect it."""
    if not qualified_name.startswith(f"{module}."):
        return None
    local = qualified_name[len(module) + 1 :]
    if "<locals>" in local:
        return None
    return "::".join([path, *local.split(".")])
