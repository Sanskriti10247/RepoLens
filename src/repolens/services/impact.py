"""Impact report: propagate a change set through the dependency graphs and explain it.

Observed facts (changed files, changed symbols, resolved edges) are kept separate from
heuristics (containing classes, module-level changes, name matches against unresolved calls),
and every heuristic contribution is labelled as such. The risk assessment is an explainable
point score over named factors; it is not a probability of failure.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from repolens.codegraph.traversal import GraphEdge
from repolens.domain.enums import (
    ChangeMappingStatus,
    EdgeType,
    ResolutionStatus,
    SymbolKind,
)
from repolens.impact.propagation import Impact, PropagationResult, Seed, propagate
from repolens.persistence.db import transaction
from repolens.persistence.graph_queries import SqlEdgeSource
from repolens.persistence.models import (
    AnalysisRun,
    ChangedFile,
    ChangedSymbol,
    ChangeSet,
    DependencyEdge,
    ImpactItem,
    ImpactReport,
    SourceFile,
    Symbol,
    SymbolReference,
)
from repolens.services.jobs import JobContext

IMPACT_ENGINE_VERSION = "1"
MAX_DEPTH = 8
MAX_NODES = 5000
MAX_NAME_MATCHES_PER_NAME = 50
FOUNDATIONAL_FAN_IN = 10
# Files whose change can affect how the whole test suite runs.
SUITE_WIDE_FILES = frozenset(
    {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "tox.ini",
        "pytest.ini",
        "noxfile.py",
        "conftest.py",
        ".coveragerc",
        "Pipfile",
        "Pipfile.lock",
        "poetry.lock",
        "uv.lock",
        "requirements.txt",
    }
)
# Files that cannot change how the repository's tests behave when run locally: CI and editor
# configuration, ownership and licence files. Treated like documentation.
NON_RUNTIME_PREFIXES = (".github/", ".gitlab/", ".circleci/", ".devcontainer/", ".vscode/")
NON_RUNTIME_NAMES = frozenset(
    {
        ".pre-commit-config.yaml",
        ".gitlab-ci.yml",
        ".travis.yml",
        ".gitignore",
        ".gitattributes",
        ".editorconfig",
        ".mailmap",
        "CODEOWNERS",
        ".readthedocs.yaml",
        ".readthedocs.yml",
        "LICENSE",
        "LICENSE.txt",
        "LICENSE.md",
        "COPYING",
    }
)


def has_runtime_effect(file: ChangedFile) -> bool:
    """False for documentation and for files that cannot affect test execution."""
    path = file.new_path or file.old_path or ""
    if file.role == "DOC" and file.mapping_status == ChangeMappingStatus.NON_PYTHON.value:
        return False
    return not (
        path.startswith(NON_RUNTIME_PREFIXES) or PurePosixPath(path).name in NON_RUNTIME_NAMES
    )


# Method names too common to match against unresolved call sites meaningfully.
COMMON_NAMES = frozenset(
    {
        "get", "set", "run", "add", "pop", "put", "update", "close", "open", "read", "write",
        "items", "keys", "values", "append", "extend", "format", "join", "split", "copy",
        "clear", "remove", "insert", "send", "call", "load", "save", "start", "stop", "init",
        "main", "setup", "test", "name", "data", "value", "process", "handle", "execute",
    }
)  # fmt: skip
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class _Sym:
    id: int
    key: str
    qualified_name: str
    name: str
    kind: str
    visibility: str
    path: str
    is_test: bool
    parent_id: int | None


def impact_stage(ctx: JobContext, change_id: uuid.UUID) -> dict[str, Any]:
    """Change-analysis stage: build and store the impact report for ``change_id``."""
    with transaction(ctx.session_factory) as session:
        report = build_impact_report(session, change_id, checkpoint=ctx.checkpoint)
    return {"impact": report}


def build_impact_report(
    session: Session, change_id: uuid.UUID, *, checkpoint: Any = lambda: None
) -> dict[str, Any]:
    change = session.get(ChangeSet, change_id)
    if change is None or change.base_run_id is None or change.target_run_id is None:
        raise ValueError("change set is not ready for impact analysis")
    session.query(ImpactReport).filter(ImpactReport.change_set_id == change_id).delete()
    files = list(
        session.execute(select(ChangedFile).where(ChangedFile.change_set_id == change_id)).scalars()
    )
    changed = list(
        session.execute(
            select(ChangedSymbol).where(ChangedSymbol.change_set_id == change_id)
        ).scalars()
    )
    base_run, target_run = change.base_run_id, change.target_run_id
    too_large = bool((change.summary or {}).get("too_large"))

    # -- seeds ---------------------------------------------------------------------------
    target_seeds, base_seeds, changed_target_ids = _seeds(session, changed)
    target_info = _symbols(session, target_run, list(changed_target_ids))
    target_seeds += _container_seeds(session, target_run, changed, target_info, changed_target_ids)
    target_seeds += _module_level_seeds(session, target_run, changed, changed_target_ids)
    name_matches = _unresolved_name_matches(session, target_run, changed, target_info)
    target_seeds += [
        Seed(m["source_symbol_id"], "UNRESOLVED_NAME_MATCH", heuristic=True)
        for m in name_matches["matches"]
        if m["source_symbol_id"] not in changed_target_ids
    ]
    checkpoint()

    # -- propagation ---------------------------------------------------------------------
    target_result = propagate(
        SqlEdgeSource(session, target_run), target_seeds, max_depth=MAX_DEPTH, max_nodes=MAX_NODES
    )
    checkpoint()
    base_result = propagate(
        SqlEdgeSource(session, base_run), base_seeds, max_depth=MAX_DEPTH, max_nodes=MAX_NODES
    )
    checkpoint()

    target_syms = _symbols(session, target_run, _ids_in(target_result))
    base_syms = _symbols(session, base_run, _ids_in(base_result))
    # Base-graph impacts are mapped to the target revision by symbol key where possible.
    base_to_target = _match_by_key(session, target_run, [s.key for s in base_syms.values()])

    items = _items(
        target_result,
        base_result,
        target_syms,
        base_syms,
        base_to_target,
    )

    # -- assessment ----------------------------------------------------------------------
    uncertainty = _uncertainty(session, target_run, items, name_matches)
    completeness, completeness_reasons = _completeness(
        files, too_large, target_result, base_result, session, base_run, target_run
    )
    counts = _counts(items, files, changed)
    fan_in = _fan_in(session, target_run, list(changed_target_ids))
    risk_level, factors = _risk(changed, target_info, counts, fan_in, files, uncertainty)
    scope, scope_reasons = _scope(
        files, too_large, completeness, counts, uncertainty, target_result
    )

    report = ImpactReport(
        id=uuid.uuid4(),
        change_set_id=change_id,
        engine_version=IMPACT_ENGINE_VERSION,
        completeness=completeness,
        completeness_reasons=completeness_reasons,
        risk_level=risk_level,
        risk_factors=factors,
        recommended_scope=scope,
        scope_reasons=scope_reasons,
        counts=counts,
        limits={
            "max_depth": MAX_DEPTH,
            "max_nodes": MAX_NODES,
            "target_truncated": target_result.truncated,
            "target_truncation_reason": target_result.truncation_reason,
            "base_truncated": base_result.truncated,
            "base_truncation_reason": base_result.truncation_reason,
        },
        uncertainty=uncertainty,
    )
    session.add(report)
    session.flush()
    if items:
        session.execute(insert(ImpactItem), [{**item, "report_id": report.id} for item in items])
    return {
        "report_id": str(report.id),
        "risk_level": risk_level,
        "recommended_scope": scope,
        "completeness": completeness,
        "impacted_symbols": counts["impacted_symbols"],
        "impacted_tests": counts["impacted_tests"],
    }


# -- seeds ----------------------------------------------------------------------------------


def _seed_reason(change: ChangedSymbol) -> str:
    if change.change_kind in ("ADDED", "REMOVED", "RENAMED"):
        return change.change_kind
    if change.signature_changed:
        return "SIGNATURE_CHANGED"
    if change.bases_changed:
        return "BASES_CHANGED"
    if change.decorators_changed:
        return "DECORATORS_CHANGED"
    return "BODY_CHANGED"


def _seeds(
    session: Session, changed: list[ChangedSymbol]
) -> tuple[list[Seed[int]], list[Seed[int]], set[int]]:
    target: list[Seed[int]] = []
    base: list[Seed[int]] = []
    target_ids: set[int] = set()
    for change in changed:
        if change.symbol_kind == SymbolKind.MODULE.value:
            continue  # module-level changes are handled separately (see _module_level_seeds)
        reason = _seed_reason(change)
        if change.target_symbol_id is not None:
            target.append(Seed(change.target_symbol_id, reason))
            target_ids.add(change.target_symbol_id)
        if change.base_symbol_id is not None and (
            change.change_kind in ("REMOVED", "RENAMED") or change.signature_changed
        ):
            # Callers as of the base revision may now be broken.
            base.append(Seed(change.base_symbol_id, reason))
    return target, base, target_ids


def _container_seeds(
    session: Session,
    run_id: uuid.UUID,
    changed: list[ChangedSymbol],
    info: dict[int, _Sym],
    changed_ids: set[int],
) -> list[Seed[int]]:
    """Classes whose methods changed: users of the class may call the method indirectly."""
    parents = {
        info[c.target_symbol_id].parent_id
        for c in changed
        if c.target_symbol_id in info
        and c.symbol_kind in (SymbolKind.METHOD.value, SymbolKind.ASYNC_METHOD.value)
    }
    parents.discard(None)
    classes = _symbols(session, run_id, [p for p in parents if p is not None])
    return [
        Seed(cls.id, "CONTAINER_OF_CHANGE", heuristic=True)
        for cls in classes.values()
        if cls.kind == SymbolKind.CLASS.value and cls.id not in changed_ids
    ]


def _module_level_seeds(
    session: Session, run_id: uuid.UUID, changed: list[ChangedSymbol], changed_ids: set[int]
) -> list[Seed[int]]:
    """Statements outside any definition changed: importers, and same-module code."""
    seeds: list[Seed[int]] = []
    for change in changed:
        if (
            change.symbol_kind != SymbolKind.MODULE.value
            or change.target_symbol_id is None
            or (change.change_kind == "MODIFIED" and change.changed_lines == 0)
        ):
            continue
        seeds.append(Seed(change.target_symbol_id, "MODULE_LEVEL_CHANGE"))
        members = session.execute(
            select(Symbol.id).where(
                Symbol.analysis_run_id == run_id, Symbol.parent_id == change.target_symbol_id
            )
        ).scalars()
        seeds += [
            Seed(member, "SAME_MODULE_AS_CHANGE", heuristic=True)
            for member in members
            if member not in changed_ids
        ]
    return seeds


def _unresolved_name_matches(
    session: Session, run_id: uuid.UUID, changed: list[ChangedSymbol], info: dict[int, _Sym]
) -> dict[str, Any]:
    """Unresolved/dynamic call sites ending in a changed method's name: possible callers."""
    names = sorted(
        {
            info[c.target_symbol_id].name
            for c in changed
            if c.target_symbol_id in info
            and c.symbol_kind in (SymbolKind.METHOD.value, SymbolKind.ASYNC_METHOD.value)
        }
    )
    matches: list[dict[str, Any]] = []
    skipped: list[str] = []
    for name in names:
        if name in COMMON_NAMES or len(name) < 4 or not _IDENTIFIER.match(name):
            skipped.append(name)
            continue
        rows = session.execute(
            select(
                SymbolReference.source_symbol_id, SymbolReference.raw_text, Symbol.qualified_name
            )
            .join(Symbol, Symbol.id == SymbolReference.source_symbol_id)
            .where(
                SymbolReference.analysis_run_id == run_id,
                SymbolReference.resolution_status.in_(
                    [ResolutionStatus.UNRESOLVED.value, ResolutionStatus.DYNAMIC.value]
                ),
                SymbolReference.raw_text.op("~")(rf"\.{name}\s*\("),
            )
            .order_by(Symbol.qualified_name, SymbolReference.id)
            .limit(MAX_NAME_MATCHES_PER_NAME + 1)
        ).all()
        if len(rows) > MAX_NAME_MATCHES_PER_NAME:
            skipped.append(name)
            continue
        matches += [
            {"name": name, "source_symbol_id": r[0], "raw_text": r[1], "source": r[2]} for r in rows
        ]
    return {"matches": matches, "skipped_names": skipped}


