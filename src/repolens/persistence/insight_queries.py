"""Aggregate read queries behind the repository health report (services/insights.py).

Every query is scoped to one analysis run, aggregates in PostgreSQL and returns at most
``limit`` detail rows, so the report stays cheap on large repositories. The selection rules
(what counts as public, testable or a dead-code candidate) are documented in
docs/insights.md and mirrored in the comments below.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from repolens.domain.enums import SymbolKind

FUNCTION_KINDS = (
    SymbolKind.FUNCTION.value,
    SymbolKind.ASYNC_FUNCTION.value,
    SymbolKind.METHOD.value,
    SymbolKind.ASYNC_METHOD.value,
)
DEFINITION_KINDS = (*FUNCTION_KINDS, SymbolKind.CLASS.value)

# A name is outside the public API when any dotted component starts with "_" (private
# modules, classes and functions, and the synthetic "__file__." script modules) or when it is
# local to a function.
_NOT_PUBLIC = "(s.qualified_name ~ '(^|\\.)_' OR strpos(s.qualified_name, '<locals>') > 0)"
_FUNCTION_FILTER = (
    "s.analysis_run_id = :run AND s.kind IN :functions AND NOT s.is_test AND f.role <> 'TEST'"
)


def _rows(session: Session, sql: str, **params: Any) -> list[Any]:
    statement = text(sql)
    for name in ("functions", "definitions"):
        if name in params:
            statement = statement.bindparams(bindparam(name, expanding=True))
    return list(session.execute(statement, params).all())


@dataclass(frozen=True, slots=True)
class FileGroup:
    role: str
    language: str | None
    files: int
    bytes: int
    lines: int | None  # None when no file of the group has a line count (older snapshots)


def file_groups(session: Session, snapshot_id: uuid.UUID) -> list[FileGroup]:
    rows = _rows(
        session,
        "SELECT role, language, count(*), coalesce(sum(size_bytes), 0), sum(line_count) "
        "FROM source_files WHERE snapshot_id = :snapshot GROUP BY role, language",
        snapshot=snapshot_id,
    )
    return [
        FileGroup(r[0], r[1], int(r[2]), int(r[3]), None if r[4] is None else int(r[4]))
        for r in rows
    ]


def symbol_kinds(session: Session, run_id: uuid.UUID) -> dict[str, int]:
    rows = _rows(
        session,
        "SELECT kind, count(*) FROM symbols WHERE analysis_run_id = :run GROUP BY kind",
        run=run_id,
    )
    return {r[0]: int(r[1]) for r in rows}


@dataclass(frozen=True, slots=True)
class SymbolItem:
    symbol_id: int
    qualified_name: str
    kind: str
    file_id: int
    path: str
    line: int
    lines: int
    value: int | None  # complexity, callers, ... depending on the query


_ITEM_COLUMNS = (
    "s.id, s.qualified_name, s.kind, f.id, f.path, s.start_line, "
    "s.end_line - s.start_line + 1 AS lines"
)


def _items(rows: list[Any]) -> list[SymbolItem]:
    return [
        SymbolItem(int(r[0]), r[1], r[2], int(r[3]), r[4], int(r[5]), int(r[6]), r[7]) for r in rows
    ]


@dataclass(frozen=True, slots=True)
class FunctionStats:
    functions: int  # non-test functions and methods
    measured: int  # of which have a stored complexity
    mean: float | None
    p90: int | None
    buckets: dict[str, int]
    over_100_lines: int


def function_stats(session: Session, run_id: uuid.UUID) -> FunctionStats:
    row = _rows(
        session,
        f"""
        SELECT count(*), count(s.complexity), avg(s.complexity),
               percentile_disc(0.9) WITHIN GROUP (ORDER BY s.complexity),
               count(*) FILTER (WHERE s.complexity BETWEEN 1 AND 5),
               count(*) FILTER (WHERE s.complexity BETWEEN 6 AND 10),
               count(*) FILTER (WHERE s.complexity BETWEEN 11 AND 20),
               count(*) FILTER (WHERE s.complexity >= 21),
               count(*) FILTER (WHERE s.end_line - s.start_line + 1 > 100)
        FROM symbols s JOIN source_files f ON f.id = s.file_id
        WHERE {_FUNCTION_FILTER}
        """,  # noqa: S608 - constant SQL fragments; values are bound
        run=run_id,
        functions=list(FUNCTION_KINDS),
    )[0]
    return FunctionStats(
        functions=int(row[0]),
        measured=int(row[1]),
        mean=float(row[2]) if row[2] is not None else None,
        p90=int(row[3]) if row[3] is not None else None,
        buckets={"1-5": int(row[4]), "6-10": int(row[5]), "11-20": int(row[6]), "21+": int(row[7])},
        over_100_lines=int(row[8]),
    )


def most_complex(
    session: Session, run_id: uuid.UUID, *, limit: int, below: int | None = None
) -> list[SymbolItem]:
    """Most complex functions, optionally only those with complexity < ``below``."""
    return _items(
        _rows(
            session,
            f"""
            SELECT {_ITEM_COLUMNS}, s.complexity
            FROM symbols s JOIN source_files f ON f.id = s.file_id
            WHERE {_FUNCTION_FILTER} AND s.complexity IS NOT NULL
              AND (CAST(:below AS integer) IS NULL OR s.complexity < :below)
            ORDER BY s.complexity DESC, lines DESC, s.id LIMIT :limit
            """,  # noqa: S608 - constant SQL fragments; values are bound
            run=run_id,
            functions=list(FUNCTION_KINDS),
            limit=limit,
            below=below,
        )
    )


def longest_functions(session: Session, run_id: uuid.UUID, *, limit: int) -> list[SymbolItem]:
    return _items(
        _rows(
            session,
            f"""
            SELECT {_ITEM_COLUMNS}, s.complexity
            FROM symbols s JOIN source_files f ON f.id = s.file_id
            WHERE {_FUNCTION_FILTER}
            ORDER BY lines DESC, s.id LIMIT :limit
            """,  # noqa: S608 - constant SQL fragments; values are bound
            run=run_id,
            functions=list(FUNCTION_KINDS),
            limit=limit,
        )
    )


@dataclass(frozen=True, slots=True)
class FileItem:
    file_id: int
    path: str
    bytes: int
    symbols: int
    lines: int | None
    module_symbol_id: int | None


def largest_files(session: Session, run_id: uuid.UUID, *, limit: int) -> list[FileItem]:
    """Largest analysed source files, with their definitions and line counts."""
    rows = _rows(
        session,
        """
        SELECT f.id, f.path, f.size_bytes, greatest(af.symbol_count - 1, 0),
               m.end_line - CASE WHEN m.end_col = 0 THEN 1 ELSE 0 END, m.id
        FROM analysis_files af
        JOIN source_files f ON f.id = af.file_id
        LEFT JOIN symbols m ON m.analysis_run_id = af.analysis_run_id
             AND m.file_id = af.file_id AND m.kind = 'MODULE'
        WHERE af.analysis_run_id = :run AND af.module_name IS NOT NULL
        ORDER BY f.size_bytes DESC, f.path LIMIT :limit
        """,
        run=run_id,
        limit=limit,
    )
    return [FileItem(int(r[0]), r[1], int(r[2]), int(r[3]), r[4], r[5]) for r in rows]


def long_modules(
    session: Session, run_id: uuid.UUID, *, min_lines: int, limit: int
) -> tuple[int, list[SymbolItem]]:
    """Non-test modules longer than ``min_lines`` (count, longest first)."""
    rows = _rows(
        session,
        f"""
        SELECT {_ITEM_COLUMNS}, s.end_line - CASE WHEN s.end_col = 0 THEN 1 ELSE 0 END AS n,
               count(*) OVER ()
        FROM symbols s JOIN source_files f ON f.id = s.file_id
        WHERE s.analysis_run_id = :run AND s.kind = 'MODULE' AND f.role <> 'TEST'
          AND s.end_line - CASE WHEN s.end_col = 0 THEN 1 ELSE 0 END > :min_lines
        ORDER BY n DESC, s.id LIMIT :limit
        """,  # noqa: S608 - constant SQL fragments; values are bound
        run=run_id,
        min_lines=min_lines,
        limit=limit,
    )
    return (int(rows[0][8]) if rows else 0), _items([r[:8] for r in rows])


_CALLERS = """
callers AS (
    SELECT dst_symbol_id AS id, count(DISTINCT src_symbol_id) AS n
    FROM dependency_edges
    WHERE analysis_run_id = :run AND edge_type = 'CALLS' AND src_symbol_id <> dst_symbol_id
    GROUP BY dst_symbol_id
)
"""
# Public API: classes, functions and methods in non-test code, not private, not local.
_PUBLIC = (
    "s.analysis_run_id = :run AND s.kind IN :definitions AND NOT s.is_test "
    f"AND f.role <> 'TEST' AND s.visibility = 'PUBLIC' AND NOT {_NOT_PUBLIC}"
)


def documentation_counts(session: Session, run_id: uuid.UUID) -> tuple[int, int]:
    """(public symbols, of which have a docstring)."""
    row = _rows(
        session,
        f"""
        SELECT count(*), count(s.doc_summary)
        FROM symbols s JOIN source_files f ON f.id = s.file_id
        WHERE {_PUBLIC}
        """,  # noqa: S608 - constant SQL fragments; values are bound
        run=run_id,
        definitions=list(DEFINITION_KINDS),
    )[0]
    return int(row[0]), int(row[1])


def undocumented(session: Session, run_id: uuid.UUID, *, limit: int) -> list[SymbolItem]:
    """Public symbols without a docstring, most called first."""
    return _items(
        _rows(
            session,
            f"""
            WITH {_CALLERS}
            SELECT {_ITEM_COLUMNS}, coalesce(c.n, 0) AS callers
            FROM symbols s JOIN source_files f ON f.id = s.file_id
            LEFT JOIN callers c ON c.id = s.id
            WHERE {_PUBLIC} AND s.doc_summary IS NULL
            ORDER BY callers DESC, s.qualified_name, s.id LIMIT :limit
            """,  # noqa: S608 - constant SQL fragments; values are bound
            run=run_id,
            definitions=list(DEFINITION_KINDS),
            limit=limit,
        )
    )


def hubs(session: Session, run_id: uuid.UUID, *, limit: int) -> list[SymbolItem]:
    """Non-test definitions with the most distinct callers."""
    return _items(
        _rows(
            session,
            f"""
            WITH {_CALLERS}
            SELECT {_ITEM_COLUMNS}, c.n
            FROM callers c
            JOIN symbols s ON s.id = c.id
            JOIN source_files f ON f.id = s.file_id
            WHERE s.kind IN :definitions AND NOT s.is_test AND f.role <> 'TEST'
            ORDER BY c.n DESC, s.qualified_name, s.id LIMIT :limit
            """,  # noqa: S608 - constant SQL fragments; values are bound
            run=run_id,
            definitions=list(DEFINITION_KINDS),
            limit=limit,
        )
    )


@dataclass(frozen=True, slots=True)
class ModuleRow:
    symbol_id: int
    name: str
    file_id: int
    path: str
    is_test: bool  # the file's role is TEST
    definitions: int  # classes, functions and methods in the file


def modules(session: Session, run_id: uuid.UUID) -> list[ModuleRow]:
    rows = _rows(
        session,
        """
        WITH defs AS (
            SELECT file_id, count(*) AS n FROM symbols
            WHERE analysis_run_id = :run AND kind <> 'MODULE' GROUP BY file_id
        )
        SELECT s.id, s.qualified_name, f.id, f.path, f.role = 'TEST', coalesce(d.n, 0)
        FROM symbols s
        JOIN source_files f ON f.id = s.file_id
        LEFT JOIN defs d ON d.file_id = s.file_id
        WHERE s.analysis_run_id = :run AND s.kind = 'MODULE'
        ORDER BY s.qualified_name, s.id
        """,
        run=run_id,
    )
    return [ModuleRow(int(r[0]), r[1], int(r[2]), r[3], bool(r[4]), int(r[5])) for r in rows]


@dataclass(frozen=True, slots=True)
class FileEdge:
    src_file_id: int
    dst_file_id: int
    edge_type: str


def file_edges(session: Session, run_id: uuid.UUID) -> list[FileEdge]:
    """Symbol-level dependencies (not containment) collapsed to distinct file pairs."""
    rows = _rows(
        session,
        """
        SELECT DISTINCT a.file_id, b.file_id, e.edge_type
        FROM dependency_edges e
        JOIN symbols a ON a.id = e.src_symbol_id
        JOIN symbols b ON b.id = e.dst_symbol_id
        WHERE e.analysis_run_id = :run AND e.edge_type <> 'CONTAINS' AND a.file_id <> b.file_id
        """,
        run=run_id,
    )
    return [FileEdge(int(r[0]), int(r[1]), r[2]) for r in rows]


def runtime_imports(session: Session, run_id: uuid.UUID) -> list[tuple[int, int]]:
    """File pairs linked by a resolved module-level import that runs at import time.

    Imports inside functions (deferred) and inside ``if TYPE_CHECKING:`` are left out; runs
    parsed before the flag existed (NULL) count every module-level import.
    """
    rows = _rows(
        session,
        """
        SELECT DISTINCT a.file_id, b.file_id
        FROM symbol_references r
        JOIN symbols a ON a.id = r.source_symbol_id
        JOIN symbols b ON b.id = r.target_symbol_id
        WHERE r.analysis_run_id = :run AND r.resolution_status = 'RESOLVED'
          AND r.ref_kind = 'IMPORT' AND r.type_checking IS NOT TRUE
          AND a.kind = 'MODULE' AND a.file_id <> b.file_id
        """,
        run=run_id,
    )
    return [(int(r[0]), int(r[1])) for r in rows]


def test_function_count(session: Session, run_id: uuid.UUID) -> int:
    rows = _rows(
        session,
        "SELECT count(*) FROM symbols WHERE analysis_run_id = :run AND is_test "
        "AND kind IN :functions",
        run=run_id,
        functions=list(FUNCTION_KINDS),
    )
    return int(rows[0][0])


def reference_counts(session: Session, run_id: uuid.UUID) -> dict[str, int]:
    rows = _rows(
        session,
        "SELECT resolution_status, count(*) FROM symbol_references "
        "WHERE analysis_run_id = :run GROUP BY resolution_status",
        run=run_id,
    )
    return {r[0]: int(r[1]) for r in rows}


def file_status_counts(session: Session, run_id: uuid.UUID) -> dict[str, int]:
    rows = _rows(
        session,
        "SELECT status, count(*) FROM analysis_files WHERE analysis_run_id = :run GROUP BY status",
        run=run_id,
    )
    return {r[0]: int(r[1]) for r in rows}


# Dead-code candidates. A definition qualifies only if all of these hold:
# - a class, function or method in a SOURCE file (not tests, docs, config or .pyi stubs);
# - outside the public API (see _NOT_PUBLIC), and for methods the method name itself is
#   private: public names of importable modules, and public method names (protocol methods
#   such as ``write`` or ``__iter__`` helpers), may be used by code RepoLens cannot see;
# - not a dunder, not decorated (routes, fixtures, properties, registrations), not ``main``,
#   no parse errors in its span;
# - nothing points at it, or at another definition with the same qualified name
#   (conditional definitions), through any dependency edge other than containment;
# - its name does not appear in the text of any unresolved, ambiguous or dynamic reference
#   (``obj._helper()`` on an object of unknown type, ``getattr`` strings, ...) nor in any
#   signature (classes used only in annotations);
# - not a method of a class that has, directly or through in-repository bases, a base that
#   did not resolve to a repository class (an external base may call it).
_DEAD_CODE = f"""
WITH RECURSIVE referenced AS (
    SELECT DISTINCT t.qualified_name
    FROM dependency_edges e JOIN symbols t ON t.id = e.dst_symbol_id
    WHERE e.analysis_run_id = :run AND e.edge_type <> 'CONTAINS'
),
loose_names AS (
    SELECT m[1] AS name
    FROM symbol_references r,
         regexp_matches(r.raw_text, '[A-Za-z_][A-Za-z0-9_]*', 'g') AS m
    WHERE r.analysis_run_id = :run
      AND r.resolution_status IN ('UNRESOLVED', 'AMBIGUOUS', 'DYNAMIC')
    UNION
    SELECT m[1]  -- parameter and return annotations (after the definition's own name)
    FROM symbols t,
         regexp_matches(substr(t.signature, strpos(t.signature, '(')),
                        '[A-Za-z_][A-Za-z0-9_]*', 'g') AS m
    WHERE t.analysis_run_id = :run AND strpos(t.signature, '(') > 0
),
open_classes AS (
    SELECT source_symbol_id AS id FROM symbol_references
    WHERE analysis_run_id = :run AND ref_kind = 'INHERIT' AND resolution_status <> 'RESOLVED'
    UNION
    SELECT e.src_symbol_id
    FROM dependency_edges e JOIN open_classes o ON o.id = e.dst_symbol_id
    WHERE e.analysis_run_id = :run AND e.edge_type = 'INHERITS'
)
SELECT {_ITEM_COLUMNS}, count(*) OVER () AS total
FROM symbols s JOIN source_files f ON f.id = s.file_id
WHERE s.analysis_run_id = :run AND s.kind IN :definitions
  AND NOT s.is_test AND f.role = 'SOURCE' AND f.path NOT LIKE '%.pyi'
  AND {_NOT_PUBLIC}
  AND (s.kind IN ('CLASS', 'FUNCTION', 'ASYNC_FUNCTION') OR s.visibility = 'PRIVATE')
  AND s.visibility <> 'DUNDER' AND s.decorators = '[]'::jsonb AND s.name <> 'main'
  AND NOT s.has_parse_errors
  AND s.qualified_name NOT IN (SELECT qualified_name FROM referenced)
  AND s.name NOT IN (SELECT name FROM loose_names)
  AND (s.parent_id IS NULL OR s.parent_id NOT IN (SELECT id FROM open_classes))
ORDER BY lines DESC, s.qualified_name, s.id
LIMIT :limit
"""  # noqa: S608 - constant SQL fragments; values are bound


def dead_code(session: Session, run_id: uuid.UUID, *, limit: int) -> tuple[int, list[SymbolItem]]:
    rows = _rows(session, _DEAD_CODE, run=run_id, definitions=list(DEFINITION_KINDS), limit=limit)
    total = int(rows[0][7]) if rows else 0
    return total, _items([(*r[:7], None) for r in rows])
