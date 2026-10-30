"""Health score components, grade bands and finding rules (docs/insights.md)."""

from __future__ import annotations

import pytest

from repolens.persistence.insight_queries import FunctionStats, ModuleRow, SymbolItem
from repolens.services import insights as ins


def stats(
    *, measured: int = 100, elevated: int = 0, very_high: int = 0, long: int = 0
) -> FunctionStats:
    return FunctionStats(
        functions=measured,
        measured=measured,
        mean=3.0 if measured else None,
        p90=6 if measured else None,
        buckets={
            "1-5": measured - elevated - very_high,
            "6-10": 0,
            "11-20": elevated,
            "21+": very_high,
        },
        over_100_lines=long,
    )


def item(name: str, value: int | None = None, lines: int = 10) -> SymbolItem:
    return SymbolItem(1, name, "FUNCTION", 7, "pkg/mod.py", 3, lines, value)


def module(name: str, *, is_test: bool = False, definitions: int = 3) -> ModuleRow:
    key = sum(map(ord, name))
    return ModuleRow(key, name, key, f"{name}.py", is_test, definitions)


@pytest.mark.parametrize(
    ("score", "grade"),
    [
        (100, "A"),
        (90, "A"),
        (89, "B"),
        (75, "B"),
        (74, "C"),
        (60, "C"),
        (59, "D"),
        (45, "D"),
        (44, "F"),
        (0, "F"),
    ],
)
def test_grade_bands(score: int, grade: str) -> None:
    assert ins.grade_for(score) == grade


def test_complexity_component() -> None:
    clean = ins.complexity_component(100, 100, stats().buckets)
    assert clean.score == 100
    # penalty = (5 + 2 * 1) / 100 = 0.07; score = 100 * (1 - 4 * 0.07) = 72
    mixed = ins.complexity_component(100, 100, stats(elevated=5, very_high=1).buckets)
    assert mixed.score == 72
    assert mixed.detail == "6 of 100 functions above complexity 10 (1 above 20)"
    floor = ins.complexity_component(10, 10, stats(measured=10, very_high=5).buckets)
    assert floor.score == 0  # clamped, never negative


def test_complexity_missing_for_old_analyses() -> None:
    old = ins.complexity_component(50, 0, stats(measured=0).buckets)
    assert old.score is None
    assert "before complexity was recorded" in old.detail


def test_ratio_components() -> None:
    assert ins.documentation_component(200, 150).score == 75
    assert ins.documentation_component(0, 0).score is None
    assert ins.testing_component(3, 1).score == 75
    assert ins.testing_component(0, 0).score is None
    # 2 modules in cycles + 1 with high fan-out of 20: 100 * (1 - 2 * 3 / 20) = 70
    assert ins.structure_component(20, 2, 1).score == 70
    assert ins.structure_component(0, 0, 0).score is None


def test_resolution_excludes_external_references() -> None:
    counts = {"RESOLVED": 80, "UNRESOLVED": 15, "DYNAMIC": 5, "EXTERNAL": 900}
    assert ins.resolution_rate(counts) == pytest.approx(0.8)
    assert ins.resolution_component(counts).score == 80
    assert ins.resolution_rate({"EXTERNAL": 3}) is None


def test_combine_is_the_weighted_mean() -> None:
    parts = [
        ins.Component("complexity", 80, ""),
        ins.Component("documentation", 60, ""),
        ins.Component("testing", 50, ""),
        ins.Component("structure", 100, ""),
        ins.Component("resolution", 70, ""),
    ]
    overall, weighted = ins.combine(parts)
    # 0.25*80 + 0.15*60 + 0.25*50 + 0.20*100 + 0.15*70 = 72
    assert overall == 72
    assert [c.weight for c in weighted] == [0.25, 0.15, 0.25, 0.20, 0.15]


def test_combine_rescales_when_a_component_is_missing() -> None:
    parts = [
        ins.Component("complexity", None, "Not measured."),
        ins.Component("documentation", 60, ""),
        ins.Component("testing", 50, ""),
        ins.Component("structure", 100, ""),
        ins.Component("resolution", 70, ""),
    ]
    overall, weighted = ins.combine(parts)
    # (0.15*60 + 0.25*50 + 0.20*100 + 0.15*70) / 0.75 = 69.33
    assert overall == 69
    assert weighted[0].weight == 0
    assert "Excluded from the overall score" in weighted[0].detail
    assert sum(c.weight for c in weighted) == pytest.approx(1, abs=1e-3)
    assert weighted[1].weight == pytest.approx(0.2)


def test_combine_with_no_data() -> None:
    overall, _ = ins.combine([ins.Component("testing", None, "")])
    assert overall is None


def ids(facts: ins.Facts) -> dict[str, str]:
    return {f.id: f.severity for f in ins.build_findings(facts)}


def test_clean_repository_has_no_findings() -> None:
    tested = ins.ModuleStat(module("pkg.a"), 1, 0, tested=True)
    facts = ins.Facts(
        stats=stats(),
        public=10,
        documented=10,
        test_files=2,
        testable=[tested],
        references={"RESOLVED": 9, "UNRESOLVED": 1},
        file_statuses={"PARSED": 5},
    )
    assert ins.build_findings(facts) == []


