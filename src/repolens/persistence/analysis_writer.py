"""Bulk persistence of one analysis run's results (symbols, references, edges, ...).

Everything is written inside the caller's transaction, so a run's results appear atomically
together with its final status.

Performance: ids are reserved from each table's identity sequence in a single query, and
rows are then streamed with PostgreSQL ``COPY``. Knowing ids up front also lets parent links
and edge→reference links be written in the same pass (no follow-up ``UPDATE``). On the
``click`` repository this replaced ~5.9 s of row-at-a-time ``INSERT … RETURNING`` work
(see docs/benchmarks).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb
from sqlalchemy import text
from sqlalchemy.orm import Session

from repolens.codegraph.resolver import ResolutionResult
from repolens.domain.enums import Confidence, FileAnalysisStatus, ResolutionStatus, Severity
from repolens.parsers.base import ExtractedSymbol


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


# Table and column names below are constants, never user input.
_SYMBOL_COLUMNS = (
    "id, analysis_run_id, file_id, parent_id, symbol_key, qualified_name, name, kind, "
    "visibility, start_line, start_col, end_line, end_col, signature, body_fingerprint, "
    "decorators, doc_summary, is_test, has_parse_errors, complexity"
)
_REFERENCE_COLUMNS = (
    "id, analysis_run_id, file_id, source_symbol_id, ref_kind, raw_text, start_line, "
    "start_col, end_line, end_col, resolution_status, target_symbol_id, external_target, "
    "candidates, resolver_rule, type_checking"
)
_EDGE_COLUMNS = (
    "analysis_run_id, src_symbol_id, dst_symbol_id, edge_type, provenance, confidence, "
    "reference_count, first_reference_id, evidence"
)
_FILE_COLUMNS = "analysis_run_id, file_id, status, module_name, symbol_count, error_count, parse_ms"
_WARNING_COLUMNS = (
    "analysis_run_id, file_id, code, severity, message, start_line, end_line, details"
)


def reserve_ids(session: Session, table: str, count: int) -> list[int]:
    """Take ``count`` values from the table's identity sequence in one round trip."""
    if count == 0:
        return []
    rows = session.execute(
        text("SELECT nextval(pg_get_serial_sequence(:table, 'id')) FROM generate_series(1, :n)"),
        {"table": table, "n": count},
    ).scalars()
    return [int(value) for value in rows]


def copy_rows(session: Session, table: str, columns: str, rows: Iterable[Sequence[Any]]) -> int:
    """Stream rows with ``COPY`` on the session's own connection (same transaction)."""
    connection = session.connection().connection.driver_connection
    if connection is None:  # pragma: no cover - a live session always has a connection
        raise RuntimeError("no database connection for COPY")
    written = 0
    with connection.cursor() as cursor, cursor.copy(f"COPY {table} ({columns}) FROM STDIN") as copy:
        for row in rows:
            copy.write_row(row)
            written += 1
    return written


def _json(value: Any) -> Jsonb | None:
    return None if value is None else Jsonb(value)


def write_symbols(
    session: Session,
    run_id: uuid.UUID,
    symbols: Sequence[tuple[int, ExtractedSymbol]],
) -> dict[str, int]:
    """Insert ``(file_id, symbol)`` pairs with parent links. Returns ``symbol_key → id``."""
    ids = dict(
        zip((s.key for _, s in symbols), reserve_ids(session, "symbols", len(symbols)), strict=True)
    )
    copy_rows(
        session,
        "symbols",
        _SYMBOL_COLUMNS,
        (
            (
                ids[s.key],
                run_id,
                file_id,
                ids.get(s.parent_key) if s.parent_key else None,
                s.key,
                s.qualified_name,
                s.name,
                s.kind.value,
                s.visibility.value,
                s.span.start_line,
                s.span.start_col,
                s.span.end_line,
                s.span.end_col,
                s.signature,
                s.body_fingerprint,
                Jsonb(list(s.decorators)),
                s.doc_summary,
                s.is_test,
                s.has_parse_errors,
                s.complexity,
            )
            for file_id, s in symbols
        ),
    )
    return ids


def write_references(
    session: Session,
    run_id: uuid.UUID,
    file_ids: dict[str, int],
    symbol_ids: dict[str, int],
    resolution: ResolutionResult,
) -> list[int | None]:
    """Insert references; returns database ids aligned with ``resolution.references``."""
    storable = [
        index
        for index, item in enumerate(resolution.references)
        if item.ref.source_key in symbol_ids and item.path in file_ids
    ]
    reserved = reserve_ids(session, "symbol_references", len(storable))
    aligned: list[int | None] = [None] * len(resolution.references)
    rows = []
    for index, ref_id in zip(storable, reserved, strict=True):
        item = resolution.references[index]
        ref, res = item.ref, item.resolution
        target_id = symbol_ids.get(res.target) if res.target else None
        status = res.status
        if status is ResolutionStatus.RESOLVED and target_id is None:
            status = ResolutionStatus.UNRESOLVED  # target was not persisted (should not happen)
        aligned[index] = ref_id
        rows.append(
            (
                ref_id,
                run_id,
                file_ids[item.path],
                symbol_ids[ref.source_key],
                ref.kind.value,
                ref.text,
                ref.span.start_line,
                ref.span.start_col,
                ref.span.end_line,
                ref.span.end_col,
                status.value,
                target_id if status is ResolutionStatus.RESOLVED else None,
                res.external,
                _json(list(res.candidates) or None),
                res.rule,
                ref.type_checking,
            )
        )
    copy_rows(session, "symbol_references", _REFERENCE_COLUMNS, rows)
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
        ref_id = reference_ids[edge.reference_index] if edge.reference_index is not None else None
        entry = grouped.setdefault(
            key,
            {
                "confidence": edge.confidence.value,
                "count": 0,
                "first_reference_id": ref_id,
                "rules": set(),
            },
        )
        entry["count"] += 1 if edge.reference_index is not None else 0
        if edge.confidence is Confidence.EXACT:
            entry["confidence"] = Confidence.EXACT.value
        if edge.rule:
            entry["rules"].add(edge.rule)
    counts: dict[str, int] = defaultdict(int)
    rows = []
    for (src, dst, edge_type, provenance), entry in grouped.items():
        counts[edge_type] += 1
        rules = sorted(entry["rules"])
        rows.append(
            (
                run_id,
                src,
                dst,
                edge_type,
                provenance,
                entry["confidence"],
                entry["count"],
                entry["first_reference_id"],
                Jsonb({"rules": rules}) if rules else None,
            )
        )
    copy_rows(session, "dependency_edges", _EDGE_COLUMNS, rows)
    return dict(sorted(counts.items()))


def write_file_outcomes(session: Session, run_id: uuid.UUID, outcomes: list[FileOutcome]) -> None:
    copy_rows(
        session,
        "analysis_files",
        _FILE_COLUMNS,
        (
            (
                run_id,
                o.file_id,
                o.status.value,
                o.module_name,
                o.symbol_count,
                o.error_count,
                o.parse_ms,
            )
            for o in outcomes
        ),
    )


def write_warnings(session: Session, run_id: uuid.UUID, warnings: list[WarningRecord]) -> None:
    copy_rows(
        session,
        "analysis_warnings",
        _WARNING_COLUMNS,
        (
            (
                run_id,
                w.file_id,
                w.code,
                w.severity.value,
                w.message,
                w.start_line,
                w.end_line,
                _json(w.details),
            )
            for w in warnings
        ),
    )
