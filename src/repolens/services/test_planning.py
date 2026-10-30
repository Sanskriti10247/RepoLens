"""Test plans: turn an impact report into an explainable, stored selection of pytest tests."""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, aliased

from repolens.domain.enums import ChangeSetStatus, EdgeType, SymbolKind
from repolens.parsers.treesitter import PARSED_LANGUAGES
from repolens.persistence.models import (
    AnalysisFile,
    AnalysisRun,
    ChangedFile,
    ChangeSet,
    CoverageRecord,
    DependencyEdge,
    ImpactItem,
    ImpactReport,
    RepositorySnapshot,
    SourceFile,
    Symbol,
    TestCaseRecord,
    TestPlan,
    TestPlanItem,
    TestResultRecord,
    TestRun,
)
from repolens.services.errors import InvalidInput, NotFound
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

_STRENGTH_TO_EVIDENCE = {
    "CHANGED": EvidenceKind.TEST_CHANGED,
    "DIRECT": EvidenceKind.STATIC_DIRECT,
    "TRANSITIVE": EvidenceKind.STATIC_TRANSITIVE,
    "IMPORT_ONLY": EvidenceKind.STATIC_IMPORT,
    "HEURISTIC": EvidenceKind.STATIC_HEURISTIC,
}
_TEST_FUNCTIONS = (
    SymbolKind.FUNCTION.value,
    SymbolKind.ASYNC_FUNCTION.value,
    SymbolKind.METHOD.value,
    SymbolKind.ASYNC_METHOD.value,
)


def ensure_test_catalog(session: Session, run_id: uuid.UUID) -> list[TestCase]:
    """Static test discovery from the analysis (idempotent). Repository code is not run."""
    rows = session.execute(
        select(Symbol.id, Symbol.qualified_name, SourceFile.path, AnalysisFile.module_name)
        .join(SourceFile, SourceFile.id == Symbol.file_id)
        .join(
            AnalysisFile,
            (AnalysisFile.file_id == Symbol.file_id) & (AnalysisFile.analysis_run_id == run_id),
        )
        .where(
            Symbol.analysis_run_id == run_id,
            Symbol.is_test.is_(True),
            Symbol.kind.in_(_TEST_FUNCTIONS),
        )
    ).all()
    values = []
    for symbol_id, qualified_name, path, module in rows:
        node_id = node_id_for(path, module or "", qualified_name) if module else None
        if node_id is not None:
            values.append(
                {
                    "analysis_run_id": run_id,
                    "symbol_id": symbol_id,
                    "node_id": node_id,
                    "path": path,
                    "qualified_name": qualified_name,
                    "collection_status": "STATIC",
                }
            )
    if values:
        session.execute(
            pg_insert(TestCaseRecord)
            .values(values)
            .on_conflict_do_nothing(index_elements=["analysis_run_id", "node_id"])
        )
    return [
        TestCase(r.node_id, r.path, r.qualified_name, r.symbol_id)
        for r in session.execute(
            select(TestCaseRecord)
            .where(TestCaseRecord.analysis_run_id == run_id)
            .order_by(TestCaseRecord.node_id)
        ).scalars()
    ]


def require_python_tests(session: Session, run_id: uuid.UUID) -> None:
    """Test selection and sandboxed runs use pytest: refuse repositories without Python."""
    run = session.get(AnalysisRun, run_id)
    snapshot = session.get(RepositorySnapshot, run.snapshot_id) if run and run.snapshot_id else None
    inventory: dict[str, int] = (snapshot.language_inventory if snapshot else None) or {}
    if "python" in inventory:
        return
    code = {k: v for k, v in inventory.items() if k in PARSED_LANGUAGES} or {"text": 0}
    language = max(sorted(code), key=lambda k: code[k])
    raise InvalidInput(
        f"Test selection and test runs are not supported for {language} "
        "(only Python test suites run with pytest)",
        code="language_not_supported",
        details={"language": language},
    )