# -- items ----------------------------------------------------------------------------------


def _ids_in(result: PropagationResult[int]) -> list[int]:
    ids: set[int] = set(result.impacts)
    for impact in result.impacts.values():
        for edge in impact.path:
            ids.update((edge.src, edge.dst))
    return sorted(ids)


def _symbols(session: Session, run_id: uuid.UUID, ids: list[int]) -> dict[int, _Sym]:
    if not ids:
        return {}
    rows = session.execute(
        select(
            Symbol.id,
            Symbol.symbol_key,
            Symbol.qualified_name,
            Symbol.name,
            Symbol.kind,
            Symbol.visibility,
            SourceFile.path,
            Symbol.is_test,
            Symbol.parent_id,
        )
        .join(SourceFile, SourceFile.id == Symbol.file_id)
        .where(Symbol.analysis_run_id == run_id, Symbol.id.in_(ids))
    ).all()
    return {r[0]: _Sym(*r) for r in rows}


def _match_by_key(session: Session, run_id: uuid.UUID, keys: list[str]) -> dict[str, int]:
    if not keys:
        return {}
    rows = session.execute(
        select(Symbol.symbol_key, Symbol.id).where(
            Symbol.analysis_run_id == run_id, Symbol.symbol_key.in_(keys)
        )
    ).all()
    return {str(key): int(symbol_id) for key, symbol_id in rows}