def test_complexity_rules_and_thresholds() -> None:
    facts = ins.Facts(
        stats=stats(measured=100, elevated=4, very_high=2),
        most_complex=[item("pkg.mod.huge", 30), item("pkg.mod.big", 21), item("pkg.mod.mid", 15)],
        elevated=[item("pkg.mod.mid", 15), item("pkg.mod.low", 9)],
    )
    findings = {f.id: f for f in ins.build_findings(facts)}
    very_high = findings["complexity.very_high"]
    assert very_high.severity == "high"
    assert very_high.title == "2 functions are very complex (21+)"
    assert [e.label for e in very_high.evidence] == ["pkg.mod.huge (30)", "pkg.mod.big (21)"]
    elevated = findings["complexity.elevated"]
    assert elevated.severity == "medium"  # 6 of 100 above 10 is at least 5%
    assert [e.label for e in elevated.evidence] == ["pkg.mod.mid (15)"]
    assert elevated.evidence[0].path == "pkg/mod.py"
    assert elevated.evidence[0].line == 3
    few = ins.Facts(stats=stats(measured=100, elevated=2), elevated=[item("pkg.mod.mid", 12)])
    assert ids(few)["complexity.elevated"] == "low"


def test_size_rules() -> None:
    facts = ins.Facts(
        stats=stats(long=1),
        longest=[item("pkg.mod.long", lines=140), item("pkg.mod.short", lines=40)],
        long_modules=1,
        long_module_items=[item("pkg.mod", 1500)],
    )
    findings = {f.id: f for f in ins.build_findings(facts)}
    assert findings["size.long_functions"].severity == "low"
    assert [e.label for e in findings["size.long_functions"].evidence] == [
        "pkg.mod.long (140 lines)"
    ]
    assert findings["size.long_modules"].title == "1 module is longer than 1000 lines"
    assert ids(ins.Facts(stats=stats(long=5)))["size.long_functions"] == "medium"


def test_documentation_rule_thresholds() -> None:
    assert "documentation.low_coverage" not in ids(ins.Facts(stats(), public=10, documented=8))
    assert ids(ins.Facts(stats(), public=10, documented=7))["documentation.low_coverage"] == "low"
    assert (
        ids(ins.Facts(stats(), public=10, documented=4))["documentation.low_coverage"] == "medium"
    )


def test_testing_rules() -> None:
    a = ins.ModuleStat(module("pkg.a"), 5, 0, tested=False)
    b = ins.ModuleStat(module("pkg.b"), 9, 0, tested=False)
    c = ins.ModuleStat(module("pkg.c"), 1, 0, tested=True)
    none = ins.build_findings(ins.Facts(stats(), test_files=0, testable=[a, b, c]))
    assert [f.id for f in none] == ["testing.no_tests"]
    assert none[0].severity == "high"
    untested = ins.build_findings(ins.Facts(stats(), test_files=3, testable=[a, b, c]))
    assert untested[0].id == "testing.untested_modules"
    assert untested[0].severity == "high"  # 1 of 3 tested is below 50%
    assert [e.label for e in untested[0].evidence] == ["pkg.b (used by 9)", "pkg.a (used by 5)"]
    mostly = [ins.ModuleStat(module(f"pkg.t{i}"), 0, 0, tested=True) for i in range(9)]
    assert ids(ins.Facts(stats(), test_files=3, testable=[a, *mostly])) == {
        "testing.untested_modules": "low"
    }


def test_structure_rules() -> None:
    cycle = [module("pkg.a"), module("pkg.b")]
    busy = ins.ModuleStat(module("pkg.main"), 0, 20, tested=True)
    hub = item("pkg.mod.echo", 40)
    quiet = item("pkg.mod.rare", 3)
    facts = ins.Facts(stats(), cycles=[cycle], high_fan_out=[busy], hubs=[hub, quiet])
    findings = {f.id: f for f in ins.build_findings(facts)}
    assert findings["structure.import_cycles"].severity == "medium"
    assert findings["structure.import_cycles"].title == "1 import cycle across 2 modules"
    assert findings["structure.high_fan_out"].evidence[0].label == "pkg.main (depends on 20)"
    assert [e.label for e in findings["structure.hubs"].evidence] == ["pkg.mod.echo (40 callers)"]


def test_dead_code_resolution_and_incomplete_files() -> None:
    facts = ins.Facts(
        stats(),
        dead_code=2,
        dead_code_items=[item("pkg.mod._old", lines=12), item("pkg.mod._older", lines=4)],
        references={"RESOLVED": 50, "UNRESOLVED": 40, "DYNAMIC": 10, "EXTERNAL": 500},
        file_statuses={"PARSED": 8, "PARSED_WITH_ERRORS": 2, "SKIPPED_BINARY": 40},
    )
    findings = {f.id: f for f in ins.build_findings(facts)}
    assert findings["dead_code.candidates"].severity == "low"
    assert "no references found" in findings["dead_code.candidates"].detail
    assert findings["resolution.low"].severity == "medium"  # 50% is below 60%
    assert [e.label for e in findings["resolution.low"].evidence] == [
        "40 unresolved",
        "10 dynamic",
    ]
    incomplete = findings["analysis.incomplete_files"]
    assert incomplete.severity == "medium"  # 2 of 10 Python files
    assert incomplete.evidence[0].label == "2 parsed with errors"


def test_findings_sorted_by_severity_then_evidence() -> None:
    facts = ins.Facts(
        stats(measured=100, very_high=1, long=1),
        most_complex=[item("pkg.mod.huge", 30)],
        longest=[item("a", lines=101), item("b", lines=120)],
        hubs=[item("pkg.mod.echo", 40)],
        dead_code=1,
        dead_code_items=[item("pkg.mod._x")],
    )
    order = [(f.severity, len(f.evidence)) for f in ins.build_findings(facts)]
    assert order == [("high", 1), ("low", 2), ("low", 1), ("info", 1)]
