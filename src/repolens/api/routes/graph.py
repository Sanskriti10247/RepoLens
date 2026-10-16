"""Code-graph exploration endpoints. All results are scoped to one analysis run."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request

from repolens.api.dependencies import SessionDep
from repolens.api.pagination import Pagination
from repolens.api.schemas import (
    DegreeOut,
    FileOut,
    FilePage,
    GraphEdgeOut,
    GraphNodeOut,
    GraphOut,
    ModuleEdgeOut,
    ModuleGraphOut,
    ModuleNodeOut,
    NeighbourOut,
    PageMeta,
    PathOut,
    ReferenceOut,
    SourceOut,
    StatsOut,
    SymbolDetailOut,
    SymbolPage,
    SymbolSummary,
    WarningOut,
    WarningPage,
)
from repolens.codegraph.traversal import GraphEdge
from repolens.persistence import graph_queries as q
from repolens.services import graph as graph_service

router = APIRouter(tags=["code graph"])
AnalysisParam = Annotated[
    uuid.UUID | None,
    Query(description="Analysis to read; defaults to the latest completed one"),
]


def _summary(row: q.SymbolRow) -> SymbolSummary:
    return SymbolSummary.model_validate(asdict(row))


def _neighbour(row: q.NeighbourRow) -> NeighbourOut:
    return NeighbourOut(
        edge_type=row.edge_type,
        confidence=row.confidence,
        provenance=row.provenance,
        reference_count=row.reference_count,
        rules=row.rules,
        symbol=_summary(row.symbol),
    )


def _edge(edge: GraphEdge[int]) -> GraphEdgeOut:
    return GraphEdgeOut(
        src=edge.src, dst=edge.dst, edge_type=edge.edge_type.value, confidence=edge.confidence.value
    )


@router.get("/repositories/{repository_id}/symbols", response_model=SymbolPage)
def search_symbols(
    repository_id: uuid.UUID,
    session: SessionDep,
    page: Pagination,
    analysis_id: AnalysisParam = None,
    q_: Annotated[str | None, Query(alias="q", max_length=200)] = None,
    kind: Annotated[str | None, Query(description="Comma-separated symbol kinds")] = None,
    path: Annotated[str | None, Query(max_length=1000)] = None,
    is_test: bool | None = None,
) -> SymbolPage:
    ctx = graph_service.results_run(session, repository_id, analysis_id)
    rows, total = q.find_symbols(
        session,
        ctx.run.id,
        query=q_,
        kinds=graph_service.parse_kinds(kind),
        path=path,
        is_test=is_test,
        limit=page.limit,
        offset=page.offset,
    )
    return SymbolPage(
        analysis_id=ctx.requested_run_id,
        results_run_id=ctx.run.id,
        commit_sha=ctx.snapshot.commit_sha,
        items=[_summary(r) for r in rows],
        page=PageMeta(total=total, limit=page.limit, offset=page.offset),
    )


@router.get("/repositories/{repository_id}/symbols/{symbol_id}", response_model=SymbolDetailOut)
def get_symbol(
    repository_id: uuid.UUID,
    symbol_id: int,
    session: SessionDep,
    analysis_id: AnalysisParam = None,
) -> SymbolDetailOut:
    ctx = graph_service.results_run(session, repository_id, analysis_id)
    detail = graph_service.symbol_detail(session, ctx, symbol_id)
    return SymbolDetailOut(
        analysis_id=ctx.requested_run_id,
        commit_sha=ctx.snapshot.commit_sha,
        symbol=_summary(detail.symbol),
        body_fingerprint=detail.symbol.body_fingerprint,
        decorators=detail.symbol.decorators,
        parent=_summary(detail.parent) if detail.parent else None,
        children=[_summary(c) for c in detail.children],
        callers=[_neighbour(n) for n in detail.callers],
        callers_total=detail.callers_total,
        callees=[_neighbour(n) for n in detail.callees],
        callees_total=detail.callees_total,
        dependents=[_neighbour(n) for n in detail.dependents],
        dependencies=[_neighbour(n) for n in detail.dependencies],
        references=[ReferenceOut.model_validate(asdict(r)) for r in detail.references],
        reference_counts=detail.reference_counts,
    )


@router.get("/repositories/{repository_id}/graph", response_model=GraphOut)
def get_graph(
    repository_id: uuid.UUID,
    session: SessionDep,
    symbol_id: int,
    analysis_id: AnalysisParam = None,
    direction: Literal["IN", "OUT", "BOTH"] = "BOTH",
    depth: Annotated[int, Query(ge=1, le=6)] = 2,
    edge_types: Annotated[str | None, Query(description="e.g. CALLS,IMPORTS")] = None,
    max_nodes: Annotated[int, Query(ge=1, le=500)] = 150,
    include_heuristic: bool = True,
) -> GraphOut:
    """Bounded neighbourhood of a symbol (reports truncation instead of growing unbounded)."""
    ctx = graph_service.results_run(session, repository_id, analysis_id)
    types = graph_service.parse_edge_types(edge_types, graph_service.DEPENDENCY_TYPES)
    view = graph_service.neighbourhood(
        session,
        ctx,
        symbol_id,
        direction=direction,
        depth=depth,
        types=types,
        max_nodes=max_nodes,
        include_heuristic=include_heuristic,
    )
    return GraphOut(
        analysis_id=ctx.requested_run_id,
        commit_sha=ctx.snapshot.commit_sha,
        root=symbol_id,
        nodes=[
            GraphNodeOut(depth=view.depths[i], symbol=_summary(row))
            for i, row in sorted(view.nodes.items(), key=lambda kv: (view.depths[kv[0]], kv[0]))
        ],
        edges=[_edge(e) for e in view.edges],
        truncated=view.truncated,
        truncation_reason=view.truncation_reason,
    )


@router.get("/repositories/{repository_id}/paths", response_model=PathOut)
def get_path(
    repository_id: uuid.UUID,
    session: SessionDep,
    from_symbol: Annotated[int, Query(alias="from")],
    to_symbol: Annotated[int, Query(alias="to")],
    analysis_id: AnalysisParam = None,
    edge_types: str | None = None,
    max_depth: Annotated[int, Query(ge=1, le=12)] = 8,
) -> PathOut:
    """Shortest dependency path ``from → to`` (following edges forward)."""
    ctx = graph_service.results_run(session, repository_id, analysis_id)
    types = graph_service.parse_edge_types(edge_types, graph_service.DEPENDENCY_TYPES)
    path, symbols = graph_service.dependency_path(
        session, ctx, from_symbol, to_symbol, types=types, max_depth=max_depth
    )
    return PathOut(
        found=path is not None,
        edges=[_edge(e) for e in path or []],
        symbols=[_summary(s) for s in symbols.values()],
    )


@router.get("/repositories/{repository_id}/files/{file_id}/source", response_model=SourceOut)
def get_source(
    repository_id: uuid.UUID,
    file_id: int,
    session: SessionDep,
    request: Request,
    analysis_id: AnalysisParam = None,
) -> SourceOut:
    """File contents at the analysed commit, read from the repository's bare mirror."""
    ctx = graph_service.results_run(session, repository_id, analysis_id)
    reader: graph_service.SourceReader = request.app.state.source_reader
    file, content = reader.read(session, ctx, file_id)
    return SourceOut(
        file_id=file.id,
        path=file.path,
        language=file.language,
        commit_sha=ctx.snapshot.commit_sha,
        blob_sha=file.blob_sha,
        content=content,
    )