def _strength(impact: Impact[int]) -> str:
    # Seeds are either changed symbols (exact) or heuristic pseudo-seeds (containers, ...).
    if impact.depth == 0 and not impact.heuristic:
        return "CHANGED"
    if impact.heuristic:
        return "HEURISTIC"
    if impact.via_import_only:
        return "IMPORT_ONLY"
    return "DIRECT" if impact.depth == 1 else "TRANSITIVE"


_STRENGTH_ORDER = {"CHANGED": 0, "DIRECT": 1, "TRANSITIVE": 2, "IMPORT_ONLY": 3, "HEURISTIC": 4}


def _explain(path: tuple[GraphEdge[int], ...], names: dict[int, _Sym]) -> list[dict[str, Any]]:
    return [
        {
            "from": names[e.src].qualified_name if e.src in names else str(e.src),
            "to": names[e.dst].qualified_name if e.dst in names else str(e.dst),
            "edge_type": e.edge_type.value,
            "confidence": e.confidence.value,
        }
        for e in path
    ]


def _items(
    target: PropagationResult[int],
    base: PropagationResult[int],
    target_syms: dict[int, _Sym],
    base_syms: dict[int, _Sym],
    base_to_target: dict[str, int],
) -> list[dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}

    def add(item: dict[str, Any]) -> None:
        key = item.pop("_key")
        existing = items.get(key)
        rank = (_STRENGTH_ORDER[item["strength"]], item["depth"])
        if existing is None or rank < (_STRENGTH_ORDER[existing["strength"]], existing["depth"]):
            if (
                existing is not None
                and existing.get("base_symbol_id")
                and not item.get("base_symbol_id")
            ):
                item["base_symbol_id"] = existing["base_symbol_id"]
            items[key] = item

    for node, impact in target.impacts.items():
        sym = target_syms.get(node)
        seed = target_syms.get(impact.seed)
        if sym is None or seed is None:
            continue
        add(
            {
                "_key": sym.key,
                "target_symbol_id": node,
                "base_symbol_id": None,
                "qualified_name": sym.qualified_name,
                "symbol_kind": sym.kind,
                "path": sym.path,
                "is_test": sym.is_test,
                "depth": impact.depth,
                "strength": _strength(impact),
                "seed_qualified_name": seed.qualified_name,
                "seed_reason": impact.seed_reason,
                "graph": "TARGET",
                "explanation": _explain(impact.path, target_syms),
            }
        )
    for node, impact in base.impacts.items():
        sym = base_syms.get(node)
        seed = base_syms.get(impact.seed)
        if sym is None or seed is None:
            continue
        add(
            {
                "_key": sym.key,
                "target_symbol_id": base_to_target.get(sym.key),
                "base_symbol_id": node,
                "qualified_name": sym.qualified_name,
                "symbol_kind": sym.kind,
                "path": sym.path,
                "is_test": sym.is_test,
                "depth": impact.depth,
                "strength": _strength(impact),
                "seed_qualified_name": seed.qualified_name,
                "seed_reason": impact.seed_reason,
                "graph": "BASE",
                "explanation": _explain(impact.path, base_syms),
            }
        )
    return sorted(items.values(), key=lambda i: (i["depth"], i["qualified_name"]))


