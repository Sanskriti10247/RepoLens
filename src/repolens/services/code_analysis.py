"""Python code analysis of a snapshot: parse every file, resolve names, persist the graph."""

from __future__ import annotations

import re
import time
from collections import Counter
from collections.abc import Callable
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from repolens.codegraph.modules import ModuleName, dedupe_modules, module_names
from repolens.codegraph.resolver import resolve_files
from repolens.domain.enums import FileAnalysisStatus, FileMode, Severity
from repolens.parsers.base import ParsedFile
from repolens.parsers.python import PythonAdapter, decode_python_source
from repolens.parsers.serialize import from_json, to_json
from repolens.persistence import analysis_writer as writer
from repolens.persistence.models import AnalysisRun, ParseCacheEntry
from repolens.repository.git import GitError
from repolens.services.jobs import JobContext
from repolens.services.progress import NULL_PROGRESS, ProgressSink
from repolens.services.snapshots import SnapshotFile

RESOLVER_REVISION = "1"
MAX_PROBLEMS_PER_FILE = 5
_INCOMPLETE = frozenset(
    {
        FileAnalysisStatus.PARSED_WITH_ERRORS,
        FileAnalysisStatus.SKIPPED_TOO_LARGE,
        FileAnalysisStatus.SKIPPED_ENCODING,
        FileAnalysisStatus.FAILED,
    }
)
ReadBlob = Callable[..., bytes]


def synthetic_module_name(path: str) -> str:
    """A unique, clearly non-importable module name for scripts and shadowed files."""
    cleaned = re.sub(r"[^0-9A-Za-z_]", "_", path.removesuffix(".py"))
    return f"__file__.{cleaned}"


