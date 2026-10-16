"""Batched persistence of one analysis run's results (symbols, references, edges, ...).

Everything is written inside the caller's transaction, so a run's results appear atomically
together with its final status. Inserts use multi-row ``INSERT … RETURNING`` with parameter
order preserved, so generated ids can be correlated with the input rows without per-row
round trips.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import insert, update
from sqlalchemy.orm import Session

from repolens.codegraph.resolver import ResolutionResult
from repolens.domain.enums import Confidence, FileAnalysisStatus, ResolutionStatus, Severity
from repolens.parsers.base import ExtractedSymbol
from repolens.persistence.models import (
    AnalysisFile,
    AnalysisWarning,
    DependencyEdge,
    Symbol,
    SymbolReference,
)

BATCH = 2000


@dataclass(frozen=True, slots=True)
class FileOutcome:
    file_id: int
    status: FileAnalysisStatus
    module_name: str | None = None
    symbol_count: int = 0
    error_count: int = 0
    parse_ms: float | None = None


@dataclass(frozen=True, slots=True)
class WarningRecord:
    code: str
    severity: Severity
    message: str
    file_id: int | None = None
    start_line: int | None = None
    end_line: int | None = None
    details: dict[str, Any] | None = None


def _batches[T](items: Sequence[T], size: int = BATCH) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def write_symbols(
    session: Session,
    run_id: uuid.UUID,
    symbols: Sequence[tuple[int, ExtractedSymbol]],
) -> dict[str, int]:
    """Insert ``(file_id, symbol)`` pairs. Returns ``symbol_key → id``."""
    rows = [
        {
            "analysis_run_id": run_id,
            "file_id": file_id,
            "symbol_key": s.key,
            "qualified_name": s.qualified_name,
            "name": s.name,
            "kind": s.kind.value,
            "visibility": s.visibility.value,
            "start_line": s.span.start_line,
            "start_col": s.span.start_col,
            "end_line": s.span.end_line,
            "end_col": s.span.end_col,
            "signature": s.signature,
            "body_fingerprint": s.body_fingerprint,
            "decorators": list(s.decorators),
            "doc_summary": s.doc_summary,
            "is_test": s.is_test,
            "has_parse_errors": s.has_parse_errors,
        }
        for file_id, s in symbols
    ]
    ids: dict[str, int] = {}
    for batch in _batches(rows):
        result = session.execute(
            insert(Symbol).returning(Symbol.id, Symbol.symbol_key, sort_by_parameter_order=True),
            list(batch),
        )
        ids.update({key: sid for sid, key in result.all()})
    return ids


def link_parents(session: Session, ids: dict[str, int], symbols: Iterable[ExtractedSymbol]) -> int:
    updates = [
        {"id": ids[s.key], "parent_id": ids[s.parent_key]}
        for s in symbols
        if s.parent_key is not None and s.parent_key in ids and s.key in ids
    ]
    for batch in _batches(updates):
        session.execute(update(Symbol), list(batch))
    return len(updates)


def write_references(
    session: Session,
    run_id: uuid.UUID,
    file_ids: dict[str, int],
    symbol_ids: dict[str, int],
    resolution: ResolutionResult,
) -> list[int | None]:
    """Insert references; returns database ids aligned with ``resolution.references``."""
    rows: list[dict[str, Any]] = []
    positions: list[int] = []
    for index, item in enumerate(resolution.references):
        ref, res = item.ref, item.resolution
        source_id = symbol_ids.get(ref.source_key)
        if source_id is None or item.path not in file_ids:
            continue
        target_id = symbol_ids.get(res.target) if res.target else None
        status = res.status
        if status is ResolutionStatus.RESOLVED and target_id is None:
            status = ResolutionStatus.UNRESOLVED  # target was not persisted (should not happen)
        rows.append(
            {
                "analysis_run_id": run_id,
                "file_id": file_ids[item.path],
                "source_symbol_id": source_id,
                "ref_kind": ref.kind.value,
                "raw_text": ref.text,
                "start_line": ref.span.start_line,
                "start_col": ref.span.start_col,
                "end_line": ref.span.end_line,
                "end_col": ref.span.end_col,
                "resolution_status": status.value,
                "target_symbol_id": target_id if status is ResolutionStatus.RESOLVED else None,
                "external_target": res.external,
                "candidates": list(res.candidates) or None,
                "resolver_rule": res.rule,
            }
        )
        positions.append(index)
    aligned: list[int | None] = [None] * len(resolution.references)
    cursor = 0
    for batch in _batches(rows):
        result = session.execute(
            insert(SymbolReference).returning(SymbolReference.id, sort_by_parameter_order=True),
            list(batch),
        )
        for (ref_id,) in result.all():
            aligned[positions[cursor]] = ref_id
            cursor += 1
    return aligned


def write_edges(
    session: Session,
    run_id: uuid.UUID,
    symbol_ids: dict[str, int],
    resolution: ResolutionResult,
    reference_ids: list[int | None],
) -> dict[str, int]:
    """Aggregate edges by (src, dst, type, provenance) and insert them. Returns type counts."""
    grouped: dict[tuple[int, int, str, str], dict[str, Any]] = {}
    for edge in resolution.edges:
        src, dst = symbol_ids.get(edge.src), symbol_ids.get(edge.dst)
        if src is None or dst is None:
            continue
        key = (src, dst, edge.edge_type.value, edge.provenance.value)
        entry = grouped.get(key)
        ref_id = reference_ids[edge.reference_index] if edge.reference_index is not None else None
        if entry is None:
            entry = grouped[key] = {
                "analysis_run_id": run_id,
                "src_symbol_id": src,
                "dst_symbol_id": dst,
                "edge_type": edge.edge_type.value,
                "provenance": edge.provenance.value,
                "confidence": edge.confidence.value,
                "reference_count": 0,
                "first_reference_id": ref_id,
                "evidence": {"rules": []},
            }
        entry["reference_count"] += 1 if edge.reference_index is not None else 0
        if edge.confidence is Confidence.EXACT:
            entry["confidence"] = Confidence.EXACT.value
        if edge.rule and edge.rule not in entry["evidence"]["rules"]:
            entry["evidence"]["rules"].append(edge.rule)
    rows = list(grouped.values())
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        row["evidence"]["rules"].sort()
        if not row["evidence"]["rules"]:
            row["evidence"] = None
        counts[row["edge_type"]] += 1
    for batch in _batches(rows):
        session.execute(insert(DependencyEdge), list(batch))
    return dict(sorted(counts.items()))


def write_file_outcomes(session: Session, run_id: uuid.UUID, outcomes: list[FileOutcome]) -> None:
    rows = [
        {
            "analysis_run_id": run_id,
            "file_id": o.file_id,
            "status": o.status.value,
            "module_name": o.module_name,
            "symbol_count": o.symbol_count,
            "error_count": o.error_count,
            "parse_ms": o.parse_ms,
        }
        for o in outcomes
    ]
    for batch in _batches(rows):
        session.execute(insert(AnalysisFile), list(batch))


def write_warnings(session: Session, run_id: uuid.UUID, warnings: list[WarningRecord]) -> None:
    rows = [
        {
            "analysis_run_id": run_id,
            "file_id": w.file_id,
            "code": w.code,
            "severity": w.severity.value,
            "message": w.message,
            "start_line": w.start_line,
            "end_line": w.end_line,
            "details": w.details,
        }
        for w in warnings
    ]
    for batch in _batches(rows):
        session.execute(insert(AnalysisWarning), list(batch))