# -- assessment -----------------------------------------------------------------------------


def _counts(
    items: list[dict[str, Any]], files: list[ChangedFile], changed: list[ChangedSymbol]
) -> dict[str, Any]:
    impacted = [i for i in items if i["strength"] != "CHANGED"]
    return {
        "changed_files": len(files),
        "changed_symbols": sum(1 for c in changed if c.symbol_kind != SymbolKind.MODULE.value),
        "impacted_symbols": sum(1 for i in impacted if not i["is_test"]),
        "impacted_tests": sum(1 for i in items if i["is_test"]),
        "by_strength": dict(sorted(Counter(i["strength"] for i in items).items())),
        "impacted_files": len({i["path"] for i in items if i["path"]}),
        "non_runtime_files": sum(1 for f in files if not has_runtime_effect(f)),
    }


def _completeness(
    files: list[ChangedFile],
    too_large: bool,
    target: PropagationResult[int],
    base: PropagationResult[int],
    session: Session,
    base_run: uuid.UUID,
    target_run: uuid.UUID,
) -> tuple[str, list[dict[str, Any]]]:
    if too_large:
        return "UNKNOWN", [{"reason": "DIFF_TOO_LARGE", "detail": "symbol mapping skipped"}]
    reasons: list[dict[str, Any]] = []
    for file in files:
        path = file.new_path or file.old_path
        status = ChangeMappingStatus(file.mapping_status)
        if status is ChangeMappingStatus.MAPPED:
            continue
        if not has_runtime_effect(file):
            continue  # documentation, CI and editor configuration
        reasons.append({"reason": f"FILE_{status.value}", "path": path})
    for label, result in (("TARGET", target), ("BASE", base)):
        if result.truncated:
            reasons.append(
                {
                    "reason": "PROPAGATION_TRUNCATED",
                    "graph": label,
                    "detail": result.truncation_reason,
                }
            )
    for label, run_id in (("BASE", base_run), ("TARGET", target_run)):
        run = session.get(AnalysisRun, run_id)
        if run is not None and run.status == "PARTIAL":
            reasons.append({"reason": "ANALYSIS_PARTIAL", "graph": label})
    return ("PARTIAL" if reasons else "COMPLETE"), reasons