class PythonSnapshotAnalyzer:
    def __init__(self, *, max_file_bytes: int, parse_cache: bool = True) -> None:
        self.adapter = PythonAdapter()
        self.max_file_bytes = max_file_bytes
        self.parse_cache = parse_cache
        self._new_cache_entries: list[tuple[SnapshotFile, str, ParsedFile]] = []
        self.version = f"{self.adapter.version}+resolver{RESOLVER_REVISION}"

    def analyze(
        self,
        session: Session,
        run: AnalysisRun,
        files: list[SnapshotFile],
        read_blob: ReadBlob,
        ctx: JobContext,
        progress: ProgressSink = NULL_PROGRESS,
    ) -> dict[str, Any]:
        timings: dict[str, float] = {}
        started = time.perf_counter()
        modules, shadowed = self._module_map(files)
        outcomes: list[writer.FileOutcome] = []
        warnings: list[writer.WarningRecord] = []
        parsed_files: list[ParsedFile] = []
        file_ids = {f.path: f.id for f in files}
        self._new_cache_entries = []
        cache = self._load_cache(session, files, modules)

        for index, file in enumerate(files):
            if index % 50 == 0:
                ctx.checkpoint()
            progress.advance(index, current=file.path)
            outcome, parsed = self._analyze_file(
                file, modules, shadowed, read_blob, warnings, cache
            )
            outcomes.append(outcome)
            if parsed is not None:
                parsed_files.append(parsed)
        timings["parse_ms"] = _ms(started)

        ctx.checkpoint()
        progress.stage(
            "resolve",
            detail=f"{len(parsed_files)} parsed files, {len(modules)} module names",
        )
        started = time.perf_counter()
        resolution = resolve_files(parsed_files, modules)
        timings["resolve_ms"] = _ms(started)

        started = time.perf_counter()
        symbol_pairs = [(file_ids[p.path], symbol) for p in parsed_files for symbol in p.symbols]
        progress.stage("persist", detail=f"writing {len(symbol_pairs)} symbols")
        step = time.perf_counter()
        symbol_ids = writer.write_symbols(session, run.id, symbol_pairs)
        timings["persist_symbols_ms"], step = _ms(step), time.perf_counter()
        progress.advance(detail="writing references")
        reference_ids = writer.write_references(session, run.id, file_ids, symbol_ids, resolution)
        timings["persist_references_ms"], step = _ms(step), time.perf_counter()
        progress.advance(detail="writing dependency edges")
        edge_counts = writer.write_edges(session, run.id, symbol_ids, resolution, reference_ids)
        timings["persist_edges_ms"], step = _ms(step), time.perf_counter()
        progress.advance(detail="writing file outcomes and warnings")
        writer.write_file_outcomes(session, run.id, outcomes)
        writer.write_warnings(session, run.id, warnings)
        timings["persist_files_warnings_ms"] = _ms(step)
        self._store_cache(session)
        timings["persist_ms"] = _ms(started)

        statuses = Counter(o.status.value for o in outcomes)
        python_outcomes = [o for o in outcomes if o.module_name is not None]
        return {
            "python_files": len(python_outcomes),
            "files_by_status": dict(sorted(statuses.items())),
            "incomplete_files": sum(1 for o in python_outcomes if o.status in _INCOMPLETE),
            "symbols": len(symbol_ids),
            "references": sum(1 for r in reference_ids if r is not None),
            "references_by_status": resolution.status_counts(),
            "dropped_name_references": resolution.dropped_name_references,
            "edges_by_type": edge_counts,
            "warnings": len(warnings),
            "parse_cache_hits": len(cache),
            "parse_cache_misses": len(self._new_cache_entries),
            "analysis_timings_ms": timings,
        }

    # -- helpers ---------------------------------------------------------------------------

    def _module_map(self, files: list[SnapshotFile]) -> tuple[dict[str, ModuleName], set[str]]:
        python = [f.path for f in files if self._is_python(f)]
        chosen, shadowed = dedupe_modules(module_names(python))
        for path in python:
            if path not in chosen and not path.endswith(".pyi"):
                # Scripts that are not importable, and duplicate module names, still contain
                # code that calls into the repository; give them unique synthetic names.
                chosen[path] = ModuleName(path, synthetic_module_name(path), False, "")
        return chosen, {p for p in shadowed if p.endswith(".pyi")}

    def _is_python(self, file: SnapshotFile) -> bool:
        return (
            file.language == "python"
            and file.mode in (FileMode.REGULAR.value, FileMode.EXECUTABLE.value)
            and not file.is_binary
            and self.adapter.handles(file.path)
        )

    def _analyze_file(
        self,
        file: SnapshotFile,
        modules: dict[str, ModuleName],
        shadowed_stubs: set[str],
        read_blob: ReadBlob,
        warnings: list[writer.WarningRecord],
        cache: dict[str, ParsedFile],
    ) -> tuple[writer.FileOutcome, ParsedFile | None]:
        skip = _skip_status(file)
        if skip is not None:
            return writer.FileOutcome(file.id, skip), None
        if not self._is_python(file) or file.path in shadowed_stubs or file.path not in modules:
            return writer.FileOutcome(file.id, FileAnalysisStatus.SKIPPED_UNSUPPORTED), None
        module = modules[file.path].name
        if file.size_bytes > self.max_file_bytes:
            warnings.append(
                writer.WarningRecord(
                    "FILE_TOO_LARGE",
                    Severity.WARNING,
                    f"Not parsed: {file.size_bytes} bytes exceeds {self.max_file_bytes}",
                    file.id,
                )
            )
            return writer.FileOutcome(file.id, FileAnalysisStatus.SKIPPED_TOO_LARGE, module), None
        parsed = cache.get(file.path)
        parse_ms: float | None = None
        if parsed is None:
            started = time.perf_counter()
            parsed, failure = self._parse(file, module, read_blob, warnings)
            if failure is not None:
                return failure, None
            assert parsed is not None  # noqa: S101 - _parse returns one or the other
            parse_ms = _ms(started)
            self._new_cache_entries.append((file, module, parsed))
        for problem in parsed.problems[:MAX_PROBLEMS_PER_FILE]:
            warnings.append(
                writer.WarningRecord(
                    "SYNTAX_ERROR",
                    Severity.WARNING,
                    problem.message,
                    file.id,
                    problem.span.start_line,
                    problem.span.last_line,
                )
            )
        status = FileAnalysisStatus.PARSED
        if parsed.has_errors:
            status = FileAnalysisStatus.PARSED_WITH_ERRORS
        outcome = writer.FileOutcome(
            file.id,
            status,
            module,
            symbol_count=len(parsed.symbols),
            error_count=len(parsed.problems),
            parse_ms=parse_ms,
        )
        return outcome, parsed

    def _parse(
        self,
        file: SnapshotFile,
        module: str,
        read_blob: ReadBlob,
        warnings: list[writer.WarningRecord],
    ) -> tuple[ParsedFile | None, writer.FileOutcome | None]:
        try:
            source = decode_python_source(read_blob(file.blob_sha, max_bytes=self.max_file_bytes))
        except GitError as exc:
            warnings.append(
                writer.WarningRecord("FILE_READ_FAILED", Severity.ERROR, str(exc)[:500], file.id)
            )
            return None, writer.FileOutcome(file.id, FileAnalysisStatus.FAILED, module)
        if source is None:
            warnings.append(
                writer.WarningRecord(
                    "FILE_ENCODING", Severity.WARNING, "Not valid UTF-8 or PEP 263 source", file.id
                )
            )
            return None, writer.FileOutcome(file.id, FileAnalysisStatus.SKIPPED_ENCODING, module)
        try:
            return self.adapter.parse(file.path, module, source), None
        except Exception as exc:  # noqa: BLE001 - one file's parser defect must not sink the run
            warnings.append(
                writer.WarningRecord(
                    "PARSE_FAILED", Severity.ERROR, f"{type(exc).__name__}: {exc}"[:500], file.id
                )
            )
            return None, writer.FileOutcome(file.id, FileAnalysisStatus.FAILED, module)

    # -- parse cache -----------------------------------------------------------------------

    def _load_cache(
        self, session: Session, files: list[SnapshotFile], modules: dict[str, ModuleName]
    ) -> dict[str, ParsedFile]:
        if not self.parse_cache:
            return {}
        keys = [(f.blob_sha, f.path, modules[f.path].name) for f in files if f.path in modules]
        found: dict[str, ParsedFile] = {}
        for start in range(0, len(keys), 1000):
            chunk = keys[start : start + 1000]
            rows = session.execute(
                select(ParseCacheEntry.path, ParseCacheEntry.payload).where(
                    ParseCacheEntry.parser_version == self.version,
                    tuple_(
                        ParseCacheEntry.blob_sha,
                        ParseCacheEntry.path,
                        ParseCacheEntry.module_name,
                    ).in_(chunk),
                )
            ).all()
            for path, payload in rows:
                try:
                    found[path] = from_json(payload)
                except (ValueError, KeyError, TypeError, IndexError):
                    continue  # unreadable entry: parse the file again
        return found

    def _store_cache(self, session: Session) -> None:
        if not self.parse_cache or not self._new_cache_entries:
            return
        rows = [
            {
                "blob_sha": file.blob_sha,
                "path": file.path,
                "module_name": module,
                "parser_version": self.version,
                "payload": to_json(parsed),
            }
            for file, module, parsed in self._new_cache_entries
        ]
        for start in range(0, len(rows), 200):
            session.execute(
                pg_insert(ParseCacheEntry)
                .values(rows[start : start + 200])
                .on_conflict_do_nothing()
            )


def _skip_status(file: SnapshotFile) -> FileAnalysisStatus | None:
    if file.mode == FileMode.SYMLINK.value:
        return FileAnalysisStatus.SKIPPED_SYMLINK
    if file.mode == FileMode.SUBMODULE.value:
        return FileAnalysisStatus.SKIPPED_UNSUPPORTED
    if file.is_binary:
        return FileAnalysisStatus.SKIPPED_BINARY
    return None


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)