def create_test_plan(
    session: Session,
    change_id: uuid.UUID,
    *,
    policy: Policy,
    max_tests: int | None = None,
    max_seconds: float | None = None,
) -> TestPlan:
    change = session.get(ChangeSet, change_id)
    if change is None:
        raise NotFound("Impact analysis not found")
    if change.status != ChangeSetStatus.SUCCEEDED.value or change.target_run_id is None:
        raise InvalidInput(
            f"Impact analysis is {change.status}; a plan needs a completed analysis",
            code="impact_not_ready",
        )
    report = session.execute(
        select(ImpactReport).where(ImpactReport.change_set_id == change_id)
    ).scalar_one_or_none()
    if report is None:
        raise InvalidInput("Impact report is missing", code="report_not_ready")
    if policy is Policy.BUDGETED and max_tests is None and max_seconds is None:
        raise InvalidInput("A budgeted plan needs max_tests or max_seconds", code="budget_missing")

    run_id = change.target_run_id
    require_python_tests(session, run_id)
    catalog = ensure_test_catalog(session, run_id)
    statuses = dict(
        session.execute(
            select(TestCaseRecord.node_id, TestCaseRecord.collection_status).where(
                TestCaseRecord.analysis_run_id == run_id
            )
        ).all()
    )
    # After sandboxed collection, tests pytest would not collect cannot be selected.
    not_collected = sorted(n for n, s in statuses.items() if s == "NOT_COLLECTED")
    catalog = [t for t in catalog if statuses.get(t.node_id) != "NOT_COLLECTED"]
    sources: dict[str, Any] = {
        "static_graph": True,
        "collection": any(s == "COLLECTED" for s in statuses.values()),
        "coverage": False,
        "history": False,
        "excluded_not_collected": not_collected[:100],
    }
    scope = Scope(report.recommended_scope)
    evidence, durations = _gather_evidence(session, change, report, catalog, sources, scope)

    if scope is Scope.BROAD and not any(
        e.kind is not EvidenceKind.PREVIOUSLY_FAILED for found in evidence.values() for e in found
    ):
        # Changes that could not be traced, and no test linked to them: be conservative.
        scope = Scope.FULL_SUITE
        sources["fallback"] = "broad scope with no test linked to the changes"
    reasons = list(report.scope_reasons)
    if sources.get("fallback"):
        reasons.append(str(sources["fallback"]))
    fallback = "; ".join(reasons) if scope is Scope.FULL_SUITE else None
    budget = Budget(max_tests, max_seconds) if policy is Policy.BUDGETED else None
    result = select_tests(
        catalog,
        evidence,
        scope=scope,
        full_suite_reason=fallback,
        policy=policy,
        budget=budget,
        durations=durations,
    )
    plan = TestPlan(
        id=uuid.uuid4(),
        change_set_id=change_id,
        analysis_run_id=run_id,
        policy=policy.value,
        budget={"max_tests": max_tests, "max_seconds": max_seconds} if budget else None,
        scope=scope.value,
        fallback_reason=result.fallback_reason,
        total_tests=result.total_tests,
        selected_tests=len(result.selected),
        estimated_seconds=result.estimated_seconds,
        budget_limited=result.budget_limited,
        evidence_sources=sources,
    )
    session.add(plan)
    session.flush()
    case_ids = dict(
        session.execute(
            select(TestCaseRecord.node_id, TestCaseRecord.id).where(
                TestCaseRecord.analysis_run_id == run_id
            )
        ).all()
    )
    rows = [
        {
            "plan_id": plan.id,
            "test_case_id": case_ids.get(d.test.node_id),
            "node_id": d.test.node_id,
            "selected": d.selected,
            "rank": rank,
            "score": d.score,
            "evidence": [_evidence_json(e) for e in d.evidence],
            "exclusion_reason": d.exclusion_reason,
            "estimated_seconds": d.estimated_seconds,
        }
        for rank, d in enumerate(result.decisions, start=1)
    ]
    if rows:
        session.execute(insert(TestPlanItem), rows)
    return plan


def _gather_evidence(
    session: Session,
    change: ChangeSet,
    report: ImpactReport,
    catalog: list[TestCase],
    sources: dict[str, Any],
    scope: Scope,
) -> tuple[dict[str, list[Evidence]], dict[str, float]]:
    """All evidence for every candidate test: static paths, coverage, history, modules."""
    assert change.target_run_id is not None  # noqa: S101 - checked by the caller
    by_qualified = {t.qualified_name: t for t in catalog}
    evidence: dict[str, list[Evidence]] = defaultdict(list)
    items = list(
        session.execute(select(ImpactItem).where(ImpactItem.report_id == report.id)).scalars()
    )
    for item in items:
        if not (item.is_test or item.symbol_kind == SymbolKind.MODULE.value):
            continue
        detail = {
            "seed": item.seed_qualified_name,
            "seed_reason": item.seed_reason,
            "graph": item.graph,
            "path": item.explanation,
        }
        kind = _STRENGTH_TO_EVIDENCE[item.strength]
        for test in _tests_for_item(item, by_qualified, catalog):
            evidence[test.node_id].append(Evidence(kind, detail, item.depth))

    coverage_run = _base_coverage_run(session, change)
    if coverage_run is not None:
        sources["coverage"] = True
        sources["coverage_run_id"] = str(coverage_run)
        for node_id, detail in _coverage_evidence(session, change.id, coverage_run).items():
            evidence[node_id].append(Evidence(EvidenceKind.COVERAGE, detail, 0))
    durations, failed = _history(session, change.repository_id)
    sources["history"] = bool(durations or failed)
    for node_id in failed:
        evidence[node_id].append(Evidence(EvidenceKind.PREVIOUSLY_FAILED, {}, 0))
    if scope is Scope.BROAD:
        module_tests = _module_dependency_tests(session, change.target_run_id, items, catalog)
        for node_id, detail in module_tests.items():
            evidence[node_id].append(Evidence(EvidenceKind.MODULE_DEPENDENCY, detail, 0))
    return evidence, durations


def _tests_for_item(
    item: ImpactItem, by_qualified: dict[str, TestCase], catalog: list[TestCase]
) -> list[TestCase]:
    if item.qualified_name in by_qualified:
        return [by_qualified[item.qualified_name]]
    if item.symbol_kind in (SymbolKind.CLASS.value, SymbolKind.MODULE.value):
        # A test class or test module: every collected test inside it.
        prefix = f"{item.qualified_name}."
        return [t for t in catalog if t.qualified_name.startswith(prefix)]
    return []