def _uncertainty(
    session: Session, run_id: uuid.UUID, items: list[dict[str, Any]], name_matches: dict[str, Any]
) -> dict[str, Any]:
    ids = [i["target_symbol_id"] for i in items if i["target_symbol_id"] is not None]
    statuses: dict[str, int] = {}
    samples: list[dict[str, Any]] = []
    if ids:
        statuses = {
            k: int(v)
            for k, v in session.execute(
                select(SymbolReference.resolution_status, func.count())
                .where(
                    SymbolReference.analysis_run_id == run_id,
                    SymbolReference.source_symbol_id.in_(ids),
                    SymbolReference.resolution_status.in_(
                        [ResolutionStatus.UNRESOLVED.value, ResolutionStatus.DYNAMIC.value]
                    ),
                )
                .group_by(SymbolReference.resolution_status)
            ).all()
        }
        samples = [
            {"source": r[0], "raw_text": r[1], "status": r[2], "rule": r[3]}
            for r in session.execute(
                select(
                    Symbol.qualified_name,
                    SymbolReference.raw_text,
                    SymbolReference.resolution_status,
                    SymbolReference.resolver_rule,
                )
                .join(Symbol, Symbol.id == SymbolReference.source_symbol_id)
                .where(
                    SymbolReference.analysis_run_id == run_id,
                    SymbolReference.source_symbol_id.in_(ids),
                    SymbolReference.resolution_status == ResolutionStatus.DYNAMIC.value,
                )
                .order_by(Symbol.qualified_name, SymbolReference.id)
                .limit(20)
            ).all()
        ]
    return {
        "unresolved_references_in_impacted_code": statuses.get("UNRESOLVED", 0),
        "dynamic_references_in_impacted_code": statuses.get("DYNAMIC", 0),
        "dynamic_samples": samples,
        "possible_hidden_callers": [
            {k: m[k] for k in ("name", "source", "raw_text")} for m in name_matches["matches"]
        ],
        "names_too_common_to_match": name_matches["skipped_names"],
    }


def _fan_in(session: Session, run_id: uuid.UUID, ids: list[int]) -> dict[int, int]:
    if not ids:
        return {}
    rows = session.execute(
        select(DependencyEdge.dst_symbol_id, func.count())
        .where(
            DependencyEdge.analysis_run_id == run_id,
            DependencyEdge.dst_symbol_id.in_(ids),
            DependencyEdge.edge_type.in_(
                [EdgeType.CALLS.value, EdgeType.REFERENCES.value, EdgeType.INHERITS.value]
            ),
        )
        .group_by(DependencyEdge.dst_symbol_id)
    ).all()
    return {int(a): int(b) for a, b in rows}


