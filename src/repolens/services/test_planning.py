"""Test plans: turn an impact report into an explainable, stored selection of pytest tests."""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, aliased

from repolens.domain.enums import ChangeSetStatus, EdgeType, SymbolKind
from repolens.persistence.models import (
    AnalysisFile,
    ChangeSet,
    DependencyEdge,
    ImpactItem,
    ImpactReport,
    SourceFile,
    Symbol,
    TestCaseRecord,
    TestPlan,
    TestPlanItem,
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
    catalog = ensure_test_catalog(session, run_id)
    by_qualified = {t.qualified_name: t for t in catalog}
    evidence: dict[str, list[Evidence]] = defaultdict(list)
    sources = {"static_graph": True, "coverage": False, "history": False}

    items = list(
        session.execute(select(ImpactItem).where(ImpactItem.report_id == report.id)).scalars()
    )
    test_items = [i for i in items if i.is_test or i.symbol_kind == SymbolKind.MODULE.value]
    for item in test_items:
        kind = _STRENGTH_TO_EVIDENCE[item.strength]
        detail = {
            "seed": item.seed_qualified_name,
            "seed_reason": item.seed_reason,
            "graph": item.graph,
            "path": item.explanation,
        }
        for test in _tests_for_item(item, by_qualified, catalog):
            evidence[test.node_id].append(Evidence(kind, detail, item.depth))

    scope = Scope(report.recommended_scope)
    if scope is Scope.BROAD:
        for node_id, detail in _module_dependency_tests(session, run_id, items, catalog).items():
            evidence[node_id].append(Evidence(EvidenceKind.MODULE_DEPENDENCY, detail, 0))

    fallback = "; ".join(report.scope_reasons) if scope is Scope.FULL_SUITE else None
    budget = Budget(max_tests, max_seconds) if policy is Policy.BUDGETED else None
    result = select_tests(
        catalog, evidence, scope=scope, full_suite_reason=fallback, policy=policy, budget=budget
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


def _evidence_json(evidence: Evidence) -> dict[str, Any]:
    return {
        "kind": evidence.kind.value,
        "weight": evidence.weight,
        "depth": evidence.depth,
        **dict(evidence.detail),
    }
