"""Code-graph use cases: symbol search and detail, neighbourhoods, paths, modules, source."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from repolens.codegraph.traversal import (
    Direction,
    EdgePolicy,
    GraphEdge,
    bounded_traversal,
    shortest_path,
    strongly_connected_components,
)
from repolens.domain.enums import AnalysisStatus, EdgeType, SymbolKind
from repolens.persistence import graph_queries as q
from repolens.persistence.models import AnalysisRun, Repository, RepositorySnapshot, SourceFile
from repolens.repository.git import GitError, GitOutputLimitExceeded
from repolens.repository.mirror import MirrorStore
from repolens.services.analysis import effective_run_id
from repolens.services.errors import InvalidInput, NotFound

USABLE = (AnalysisStatus.SUCCEEDED.value, AnalysisStatus.PARTIAL.value, AnalysisStatus.REUSED.value)
DEPENDENCY_TYPES = frozenset(
    {EdgeType.CALLS, EdgeType.IMPORTS, EdgeType.INHERITS, EdgeType.REFERENCES}
)
MAX_SOURCE_BYTES = 2_000_000


@dataclass(frozen=True, slots=True)
class RunContext:
    repository_id: uuid.UUID
    requested_run_id: uuid.UUID
    run: AnalysisRun  # the run that owns the results
    snapshot: RepositorySnapshot


def results_run(
    session: Session, repository_id: uuid.UUID, analysis_id: uuid.UUID | None
) -> RunContext:
    """The analysis to read results from: the given one, or the latest usable one."""
    if session.get(Repository, repository_id) is None:
        raise NotFound("Repository not found")
    if analysis_id is None:
        analysis_id = session.execute(
            select(AnalysisRun.id)
            .where(AnalysisRun.repository_id == repository_id, AnalysisRun.status.in_(USABLE))
            .order_by(AnalysisRun.finished_at.desc().nulls_last(), AnalysisRun.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if analysis_id is None:
            raise NotFound("No completed analysis for this repository", code="no_analysis")
    requested = session.get(AnalysisRun, analysis_id)
    if requested is None or requested.repository_id != repository_id:
        raise NotFound("Analysis not found for this repository")
    if requested.status not in USABLE:
        raise InvalidInput(
            f"Analysis is {requested.status}; results are not available", code="analysis_not_ready"
        )
    run = session.get(AnalysisRun, effective_run_id(session, requested.id))
    if run is None or run.snapshot_id is None:
        raise NotFound("Analysis results not found")
    snapshot = session.get(RepositorySnapshot, run.snapshot_id)
    if snapshot is None:
        raise NotFound("Snapshot not found")
    return RunContext(repository_id, requested.id, run, snapshot)


def run_context_for_analysis(session: Session, analysis_id: uuid.UUID) -> RunContext:
    run = session.get(AnalysisRun, analysis_id)
    if run is None:
        raise NotFound("Analysis not found")
    return results_run(session, run.repository_id, analysis_id)


def parse_edge_types(raw: str | None, default: frozenset[EdgeType]) -> frozenset[EdgeType]:
    if not raw:
        return default
    try:
        return frozenset(EdgeType(part.strip().upper()) for part in raw.split(",") if part.strip())
    except ValueError as exc:
        raise InvalidInput(f"Unknown edge type in '{raw}'", code="invalid_edge_type") from exc


def parse_kinds(raw: str | None) -> list[SymbolKind]:
    if not raw:
        return []
    try:
        return [SymbolKind(part.strip().upper()) for part in raw.split(",") if part.strip()]
    except ValueError as exc:
        raise InvalidInput(f"Unknown symbol kind in '{raw}'", code="invalid_kind") from exc


def require_symbol(session: Session, ctx: RunContext, symbol_id: int) -> q.SymbolRow:
    symbol = q.get_symbol(session, ctx.run.id, symbol_id)
    if symbol is None:
        raise NotFound("Symbol not found in this analysis", code="symbol_not_found")
    return symbol


@dataclass
class SymbolDetail:
    symbol: q.SymbolRow
    parent: q.SymbolRow | None
    children: list[q.SymbolRow]
    callers: list[q.NeighbourRow]
    callers_total: int
    callees: list[q.NeighbourRow]
    callees_total: int
    dependents: list[q.NeighbourRow]  # IMPORTS / REFERENCES / INHERITS pointing here
    dependencies: list[q.NeighbourRow]
    references: list[q.ReferenceRow]
    reference_counts: dict[str, int] = field(default_factory=dict)


def symbol_detail(session: Session, ctx: RunContext, symbol_id: int) -> SymbolDetail:
    symbol = require_symbol(session, ctx, symbol_id)
    run_id = ctx.run.id
    other = [EdgeType.IMPORTS, EdgeType.INHERITS, EdgeType.REFERENCES]
    callers, callers_total = q.neighbours(
        session, run_id, symbol_id, outgoing=False, types=[EdgeType.CALLS]
    )
    callees, callees_total = q.neighbours(
        session, run_id, symbol_id, outgoing=True, types=[EdgeType.CALLS]
    )
    dependents, _ = q.neighbours(session, run_id, symbol_id, outgoing=False, types=other)
    dependencies, _ = q.neighbours(session, run_id, symbol_id, outgoing=True, types=other)
    references, counts = q.outgoing_references(session, run_id, symbol_id)
    parent = q.get_symbol(session, run_id, symbol.parent_id) if symbol.parent_id else None
    return SymbolDetail(
        symbol=symbol,
        parent=parent,
        children=q.children(session, run_id, symbol_id),
        callers=callers,
        callers_total=callers_total,
        callees=callees,
        callees_total=callees_total,
        dependents=dependents,
        dependencies=dependencies,
        references=references,
        reference_counts=counts,
    )


@dataclass
class GraphView:
    nodes: dict[int, q.SymbolRow]
    depths: dict[int, int]
    edges: list[GraphEdge[int]]
    truncated: bool
    truncation_reason: str | None


def neighbourhood(
    session: Session,
    ctx: RunContext,
    symbol_id: int,
    *,
    direction: str,
    depth: int,
    types: frozenset[EdgeType],
    max_nodes: int,
    include_heuristic: bool,
) -> GraphView:
    require_symbol(session, ctx, symbol_id)
    source = q.SqlEdgeSource(session, ctx.run.id)
    directions = [Direction.IN, Direction.OUT] if direction == "BOTH" else [Direction(direction)]
    depths: dict[int, int] = {}
    edges: dict[tuple[int, int, EdgeType], GraphEdge[int]] = {}
    truncated, reason = False, None
    for d in directions:
        policy = EdgePolicy(types=types, direction=d, include_heuristic=include_heuristic)
        result = bounded_traversal(
            source, [symbol_id], policy, max_depth=depth, max_nodes=max_nodes
        )
        truncated = truncated or result.truncated
        reason = reason or result.truncation_reason
        for node, reached in result.reached.items():
            depths[node] = min(depths.get(node, reached.depth), reached.depth)
            if reached.via is not None:
                edges[(reached.via.src, reached.via.dst, reached.via.edge_type)] = reached.via
    nodes = q.symbols_by_ids(session, ctx.run.id, list(depths))
    # Also include edges between reached nodes, so the picture is not just a tree.
    if len(nodes) <= max_nodes:
        for edge in source.out_edges(list(nodes), types):
            if edge.dst in nodes and (include_heuristic or edge.confidence.value == "EXACT"):
                edges.setdefault((edge.src, edge.dst, edge.edge_type), edge)
    return GraphView(
        nodes=nodes,
        depths=depths,
        edges=sorted(edges.values(), key=lambda e: (e.src, e.dst, e.edge_type.value)),
        truncated=truncated,
        truncation_reason=reason,
    )


def dependency_path(
    session: Session,
    ctx: RunContext,
    from_id: int,
    to_id: int,
    *,
    types: frozenset[EdgeType],
    max_depth: int,
) -> tuple[list[GraphEdge[int]] | None, dict[int, q.SymbolRow]]:
    require_symbol(session, ctx, from_id)
    require_symbol(session, ctx, to_id)
    source = q.SqlEdgeSource(session, ctx.run.id)
    path = shortest_path(source, from_id, to_id, types, max_depth=max_depth)
    ids = {from_id, to_id} | {n for e in path or [] for n in (e.src, e.dst)}
    return path, q.symbols_by_ids(session, ctx.run.id, list(ids))


@dataclass
class ModuleSummary:
    modules: list[q.SymbolRow]
    edges: list[q.ModuleEdgeRow]
    import_cycles: list[list[int]]


def module_summary(session: Session, ctx: RunContext) -> ModuleSummary:
    modules, edges = q.module_graph(session, ctx.run.id)
    import_edges = [(e.src_module_id, e.dst_module_id) for e in edges if e.edge_type == "IMPORTS"]
    cycles = strongly_connected_components([m.id for m in modules], import_edges)
    return ModuleSummary(modules=modules, edges=edges, import_cycles=cycles)


def stats(session: Session, ctx: RunContext) -> dict[str, Any]:
    return q.graph_stats(session, ctx.run.id)


class SourceReader:
    """Reads file contents from a repository's bare mirror (never from a checkout)."""

    def __init__(self, mirrors: MirrorStore) -> None:
        self.mirrors = mirrors

    def read(self, session: Session, ctx: RunContext, file_id: int) -> tuple[SourceFile, str]:
        file = session.get(SourceFile, file_id)
        if file is None or file.snapshot_id != ctx.snapshot.id:
            raise NotFound("File not found in this snapshot", code="file_not_found")
        if file.is_binary:
            raise InvalidInput("Binary files cannot be displayed", code="binary_file")
        mirror = self.mirrors.path_for(ctx.repository_id)
        if not (mirror / "HEAD").exists():
            raise NotFound("Repository mirror is not available", code="source_unavailable")
        try:
            with self.mirrors.runner.blob_reader(mirror, timeout=30) as reader:
                data = reader.read(file.blob_sha, max_bytes=MAX_SOURCE_BYTES)
        except GitOutputLimitExceeded as exc:
            raise InvalidInput("File is too large to display", code="file_too_large") from exc
        except GitError as exc:
            raise NotFound("File contents are not available", code="source_unavailable") from exc
        return file, data.decode("utf-8", errors="replace")