def _risk(
    changed: list[ChangedSymbol],
    info: dict[int, _Sym],
    counts: dict[str, Any],
    fan_in: dict[int, int],
    files: list[ChangedFile],
    uncertainty: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    factors: list[dict[str, Any]] = []

    def factor(name: str, points: int, value: Any, detail: str) -> None:
        if points:
            factors.append({"factor": name, "points": points, "value": value, "detail": detail})

    impacted = counts["impacted_symbols"]
    factor(
        "impacted_symbols",
        3 if impacted > 50 else 2 if impacted > 10 else 1 if impacted > 0 else 0,
        impacted,
        "non-test symbols that depend on the change",
    )
    public_breaks = sorted(
        c.qualified_name
        for c in changed
        if c.symbol_kind != SymbolKind.MODULE.value
        and (c.change_kind in ("REMOVED", "RENAMED") or c.signature_changed)
        and not c.qualified_name.rsplit(".", 1)[-1].startswith("_")
    )
    factor(
        "public_interface_change",
        2 if public_breaks else 0,
        public_breaks[:20],
        "public symbols removed, renamed or with changed signatures",
    )
    foundational = sorted(
        info[i].qualified_name for i, n in fan_in.items() if n >= FOUNDATIONAL_FAN_IN and i in info
    )
    factor(
        "foundational_symbols",
        2 if foundational else 0,
        foundational[:20],
        f"changed symbols with at least {FOUNDATIONAL_FAN_IN} direct dependents",
    )
    inheritance = sorted(c.qualified_name for c in changed if c.bases_changed)
    factor("inheritance_change", 2 if inheritance else 0, inheritance, "base classes changed")
    unmapped = sorted(
        (f.new_path or f.old_path or "")
        for f in files
        if f.mapping_status != ChangeMappingStatus.MAPPED.value and has_runtime_effect(f)
    )
    factor(
        "unmapped_changes",
        2 if unmapped else 0,
        unmapped[:20],
        "changed files whose effect could not be traced to symbols",
    )
    hidden = len(uncertainty["possible_hidden_callers"])
    dynamic = uncertainty["dynamic_references_in_impacted_code"]
    factor(
        "dynamic_or_unresolved_relationships",
        1 if hidden or dynamic else 0,
        {"possible_hidden_callers": hidden, "dynamic_references": dynamic},
        "relationships that static analysis could not resolve",
    )
    if counts["changed_symbols"] and counts["impacted_tests"] == 0:
        factor("no_tests_reach_change", 2, 0, "no test was found that depends on the change")
    points = sum(f["points"] for f in factors)
    level = "HIGH" if points >= 6 else "MEDIUM" if points >= 3 else "LOW"
    return level, factors


def _scope(
    files: list[ChangedFile],
    too_large: bool,
    completeness: str,
    counts: dict[str, Any],
    uncertainty: dict[str, Any],
    target: PropagationResult[int],
) -> tuple[str, list[str]]:
    if not files:
        return "NONE", ["no files changed"]
    reasons: list[str] = []
    suite_wide = sorted(
        p
        for f in files
        for p in [f.new_path or f.old_path or ""]
        if PurePosixPath(p).name in SUITE_WIDE_FILES
        or PurePosixPath(p).name.startswith("requirements")
    )
    if too_large:
        reasons.append("the diff was too large to map to symbols")
    if suite_wide:
        reasons.append(f"test or packaging configuration changed: {', '.join(suite_wide[:5])}")
    if target.truncated:
        reasons.append("impact propagation hit its limits")
    if completeness == "UNKNOWN":
        reasons.append("impact completeness is unknown")
    if reasons:
        return "FULL_SUITE", reasons
    if completeness == "PARTIAL":
        reasons.append("some changes could not be traced to symbols")
    if uncertainty["possible_hidden_callers"]:
        reasons.append("unresolved call sites may reach changed methods")
    if counts["changed_symbols"] and counts["impacted_tests"] == 0:
        reasons.append("no test depends on the changed code")
    if reasons:
        return "BROAD", reasons
    if counts["changed_symbols"] == 0:
        return "NONE", ["only documentation, CI configuration or formatting changed"]
    return "TARGETED", ["all changes were traced to symbols and tests"]
