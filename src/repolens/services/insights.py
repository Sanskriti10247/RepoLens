"""Repository health report for one analysis: score, metrics and rule-based findings.

Everything is derived from the stored analysis (symbols, references, edges, files); nothing
is sampled or estimated. The scoring formulas, thresholds and selection rules are documented
in docs/insights.md; the constants below are the single source for their values.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from repolens.codegraph.traversal import strongly_connected_components
from repolens.domain.enums import (
    FileAnalysisStatus,
    FileRole,
    ResolutionStatus,
    SymbolKind,
)
from repolens.persistence import insight_queries as q
from repolens.persistence.models import AnalysisRun
from repolens.services.errors import Conflict, NotFound
from repolens.services.graph import USABLE, run_context_for_analysis

TOP = 10  # list sizes in the report
EVIDENCE = 5  # evidence items per finding

WEIGHTS = {
    "complexity": 0.25,
    "documentation": 0.15,
    "testing": 0.25,
    "structure": 0.20,
    "resolution": 0.15,
}
LABELS = {
    "complexity": "Complexity",
    "documentation": "Documentation",
    "testing": "Tests",
    "structure": "Structure",
    "resolution": "Resolvable references",
}
GRADES = ((90, "A"), (75, "B"), (60, "C"), (45, "D"))
METHOD = "Weighted mean of component scores; see docs/insights.md"

COMPLEXITY_ELEVATED = 11  # first complexity value counted as elevated
COMPLEXITY_VERY_HIGH = 21  # first complexity value counted as very high
ELEVATED_SHARE_MEDIUM = 0.05  # share of functions above 10 that makes it a medium finding
COMPLEXITY_PENALTY_FACTOR = 4
STRUCTURE_PENALTY_FACTOR = 2
LONG_FUNCTION_LINES = 100
LONG_FUNCTIONS_MEDIUM = 5
LONG_MODULE_LINES = 1000
HIGH_FAN_OUT = 15
HUB_CALLERS = 25
DOC_COVERAGE_LOW = 0.8
DOC_COVERAGE_VERY_LOW = 0.5
TEST_COVERAGE_LOW = 0.8
TEST_COVERAGE_VERY_LOW = 0.5
RESOLUTION_LOW = 0.8
RESOLUTION_VERY_LOW = 0.6
INCOMPLETE_FILES_SHARE = 0.1

SEVERITIES = ("high", "medium", "low", "info")
DEAD_CODE_NOTE = (
    "Static analysis: no references found. Dynamic dispatch, plugins and public API use are "
    "not visible."
)
_INCOMPLETE = (
    FileAnalysisStatus.PARSED_WITH_ERRORS.value,
    FileAnalysisStatus.SKIPPED_TOO_LARGE.value,
    FileAnalysisStatus.SKIPPED_ENCODING.value,
    FileAnalysisStatus.FAILED.value,
)
_STATUS_KEYS = tuple(s.value for s in ResolutionStatus)
_IN_REPO_STATUSES = (
    ResolutionStatus.RESOLVED.value,
    ResolutionStatus.AMBIGUOUS.value,
    ResolutionStatus.UNRESOLVED.value,
    ResolutionStatus.DYNAMIC.value,
)


# -- scoring --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Component:
    key: str
    score: int | None  # None: no data, excluded from the overall score
    detail: str
    weight: float = 0.0

    def out(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": LABELS[self.key],
            "score": self.score,
            "weight": self.weight,
            "detail": self.detail,
        }


def grade_for(score: int) -> str:
    for threshold, grade in GRADES:
        if score >= threshold:
            return grade
    return "F"


def _pct(value: float) -> int:
    return round(100 * max(0.0, min(1.0, value)))


def complexity_component(functions: int, measured: int, buckets: dict[str, int]) -> Component:
    if measured == 0:
        if functions == 0:
            return Component("complexity", None, "No functions or methods to measure.")
        return Component(
            "complexity",
            None,
            "Not measured: this analysis was parsed before complexity was recorded. "
            "Re-run the analysis to include it.",
        )
    elevated, very_high = buckets["11-20"], buckets["21+"]
    penalty = (elevated + 2 * very_high) / measured
    return Component(
        "complexity",
        _pct(1 - COMPLEXITY_PENALTY_FACTOR * penalty),
        f"{elevated + very_high} of {measured} functions above complexity 10 "
        f"({very_high} above 20)",
    )


def documentation_component(public: int, documented: int) -> Component:
    if public == 0:
        return Component("documentation", None, "No public classes, functions or methods.")
    return Component(
        "documentation",
        _pct(documented / public),
        f"{documented} of {public} public classes, functions and methods have a docstring",
    )


def testing_component(with_tests: int, without_tests: int) -> Component:
    total = with_tests + without_tests
    if total == 0:
        return Component("testing", None, "No non-test modules with definitions.")
    return Component(
        "testing",
        _pct(with_tests / total),
        f"{with_tests} of {total} modules are imported or referenced by test code",
    )


def structure_component(modules: int, in_cycles: int, high_fan_out: int) -> Component:
    if modules == 0:
        return Component("structure", None, "No non-test modules.")
    penalty = STRUCTURE_PENALTY_FACTOR * (in_cycles + high_fan_out) / modules
    return Component(
        "structure",
        _pct(1 - penalty),
        f"{in_cycles} of {modules} modules in import cycles; {high_fan_out} depend on more "
        f"than {HIGH_FAN_OUT} other modules",
    )


def resolution_rate(counts: dict[str, int]) -> float | None:
    """Share of references that could point into the repository and resolved to one target."""
    candidates = sum(counts.get(status, 0) for status in _IN_REPO_STATUSES)
    if candidates == 0:
        return None
    return counts.get(ResolutionStatus.RESOLVED.value, 0) / candidates


def resolution_component(counts: dict[str, int]) -> Component:
    rate = resolution_rate(counts)
    if rate is None:
        return Component("resolution", None, "No references to resolve.")
    candidates = sum(counts.get(status, 0) for status in _IN_REPO_STATUSES)
    resolved = counts.get(ResolutionStatus.RESOLVED.value, 0)
    return Component(
        "resolution",
        _pct(rate),
        f"{resolved} of {candidates} references resolved to a single definition "
        "(references to the standard library and third-party packages are excluded)",
    )


def combine(components: list[Component]) -> tuple[int | None, list[Component]]:
    """Overall score and the components with the weights actually applied.

    Components without data get weight 0 and the remaining weights are rescaled to sum to 1,
    so ``overall == round(sum(weight * score))`` always holds for the returned weights.
    """
    available = [c for c in components if c.score is not None]
    total = sum(WEIGHTS[c.key] for c in available)
    if not available or total == 0:
        return None, [Component(c.key, c.score, c.detail, 0.0) for c in components]
    rescaled = len(available) < len(components)
    result: list[Component] = []
    overall = 0.0
    for c in components:
        if c.score is None:
            note = " Excluded from the overall score; the other weights are rescaled."
            result.append(Component(c.key, None, c.detail + note, 0.0))
            continue
        weight = WEIGHTS[c.key] / total
        overall += weight * c.score
        result.append(Component(c.key, c.score, c.detail, round(weight, 4) if rescaled else weight))
    return round(overall), result


# -- findings -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Evidence:
    label: str
    path: str | None = None
    line: int | None = None
    symbol_id: int | None = None
    file_id: int | None = None

    def out(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "symbol_id": self.symbol_id,
            "file_id": self.file_id,
            "label": self.label,
        }


@dataclass(frozen=True, slots=True)
class Finding:
    id: str
    severity: str
    category: str
    title: str
    detail: str
    evidence: list[Evidence]

    def out(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity,
            "category": self.category,
            "title": self.title,
            "detail": self.detail,
            "evidence": [e.out() for e in self.evidence],
        }


@dataclass(frozen=True, slots=True)
class ModuleStat:
    module: q.ModuleRow
    fan_in: int  # distinct non-test modules that depend on it
    fan_out: int  # distinct non-test modules it depends on
    tested: bool


@dataclass
class Facts:
    """Everything the finding rules look at (also the input of the unit tests)."""

    stats: q.FunctionStats
    most_complex: list[q.SymbolItem] = field(default_factory=list)
    elevated: list[q.SymbolItem] = field(default_factory=list)  # most complex below 21
    longest: list[q.SymbolItem] = field(default_factory=list)
    long_modules: int = 0
    long_module_items: list[q.SymbolItem] = field(default_factory=list)
    public: int = 0
    documented: int = 0
    undocumented: list[q.SymbolItem] = field(default_factory=list)
    test_files: int = 0
    testable: list[ModuleStat] = field(default_factory=list)  # non-test modules with code
    cycles: list[list[q.ModuleRow]] = field(default_factory=list)
    high_fan_out: list[ModuleStat] = field(default_factory=list)
    hubs: list[q.SymbolItem] = field(default_factory=list)
    dead_code: int = 0
    dead_code_items: list[q.SymbolItem] = field(default_factory=list)
    references: dict[str, int] = field(default_factory=dict)
    file_statuses: dict[str, int] = field(default_factory=dict)


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _symbol_evidence(item: q.SymbolItem, label: str) -> Evidence:
    return Evidence(label, item.path, item.line, item.symbol_id, item.file_id)


def _module_evidence(module: q.ModuleRow, label: str) -> Evidence:
    return Evidence(label, module.path, 1, module.symbol_id, module.file_id)


def _complexity_findings(facts: Facts) -> list[Finding]:
    found: list[Finding] = []
    stats = facts.stats
    very_high, elevated = stats.buckets["21+"], stats.buckets["11-20"]
    if very_high:
        items = [i for i in facts.most_complex if (i.value or 0) >= COMPLEXITY_VERY_HIGH]
        found.append(
            Finding(
                "complexity.very_high",
                "high",
                "complexity",
                f"{_plural(very_high, 'function is', 'functions are')} very complex (21+)",
                "A function with more than 20 independent paths is hard to follow and "
                "practically impossible to test path by path. Split the largest ones into "
                "smaller functions with one job each, starting with those listed.",
                [_symbol_evidence(i, f"{i.qualified_name} ({i.value})") for i in items][:EVIDENCE],
            )
        )
    if elevated:
        share = (elevated + very_high) / max(stats.measured, 1)
        items = [i for i in facts.elevated if (i.value or 0) >= COMPLEXITY_ELEVATED]
        found.append(
            Finding(
                "complexity.elevated",
                "medium" if share >= ELEVATED_SHARE_MEDIUM else "low",
                "complexity",
                f"{_plural(elevated, 'function has', 'functions have')} complexity 11-20",
                "These functions branch enough that changes to them deserve extra review and "
                "tests for each branch. Consider extracting the branches into helpers.",
                [_symbol_evidence(i, f"{i.qualified_name} ({i.value})") for i in items][:EVIDENCE],
            )
        )
    return found


def _size_findings(facts: Facts) -> list[Finding]:
    found: list[Finding] = []
    stats = facts.stats
    if stats.over_100_lines:
        items = [i for i in facts.longest if i.lines > LONG_FUNCTION_LINES]
        found.append(
            Finding(
                "size.long_functions",
                "medium" if stats.over_100_lines >= LONG_FUNCTIONS_MEDIUM else "low",
                "size",
                f"{_plural(stats.over_100_lines, 'function is', 'functions are')} longer "
                f"than {LONG_FUNCTION_LINES} lines",
                "Long functions usually do several things at once, which makes them harder "
                "to review and reuse. Look for steps that can become named helpers.",
                [_symbol_evidence(i, f"{i.qualified_name} ({i.lines} lines)") for i in items][
                    :EVIDENCE
                ],
            )
        )
    if facts.long_modules:
        found.append(
            Finding(
                "size.long_modules",
                "low",
                "size",
                f"{_plural(facts.long_modules, 'module is', 'modules are')} longer than "
                f"{LONG_MODULE_LINES} lines",
                "Very long modules tend to mix responsibilities and attract merge conflicts. "
                "Consider splitting them along the groups of classes and functions they hold.",
                [
                    _symbol_evidence(i, f"{i.path} ({i.value} lines)")
                    for i in facts.long_module_items
                ][:EVIDENCE],
            )
        )
    return found


def _documentation_findings(facts: Facts) -> list[Finding]:
    found: list[Finding] = []
    if facts.public:
        coverage = facts.documented / facts.public
        if coverage < DOC_COVERAGE_LOW:
            missing = facts.public - facts.documented
            found.append(
                Finding(
                    "documentation.low_coverage",
                    "medium" if coverage < DOC_COVERAGE_VERY_LOW else "low",
                    "documentation",
                    f"{_plural(missing, 'public symbol has', 'public symbols have')} no "
                    f"docstring ({round(100 * coverage)}% documented)",
                    "Public classes and functions are what other code and other people use. "
                    "Document the most called ones first; they are listed by caller count.",
                    [
                        _symbol_evidence(
                            i, f"{i.qualified_name} ({_plural(i.value or 0, 'caller')})"
                        )
                        for i in facts.undocumented
                    ][:EVIDENCE],
                )
            )
    return found


def _testing_findings(facts: Facts) -> list[Finding]:
    found: list[Finding] = []
    untested = [m for m in facts.testable if not m.tested]
    if facts.testable and facts.test_files == 0:
        found.append(
            Finding(
                "testing.no_tests",
                "high",
                "testing",
                "No test files found",
                "No file in the repository looks like a test (test_*.py, *.test.ts, "
                "*_test.go, a tests/ directory and similar), so changes cannot be checked "
                "automatically. Start with tests for the most depended-upon modules.",
                [
                    _module_evidence(m.module, f"{m.module.name} (used by {m.fan_in})")
                    for m in sorted(untested, key=lambda m: (-m.fan_in, m.module.name))
                ][:EVIDENCE],
            )
        )
    elif untested:
        coverage = 1 - len(untested) / len(facts.testable)
        severity = "low"
        if coverage < TEST_COVERAGE_VERY_LOW:
            severity = "high"
        elif coverage < TEST_COVERAGE_LOW:
            severity = "medium"
        found.append(
            Finding(
                "testing.untested_modules",
                severity,
                "testing",
                f"{_plural(len(untested), 'module is', 'modules are')} never imported or "
                "referenced by tests",
                "No test file imports or calls anything in these modules, so a change there "
                "is not checked by any test that RepoLens can see. Modules many others "
                "depend on are listed first.",
                [
                    _module_evidence(m.module, f"{m.module.name} (used by {m.fan_in})")
                    for m in sorted(untested, key=lambda m: (-m.fan_in, m.module.name))
                ][:EVIDENCE],
            )
        )
    return found


def _structure_findings(facts: Facts) -> list[Finding]:
    found: list[Finding] = []
    if facts.cycles:
        members = sum(len(c) for c in facts.cycles)
        largest = max(len(c) for c in facts.cycles)
        found.append(
            Finding(
                "structure.import_cycles",
                "medium",
                "structure",
                f"{_plural(len(facts.cycles), 'import cycle')} across {_plural(members, 'module')}",
                "Modules that import each other at the top level cannot be understood, "
                "tested or reused separately, and the cycle can break at import time. Move "
                "the shared code into a module both can import, or import lazily. "
                f"The largest cycle has {largest} modules.",
                [
                    _module_evidence(m, f"{m.name} (cycle of {len(cycle)})")
                    for cycle in facts.cycles
                    for m in cycle
                ][:EVIDENCE],
            )
        )
    if facts.high_fan_out:
        found.append(
            Finding(
                "structure.high_fan_out",
                "low",
                "structure",
                f"{_plural(len(facts.high_fan_out), 'module depends', 'modules depend')} on "
                f"more than {HIGH_FAN_OUT} other modules",
                "A module that depends on many others changes whenever any of them does. "
                "This is expected for entry points; elsewhere it often means the module does "
                "too much.",
                [
                    _module_evidence(m.module, f"{m.module.name} (depends on {m.fan_out})")
                    for m in facts.high_fan_out
                ][:EVIDENCE],
            )
        )
    hubs = [h for h in facts.hubs if (h.value or 0) >= HUB_CALLERS]
    if hubs:
        found.append(
            Finding(
                "structure.hubs",
                "info",
                "structure",
                f"{_plural(len(hubs), 'definition has', 'definitions have')} "
                f"{HUB_CALLERS} or more callers",
                "Changes to these ripple widely. Keep their behaviour stable and well "
                "tested; RepoLens's impact analysis will flag their callers when they change.",
                [_symbol_evidence(h, f"{h.qualified_name} ({h.value} callers)") for h in hubs][
                    :EVIDENCE
                ],
            )
        )
    return found


def _dead_code_findings(facts: Facts) -> list[Finding]:
    found: list[Finding] = []
    if facts.dead_code:
        found.append(
            Finding(
                "dead_code.candidates",
                "low",
                "dead_code",
                f"{_plural(facts.dead_code, 'private definition has', 'private definitions have')}"
                " no references",
                "Nothing in the repository calls, imports or references these private or local "
                "definitions. Check for dynamic use, then delete what is unused. " + DEAD_CODE_NOTE,
                [
                    _symbol_evidence(i, f"{i.qualified_name} ({i.lines} lines)")
                    for i in facts.dead_code_items
                ][:EVIDENCE],
            )
        )
    return found


def _resolution_findings(facts: Facts) -> list[Finding]:
    found: list[Finding] = []
    rate = resolution_rate(facts.references)
    if rate is not None and rate < RESOLUTION_LOW:
        refs = facts.references
        found.append(
            Finding(
                "resolution.low",
                "medium" if rate < RESOLUTION_VERY_LOW else "low",
                "resolution",
                f"Only {round(100 * rate)}% of in-repository references could be resolved",
                "Calls on objects of unknown type, dynamic attribute access and ambiguous "
                "names leave gaps in the dependency graph, so impact analysis and test "
                "selection are less complete for this repository. Type annotations on "
                "parameters and attributes help.",
                [
                    Evidence(f"{refs.get(status, 0)} {status.lower()}")
                    for status in _IN_REPO_STATUSES[1:]
                    if refs.get(status, 0)
                ],
            )
        )
    return found


def _analysis_findings(facts: Facts) -> list[Finding]:
    found: list[Finding] = []
    incomplete = {s: n for s, n in facts.file_statuses.items() if s in _INCOMPLETE and n}
    if incomplete:
        total = sum(incomplete.values())
        code_files = total + facts.file_statuses.get(FileAnalysisStatus.PARSED.value, 0)
        share = total / code_files
        found.append(
            Finding(
                "analysis.incomplete_files",
                "medium" if share >= INCOMPLETE_FILES_SHARE else "info",
                "analysis",
                f"{_plural(total, 'source file was', 'source files were')} not fully analysed",
                "Syntax errors, oversized or undecodable files leave parts of the code out of "
                "every number in this report. See the analysis warnings for the files.",
                [
                    Evidence(f"{n} {s.lower().replace('_', ' ')}")
                    for s, n in sorted(incomplete.items())
                ],
            )
        )
    return found


_RULES = (
    _complexity_findings,
    _size_findings,
    _documentation_findings,
    _testing_findings,
    _structure_findings,
    _dead_code_findings,
    _resolution_findings,
    _analysis_findings,
)


def build_findings(facts: Facts) -> list[Finding]:
    found = [finding for rule in _RULES for finding in rule(facts)]
    order = {s: i for i, s in enumerate(SEVERITIES)}
    return sorted(found, key=lambda f: (order[f.severity], -len(f.evidence), f.id))


# -- report ---------------------------------------------------------------------------------


def _role_key(role: str) -> str:
    return "docs" if role == FileRole.DOC.value else role.lower()


def _symbol_out(item: q.SymbolItem, **extra: Any) -> dict[str, Any]:
    return {
        "symbol_id": item.symbol_id,
        "qualified_name": item.qualified_name,
        "kind": item.kind,
        "file_id": item.file_id,
        "path": item.path,
        "line": item.line,
        **extra,
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def module_stats(
    modules: list[q.ModuleRow], edges: list[q.FileEdge], runtime_imports: list[tuple[int, int]]
) -> tuple[dict[int, ModuleStat], list[list[q.ModuleRow]]]:
    """Fan-in/out between non-test modules, test reachability and import-time cycles."""
    by_file = {m.file_id: m for m in modules}
    test_files = {m.file_id for m in modules if m.is_test}
    dependents: dict[int, set[int]] = defaultdict(set)
    dependencies: dict[int, set[int]] = defaultdict(set)
    tested: set[int] = set()
    for edge in edges:
        src, dst = edge.src_file_id, edge.dst_file_id
        if dst not in by_file or src not in by_file or dst in test_files:
            continue
        if src in test_files:
            tested.add(dst)
            continue
        dependents[dst].add(src)
        dependencies[src].add(dst)
    stats = {
        m.file_id: ModuleStat(
            m, len(dependents[m.file_id]), len(dependencies[m.file_id]), m.file_id in tested
        )
        for m in modules
        if not m.is_test
    }
    imports = [(a, b) for a, b in runtime_imports if a in stats and b in stats]
    cycles = [
        [by_file[f] for f in component]
        for component in strongly_connected_components(sorted(stats), imports)
    ]
    cycles.sort(key=lambda c: (-len(c), c[0].name))
    return stats, cycles


def analysis_insights(session: Session, analysis_id: uuid.UUID) -> dict[str, Any]:
    run = session.get(AnalysisRun, analysis_id)
    if run is None:
        raise NotFound("Analysis not found")
    if run.status not in USABLE:
        raise Conflict(
            f"Analysis is {run.status}; insights are available once it has finished",
            code="analysis_not_ready",
            details={"status": run.status},
        )
    ctx = run_context_for_analysis(session, analysis_id)
    run_id = ctx.run.id

    groups = q.file_groups(session, ctx.snapshot.id)
    roles: dict[str, int] = defaultdict(int)
    languages: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    counted: set[str] = set()  # languages with line counts (older snapshots have none)
    for g in groups:
        roles[_role_key(g.role)] += g.files
        if g.language:
            languages[g.language][0] += g.files
            languages[g.language][1] += g.bytes
            languages[g.language][2] += g.lines or 0
            if g.lines is not None:
                counted.add(g.language)
    kinds = q.symbol_kinds(session, run_id)

    stats = q.function_stats(session, run_id)
    available = stats.measured > 0
    most_complex = q.most_complex(session, run_id, limit=TOP) if available else []
    elevated = (
        q.most_complex(session, run_id, limit=EVIDENCE, below=COMPLEXITY_VERY_HIGH)
        if stats.buckets["11-20"]
        else []
    )
    longest = q.longest_functions(session, run_id, limit=TOP)
    long_count, long_items = q.long_modules(
        session, run_id, min_lines=LONG_MODULE_LINES, limit=EVIDENCE
    )
    public, documented = q.documentation_counts(session, run_id)
    undocumented = q.undocumented(session, run_id, limit=TOP)

    modules = q.modules(session, run_id)
    per_module, cycles = module_stats(
        modules, q.file_edges(session, run_id), q.runtime_imports(session, run_id)
    )
    testable = [m for m in per_module.values() if m.module.definitions > 0]
    untested = sorted(
        (m for m in testable if not m.tested),
        key=lambda m: (-m.fan_in, -m.module.definitions, m.module.name),
    )
    in_cycles = sum(len(c) for c in cycles)
    high_fan_out = sorted(
        (m for m in per_module.values() if m.fan_out > HIGH_FAN_OUT),
        key=lambda m: (-m.fan_out, m.module.name),
    )
    hubs = q.hubs(session, run_id, limit=TOP)
    dead_count, dead_items = q.dead_code(session, run_id, limit=TOP)
    references = q.reference_counts(session, run_id)
    test_files = roles.get("test", 0)

    overall, components = combine(
        [
            complexity_component(stats.functions, stats.measured, stats.buckets),
            documentation_component(public, documented),
            testing_component(len(testable) - len(untested), len(untested)),
            structure_component(len(per_module), in_cycles, len(high_fan_out)),
            resolution_component(references),
        ]
    )
    findings = build_findings(
        Facts(
            stats=stats,
            most_complex=most_complex,
            elevated=elevated,
            longest=longest,
            long_modules=long_count,
            long_module_items=long_items,
            public=public,
            documented=documented,
            undocumented=undocumented,
            test_files=test_files,
            testable=testable,
            cycles=cycles,
            high_fan_out=high_fan_out,
            hubs=hubs,
            dead_code=dead_count,
            dead_code_items=dead_items,
            references=references,
            file_statuses=q.file_status_counts(session, run_id),
        )
    )
    coupling = sorted(per_module.values(), key=lambda m: (-m.fan_in, -m.fan_out, m.module.name))
    rate = resolution_rate(references)
    return {
        "analysis_id": analysis_id,
        "results_run_id": run_id,
        "commit_sha": ctx.snapshot.commit_sha,
        "score": {
            "overall": overall,
            "grade": grade_for(overall) if overall is not None else None,
            "components": [c.out() for c in components],
            "method": METHOD,
        },
        "overview": {
            "files": sum(g.files for g in groups),
            "source_files": roles.get("source", 0),
            "test_files": test_files,
            "bytes": sum(g.bytes for g in groups),
            "languages": [
                {
                    "language": name,
                    "files": files,
                    "bytes": size,
                    "lines": lines if name in counted else None,
                }
                for name, (files, size, lines) in sorted(
                    languages.items(), key=lambda kv: (-kv[1][1], kv[0])
                )
            ],
            "roles": dict(sorted(roles.items())),
            "modules": kinds.get(SymbolKind.MODULE.value, 0),
            "symbols": sum(kinds.values()),
            "functions": kinds.get(SymbolKind.FUNCTION.value, 0)
            + kinds.get(SymbolKind.ASYNC_FUNCTION.value, 0),
            "classes": kinds.get(SymbolKind.CLASS.value, 0),
            "methods": kinds.get(SymbolKind.METHOD.value, 0)
            + kinds.get(SymbolKind.ASYNC_METHOD.value, 0),
        },
        "complexity": {
            "available": available,
            "functions": stats.measured,
            "distribution": [
                {"bucket": bucket, "count": count if available else 0}
                for bucket, count in stats.buckets.items()
            ],
            "mean": round(stats.mean, 2) if stats.mean is not None else None,
            "p90": stats.p90,
            "top": [_symbol_out(i, complexity=i.value, lines=i.lines) for i in most_complex],
        },
        "size": {
            "longest_functions": [_symbol_out(i, lines=i.lines) for i in longest],
            "largest_files": [
                {
                    "file_id": f.file_id,
                    "path": f.path,
                    "bytes": f.bytes,
                    "symbols": f.symbols,
                    "lines": f.lines,
                }
                for f in q.largest_files(session, run_id, limit=TOP)
            ],
        },
        "documentation": {
            "public_symbols": public,
            "documented": documented,
            "coverage": _ratio(documented, public),
            "undocumented": [_symbol_out(i, callers=i.value) for i in undocumented],
        },
        "testing": {
            "test_files": test_files,
            "test_functions": q.test_function_count(session, run_id),
            "modules_with_tests": len(testable) - len(untested),
            "modules_without_tests": len(untested),
            "coverage": _ratio(len(testable) - len(untested), len(testable)),
            "untested_modules": [
                {
                    "module": m.module.name,
                    "symbol_id": m.module.symbol_id,
                    "file_id": m.module.file_id,
                    "path": m.module.path,
                    "symbols": m.module.definitions,
                    "fan_in": m.fan_in,
                }
                for m in untested[:TOP]
            ],
        },
        "coupling": {
            "modules": [
                {
                    "module": m.module.name,
                    "symbol_id": m.module.symbol_id,
                    "path": m.module.path,
                    "fan_in": m.fan_in,
                    "fan_out": m.fan_out,
                    "instability": _ratio(m.fan_out, m.fan_in + m.fan_out),
                }
                for m in coupling[:TOP]
            ],
            "cycles": [[m.name for m in cycle] for cycle in cycles[:TOP]],
            "hubs": [_symbol_out(h, callers=h.value) for h in hubs],
        },
        "dead_code": {
            "count": dead_count,
            "candidates": [_symbol_out(i, lines=i.lines) for i in dead_items],
            "note": DEAD_CODE_NOTE,
        },
        "references": {
            **{status.lower(): references.get(status, 0) for status in _STATUS_KEYS},
            "resolution_rate": round(rate, 4) if rate is not None else None,
        },
        "findings": [f.out() for f in findings],
    }
