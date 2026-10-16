"""Read queries over a stored analysis graph. All are scoped to one analysis run and bounded."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, and_, case, func, literal, or_, select
from sqlalchemy.orm import Session, aliased

from repolens.codegraph.traversal import GraphEdge
from repolens.domain.enums import Confidence, EdgeType, SymbolKind
from repolens.persistence.models import (
    AnalysisFile,
    AnalysisWarning,
    DependencyEdge,
    SourceFile,
    Symbol,
    SymbolReference,
)


class SqlEdgeSource:
    """``EdgeSource`` backed by indexed per-frontier queries (no whole-graph loading)."""

    def __init__(self, session: Session, run_id: uuid.UUID) -> None:
        self.session = session
        self.run_id = run_id
        self.queries = 0

    def out_edges(self, nodes: Sequence[int], types: frozenset[EdgeType]) -> list[GraphEdge[int]]:
        return self._edges(DependencyEdge.src_symbol_id, nodes, types)

    def in_edges(self, nodes: Sequence[int], types: frozenset[EdgeType]) -> list[GraphEdge[int]]:
        return self._edges(DependencyEdge.dst_symbol_id, nodes, types)

    def _edges(
        self, column: Any, nodes: Sequence[int], types: frozenset[EdgeType]
    ) -> list[GraphEdge[int]]:
        if not nodes or not types:
            return []
        self.queries += 1
        rows = self.session.execute(
            select(
                DependencyEdge.src_symbol_id,
                DependencyEdge.dst_symbol_id,
                DependencyEdge.edge_type,
                DependencyEdge.confidence,
            ).where(
                DependencyEdge.analysis_run_id == self.run_id,
                column.in_(list(nodes)),
                DependencyEdge.edge_type.in_([t.value for t in types]),
            )
        ).all()
        return [
            GraphEdge(
                r.src_symbol_id, r.dst_symbol_id, EdgeType(r.edge_type), Confidence(r.confidence)
            )
            for r in rows
        ]


@dataclass(frozen=True, slots=True)
class SymbolRow:
    id: int
    symbol_key: str
    qualified_name: str
    name: str
    kind: str
    visibility: str
    path: str
    file_id: int
    start_line: int
    start_col: int
    end_line: int
    end_col: int
    signature: str | None
    doc_summary: str | None
    is_test: bool
    has_parse_errors: bool
    parent_id: int | None
    body_fingerprint: str | None
    decorators: list[str]


def _symbol_columns() -> tuple[Any, ...]:
    return (
        Symbol.id,
        Symbol.symbol_key,
        Symbol.qualified_name,
        Symbol.name,
        Symbol.kind,
        Symbol.visibility,
        SourceFile.path,
        Symbol.file_id,
        Symbol.start_line,
        Symbol.start_col,
        Symbol.end_line,
        Symbol.end_col,
        Symbol.signature,
        Symbol.doc_summary,
        Symbol.is_test,
        Symbol.has_parse_errors,
        Symbol.parent_id,
        Symbol.body_fingerprint,
        Symbol.decorators,
    )


def _symbol_select(run_id: uuid.UUID) -> Select[Any]:
    return (
        select(*_symbol_columns())
        .join(SourceFile, SourceFile.id == Symbol.file_id)
        .where(Symbol.analysis_run_id == run_id)
    )


def symbols_by_ids(session: Session, run_id: uuid.UUID, ids: Sequence[int]) -> dict[int, SymbolRow]:
    if not ids:
        return {}
    rows = session.execute(_symbol_select(run_id).where(Symbol.id.in_(list(ids)))).all()
    return {r.id: SymbolRow(*r) for r in rows}


def get_symbol(session: Session, run_id: uuid.UUID, symbol_id: int) -> SymbolRow | None:
    return symbols_by_ids(session, run_id, [symbol_id]).get(symbol_id)


def find_symbols(
    session: Session,
    run_id: uuid.UUID,
    *,
    query: str | None,
    kinds: Sequence[SymbolKind] = (),
    path: str | None = None,
    is_test: bool | None = None,
    limit: int,
    offset: int,
) -> tuple[list[SymbolRow], int]:
    stmt = _symbol_select(run_id)
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(
            or_(
                Symbol.name.ilike(f"{escaped}%", escape="\\"),
                Symbol.qualified_name.ilike(f"%{escaped}%", escape="\\"),
            )
        )
    if kinds:
        stmt = stmt.where(Symbol.kind.in_([k.value for k in kinds]))
    if path:
        stmt = stmt.where(SourceFile.path == path)
    if is_test is not None:
        stmt = stmt.where(Symbol.is_test.is_(is_test))
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rank = (
        case((func.lower(Symbol.name) == (query or "").lower(), 0), else_=1)
        if query
        else literal(0)
    )
    rows = session.execute(
        stmt.order_by(rank, func.length(Symbol.qualified_name), Symbol.qualified_name, Symbol.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return [SymbolRow(*r) for r in rows], total


@dataclass(frozen=True, slots=True)
class NeighbourRow:
    edge_type: str
    confidence: str
    provenance: str
    reference_count: int
    rules: list[str]
    symbol: SymbolRow


def neighbours(
    session: Session,
    run_id: uuid.UUID,
    symbol_id: int,
    *,
    outgoing: bool,
    types: Sequence[EdgeType],
    limit: int = 200,
) -> tuple[list[NeighbourRow], int]:
    """Direct neighbours with edge metadata. Returns (rows, total before limit)."""
    other = aliased(Symbol)
    this_col = DependencyEdge.src_symbol_id if outgoing else DependencyEdge.dst_symbol_id
    other_col = DependencyEdge.dst_symbol_id if outgoing else DependencyEdge.src_symbol_id
    conditions = and_(
        DependencyEdge.analysis_run_id == run_id,
        this_col == symbol_id,
        DependencyEdge.edge_type.in_([t.value for t in types]),
    )
    total = session.execute(
        select(func.count()).select_from(DependencyEdge).where(conditions)
    ).scalar_one()
    rows = session.execute(
        select(
            DependencyEdge.edge_type,
            DependencyEdge.confidence,
            DependencyEdge.provenance,
            DependencyEdge.reference_count,
            DependencyEdge.evidence,
            other_col,
        )
        .join(other, other.id == other_col)
        .where(conditions)
        .order_by(DependencyEdge.edge_type, other.qualified_name, other.id)
        .limit(limit)
    ).all()
    symbols = symbols_by_ids(session, run_id, [r[5] for r in rows])
    return [
        NeighbourRow(
            r.edge_type,
            r.confidence,
            r.provenance,
            r.reference_count,
            list((r.evidence or {}).get("rules", [])),
            symbols[r[5]],
        )
        for r in rows
        if r[5] in symbols
    ], total


@dataclass(frozen=True, slots=True)
class ReferenceRow:
    id: int
    ref_kind: str
    raw_text: str
    start_line: int
    end_line: int
    resolution_status: str
    resolver_rule: str | None
    external_target: str | None
    candidates: list[str] | None
    target_symbol_id: int | None
    target_qualified_name: str | None


def outgoing_references(
    session: Session, run_id: uuid.UUID, symbol_id: int, *, limit: int = 300
) -> tuple[list[ReferenceRow], dict[str, int]]:
    target = aliased(Symbol)
    rows = session.execute(
        select(
            SymbolReference.id,
            SymbolReference.ref_kind,
            SymbolReference.raw_text,
            SymbolReference.start_line,
            SymbolReference.end_line,
            SymbolReference.resolution_status,
            SymbolReference.resolver_rule,
            SymbolReference.external_target,
            SymbolReference.candidates,
            SymbolReference.target_symbol_id,
            target.qualified_name,
        )
        .outerjoin(target, target.id == SymbolReference.target_symbol_id)
        .where(
            SymbolReference.analysis_run_id == run_id,
            SymbolReference.source_symbol_id == symbol_id,
        )
        .order_by(SymbolReference.start_line, SymbolReference.start_col, SymbolReference.id)
        .limit(limit)
    ).all()
    counts = dict(
        session.execute(
            select(SymbolReference.resolution_status, func.count())
            .where(
                SymbolReference.analysis_run_id == run_id,
                SymbolReference.source_symbol_id == symbol_id,
            )
            .group_by(SymbolReference.resolution_status)
        ).all()
    )
    return [ReferenceRow(*r) for r in rows], {k: int(v) for k, v in sorted(counts.items())}


def children(session: Session, run_id: uuid.UUID, symbol_id: int) -> list[SymbolRow]:
    rows = session.execute(
        _symbol_select(run_id)
        .where(Symbol.parent_id == symbol_id)
        .order_by(Symbol.start_line, Symbol.start_col)
    ).all()
    return [SymbolRow(*r) for r in rows]


@dataclass(frozen=True, slots=True)
class ModuleEdgeRow:
    src_module_id: int
    dst_module_id: int
    edge_type: str
    edges: int
    references: int


def module_graph(
    session: Session, run_id: uuid.UUID
) -> tuple[list[SymbolRow], list[ModuleEdgeRow]]:
    """Symbol-level edges aggregated to the modules (files) that contain them."""
    modules = [
        SymbolRow(*r)
        for r in session.execute(
            _symbol_select(run_id)
            .where(Symbol.kind == SymbolKind.MODULE.value)
            .order_by(Symbol.qualified_name)
        ).all()
    ]
    src_sym, dst_sym = aliased(Symbol), aliased(Symbol)
    src_mod, dst_mod = aliased(Symbol), aliased(Symbol)
    rows = session.execute(
        select(
            src_mod.id,
            dst_mod.id,
            DependencyEdge.edge_type,
            func.count(),
            func.sum(DependencyEdge.reference_count),
        )
        .join(src_sym, src_sym.id == DependencyEdge.src_symbol_id)
        .join(dst_sym, dst_sym.id == DependencyEdge.dst_symbol_id)
        .join(
            src_mod,
            and_(
                src_mod.file_id == src_sym.file_id,
                src_mod.analysis_run_id == run_id,
                src_mod.kind == SymbolKind.MODULE.value,
            ),
        )
        .join(
            dst_mod,
            and_(
                dst_mod.file_id == dst_sym.file_id,
                dst_mod.analysis_run_id == run_id,
                dst_mod.kind == SymbolKind.MODULE.value,
            ),
        )
        .where(
            DependencyEdge.analysis_run_id == run_id,
            DependencyEdge.edge_type != EdgeType.CONTAINS.value,
            src_mod.id != dst_mod.id,
        )
        .group_by(src_mod.id, dst_mod.id, DependencyEdge.edge_type)
        .order_by(src_mod.id, dst_mod.id, DependencyEdge.edge_type)
    ).all()
    return modules, [ModuleEdgeRow(r[0], r[1], r[2], int(r[3]), int(r[4] or 0)) for r in rows]


def graph_stats(session: Session, run_id: uuid.UUID, *, top: int = 10) -> dict[str, Any]:
    kinds = dict(
        session.execute(
            select(Symbol.kind, func.count())
            .where(Symbol.analysis_run_id == run_id)
            .group_by(Symbol.kind)
        ).all()
    )
    edges = dict(
        session.execute(
            select(DependencyEdge.edge_type, func.count())
            .where(DependencyEdge.analysis_run_id == run_id)
            .group_by(DependencyEdge.edge_type)
        ).all()
    )
    resolution = dict(
        session.execute(
            select(SymbolReference.resolution_status, func.count())
            .where(SymbolReference.analysis_run_id == run_id)
            .group_by(SymbolReference.resolution_status)
        ).all()
    )

    def degree(column: Any) -> list[tuple[int, int]]:
        return [
            (int(r[0]), int(r[1]))
            for r in session.execute(
                select(column, func.count())
                .where(
                    DependencyEdge.analysis_run_id == run_id,
                    DependencyEdge.edge_type == EdgeType.CALLS.value,
                )
                .group_by(column)
                .order_by(func.count().desc(), column)
                .limit(top)
            ).all()
        ]

    fan_in, fan_out = degree(DependencyEdge.dst_symbol_id), degree(DependencyEdge.src_symbol_id)
    names = symbols_by_ids(session, run_id, [i for i, _ in fan_in + fan_out])
    return {
        "symbols_by_kind": {k: int(v) for k, v in sorted(kinds.items())},
        "edges_by_type": {k: int(v) for k, v in sorted(edges.items())},
        "references_by_status": {k: int(v) for k, v in sorted(resolution.items())},
        "top_fan_in": [(names[i], n) for i, n in fan_in if i in names],
        "top_fan_out": [(names[i], n) for i, n in fan_out if i in names],
    }


@dataclass(frozen=True, slots=True)
class FileRow:
    id: int
    path: str
    language: str | None
    role: str
    size_bytes: int
    status: str | None
    module_name: str | None
    symbol_count: int | None
    error_count: int | None


def analysis_files(
    session: Session,
    run_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    *,
    status: str | None,
    language: str | None,
    query: str | None,
    limit: int,
    offset: int,
) -> tuple[list[FileRow], int, dict[str, int]]:
    join = and_(AnalysisFile.file_id == SourceFile.id, AnalysisFile.analysis_run_id == run_id)
    stmt = (
        select(
            SourceFile.id,
            SourceFile.path,
            SourceFile.language,
            SourceFile.role,
            SourceFile.size_bytes,
            AnalysisFile.status,
            AnalysisFile.module_name,
            AnalysisFile.symbol_count,
            AnalysisFile.error_count,
        )
        .outerjoin(AnalysisFile, join)
        .where(SourceFile.snapshot_id == snapshot_id)
    )
    if status:
        stmt = stmt.where(AnalysisFile.status == status)
    if language:
        stmt = stmt.where(SourceFile.language == language)
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(SourceFile.path.ilike(f"%{escaped}%", escape="\\"))
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = session.execute(stmt.order_by(SourceFile.path).limit(limit).offset(offset)).all()
    by_status = dict(
        session.execute(
            select(AnalysisFile.status, func.count())
            .where(AnalysisFile.analysis_run_id == run_id)
            .group_by(AnalysisFile.status)
        ).all()
    )
    return (
        [FileRow(*r) for r in rows],
        total,
        {k: int(v) for k, v in sorted(by_status.items())},
    )


def warnings(
    session: Session, run_id: uuid.UUID, *, code: str | None, limit: int, offset: int
) -> tuple[list[tuple[AnalysisWarning, str | None]], int]:
    stmt = (
        select(AnalysisWarning, SourceFile.path)
        .outerjoin(SourceFile, SourceFile.id == AnalysisWarning.file_id)
        .where(AnalysisWarning.analysis_run_id == run_id)
    )
    if code:
        stmt = stmt.where(AnalysisWarning.code == code)
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = session.execute(
        stmt.order_by(SourceFile.path, AnalysisWarning.start_line, AnalysisWarning.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return [(r[0], r[1]) for r in rows], total