@router.get("/analyses/{analysis_id}/files", response_model=FilePage)
def list_files(
    analysis_id: uuid.UUID,
    session: SessionDep,
    page: Pagination,
    status: str | None = None,
    language: str | None = None,
    q_: Annotated[str | None, Query(alias="q", max_length=200)] = None,
) -> FilePage:
    ctx = graph_service.run_context_for_analysis(session, analysis_id)
    rows, total, counts = q.analysis_files(
        session,
        ctx.run.id,
        ctx.snapshot.id,
        status=status,
        language=language,
        query=q_,
        limit=page.limit,
        offset=page.offset,
    )
    return FilePage(
        items=[FileOut.model_validate(asdict(r)) for r in rows],
        page=PageMeta(total=total, limit=page.limit, offset=page.offset),
        status_counts=counts,
    )


@router.get("/analyses/{analysis_id}/warnings", response_model=WarningPage)
def list_warnings(
    analysis_id: uuid.UUID, session: SessionDep, page: Pagination, code: str | None = None
) -> WarningPage:
    ctx = graph_service.run_context_for_analysis(session, analysis_id)
    rows, total = q.warnings(session, ctx.run.id, code=code, limit=page.limit, offset=page.offset)
    return WarningPage(
        items=[
            WarningOut(
                id=w.id,
                code=w.code,
                severity=w.severity,
                message=w.message,
                path=path,
                start_line=w.start_line,
                end_line=w.end_line,
            )
            for w, path in rows
        ],
        page=PageMeta(total=total, limit=page.limit, offset=page.offset),
    )


@router.get("/analyses/{analysis_id}/modules", response_model=ModuleGraphOut)
def module_graph(analysis_id: uuid.UUID, session: SessionDep) -> ModuleGraphOut:
    """Module-level dependency summary and import cycles."""
    ctx = graph_service.run_context_for_analysis(session, analysis_id)
    summary = graph_service.module_summary(session, ctx)
    return ModuleGraphOut(
        analysis_id=analysis_id,
        modules=[
            ModuleNodeOut(id=m.id, name=m.qualified_name, path=m.path) for m in summary.modules
        ],
        edges=[ModuleEdgeOut.model_validate(asdict(e)) for e in summary.edges],
        import_cycles=summary.import_cycles,
    )


@router.get("/analyses/{analysis_id}/stats", response_model=StatsOut)
def graph_stats(analysis_id: uuid.UUID, session: SessionDep) -> StatsOut:
    ctx = graph_service.run_context_for_analysis(session, analysis_id)
    data = graph_service.stats(session, ctx)
    return StatsOut(
        analysis_id=analysis_id,
        symbols_by_kind=data["symbols_by_kind"],
        edges_by_type=data["edges_by_type"],
        references_by_status=data["references_by_status"],
        top_fan_in=[DegreeOut(symbol=_summary(s), count=n) for s, n in data["top_fan_in"]],
        top_fan_out=[DegreeOut(symbol=_summary(s), count=n) for s, n in data["top_fan_out"]],
    )