def _module_dependency_tests(
    session: Session, run_id: uuid.UUID, items: list[ImpactItem], catalog: list[TestCase]
) -> dict[str, dict[str, Any]]:
    """Broad scope: tests in files that import any module containing changed or impacted code."""
    paths = sorted({i.path for i in items if i.path and not i.is_test})
    if not paths:
        return {}
    src_sym, dst_sym = aliased(Symbol), aliased(Symbol)
    src_file, dst_file = aliased(SourceFile), aliased(SourceFile)
    rows = session.execute(
        select(src_file.path, dst_file.path)
        .select_from(DependencyEdge)
        .join(src_sym, src_sym.id == DependencyEdge.src_symbol_id)
        .join(dst_sym, dst_sym.id == DependencyEdge.dst_symbol_id)
        .join(src_file, src_file.id == src_sym.file_id)
        .join(dst_file, dst_file.id == dst_sym.file_id)
        .where(
            DependencyEdge.analysis_run_id == run_id,
            DependencyEdge.edge_type == EdgeType.IMPORTS.value,
            dst_file.path.in_(paths),
            src_file.role == "TEST",
        )
        .distinct()
    ).all()
    imported_by: dict[str, set[str]] = defaultdict(set)
    for test_path, module_path in rows:
        imported_by[test_path].add(module_path)
    return {
        test.node_id: {"imports_modules": sorted(imported_by[test.path])}
        for test in catalog
        if test.path in imported_by
    }


def _base_coverage_run(session: Session, change: ChangeSet) -> uuid.UUID | None:
    """Most recent completed run with coverage on the *base* commit.

    Coverage line numbers are only meaningful for the exact file contents they were recorded
    against, so only runs on the base snapshot are used.
    """
    if change.base_run_id is None:
        return None
    base = session.get(AnalysisRun, change.base_run_id)
    if base is None or base.snapshot_id is None:
        return None
    run_id: uuid.UUID | None = session.execute(
        select(TestRun.id)
        .where(
            TestRun.snapshot_id == base.snapshot_id,
            TestRun.coverage.is_(True),
            TestRun.outcome.in_(["PASSED", "FAILED"]),
        )
        .order_by(TestRun.finished_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return run_id


def _coverage_evidence(
    session: Session, change_id: uuid.UUID, coverage_run: uuid.UUID
) -> dict[str, dict[str, Any]]:
    """Tests that executed base-revision lines this change modifies or deletes."""
    files = session.execute(
        select(ChangedFile.old_path, ChangedFile.old_ranges, ChangedFile.new_ranges).where(
            ChangedFile.change_set_id == change_id, ChangedFile.old_path.is_not(None)
        )
    ).all()
    found: dict[str, dict[str, Any]] = {}
    for path, old_ranges, new_ranges in files:
        lines = {n for first, last in old_ranges or [] for n in range(first, last + 1)}
        if not lines and new_ranges:
            # Pure insertions: the surrounding base lines are the closest observed evidence.
            lines = {max(first - 1, 1) for first, _ in new_ranges} | {
                first for first, _ in new_ranges
            }
        if not lines:
            continue
        rows = session.execute(
            select(CoverageRecord.test_node_id, CoverageRecord.lines).where(
                CoverageRecord.test_run_id == coverage_run,
                CoverageRecord.path == path,
                CoverageRecord.test_node_id.is_not(None),
            )
        ).all()
        for node_id, covered in rows:
            if node_id is None:
                continue
            hit = sorted(lines & set(covered or []))
            if hit:
                entry = found.setdefault(node_id, {"coverage_run": str(coverage_run), "lines": {}})
                entry["lines"][path] = hit[:50]
    return found


def _history(session: Session, repository_id: uuid.UUID) -> tuple[dict[str, float], set[str]]:
    """Latest measured duration (seconds) and latest failing outcome per test node id."""
    latest = (
        select(
            TestResultRecord.node_id,
            TestResultRecord.duration_ms,
            TestResultRecord.outcome,
            func.row_number()
            .over(partition_by=TestResultRecord.node_id, order_by=TestRun.finished_at.desc())
            .label("rank"),
        )
        .join(TestRun, TestRun.id == TestResultRecord.test_run_id)
        .where(TestRun.repository_id == repository_id, TestRun.status == "COMPLETED")
        .subquery()
    )
    rows = session.execute(
        select(latest.c.node_id, latest.c.duration_ms, latest.c.outcome).where(latest.c.rank == 1)
    ).all()
    durations = {node: round(ms / 1000, 3) for node, ms, _ in rows}
    failed = {node for node, _, outcome in rows if outcome in ("FAILED", "ERROR")}
    return durations, failed


def _evidence_json(evidence: Evidence) -> dict[str, Any]:
    return {
        "kind": evidence.kind.value,
        "weight": evidence.weight,
        "depth": evidence.depth,
        **dict(evidence.detail),
    }
