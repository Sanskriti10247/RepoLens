"""Python code analysis of a snapshot: parse every file, resolve names, persist the graph."""

from __future__ import annotations

import re
import time
from collections import Counter
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from repolens.codegraph.modules import ModuleName, dedupe_modules, module_names
from repolens.codegraph.resolver import resolve_files
from repolens.domain.enums import FileAnalysisStatus, FileMode, Severity
from repolens.parsers.base import ParsedFile
from repolens.parsers.python import PythonAdapter, decode_python_source
from repolens.persistence import analysis_writer as writer
from repolens.persistence.models import AnalysisRun
from repolens.repository.git import GitError
from repolens.services.jobs import JobContext
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
    def __init__(self, *, max_file_bytes: int) -> None:
        self.adapter = PythonAdapter()
        self.max_file_bytes = max_file_bytes
        self.version = f"{self.adapter.version}+resolver{RESOLVER_REVISION}"

    def analyze(
        self,
        session: Session,
        run: AnalysisRun,
        files: list[SnapshotFile],
        read_blob: ReadBlob,
        ctx: JobContext,
    ) -> dict[str, Any]:
        timings: dict[str, float] = {}
        started = time.perf_counter()
        modules, shadowed = self._module_map(files)
        outcomes: list[writer.FileOutcome] = []
        warnings: list[writer.WarningRecord] = []
        parsed_files: list[ParsedFile] = []
        file_ids = {f.path: f.id for f in files}

        for index, file in enumerate(files):
            if index % 50 == 0:
                ctx.checkpoint()
            outcome, parsed = self._analyze_file(file, modules, shadowed, read_blob, warnings)
            outcomes.append(outcome)
            if parsed is not None:
                parsed_files.append(parsed)
        timings["parse_ms"] = _ms(started)

        ctx.checkpoint()
        started = time.perf_counter()
        resolution = resolve_files(parsed_files, modules)
        timings["resolve_ms"] = _ms(started)

        started = time.perf_counter()
        symbol_pairs = [(file_ids[p.path], symbol) for p in parsed_files for symbol in p.symbols]
        symbol_ids = writer.write_symbols(session, run.id, symbol_pairs)
        writer.link_parents(session, symbol_ids, (s for _, s in symbol_pairs))
        reference_ids = writer.write_references(session, run.id, file_ids, symbol_ids, resolution)
        edge_counts = writer.write_edges(session, run.id, symbol_ids, resolution, reference_ids)
        writer.write_file_outcomes(session, run.id, outcomes)
        writer.write_warnings(session, run.id, warnings)
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
        try:
            source = decode_python_source(read_blob(file.blob_sha, max_bytes=self.max_file_bytes))
        except GitError as exc:
            warnings.append(
                writer.WarningRecord("FILE_READ_FAILED", Severity.ERROR, str(exc)[:500], file.id)
            )
            return writer.FileOutcome(file.id, FileAnalysisStatus.FAILED, module), None
        if source is None:
            warnings.append(
                writer.WarningRecord(
                    "FILE_ENCODING", Severity.WARNING, "Not valid UTF-8 or PEP 263 source", file.id
                )
            )
            return writer.FileOutcome(file.id, FileAnalysisStatus.SKIPPED_ENCODING, module), None
        started = time.perf_counter()
        try:
            parsed = self.adapter.parse(file.path, module, source)
        except Exception as exc:  # noqa: BLE001 - one file's parser defect must not sink the run
            warnings.append(
                writer.WarningRecord(
                    "PARSE_FAILED", Severity.ERROR, f"{type(exc).__name__}: {exc}"[:500], file.id
                )
            )
            return writer.FileOutcome(file.id, FileAnalysisStatus.FAILED, module), None
        parse_ms = _ms(started)
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
        status = (
            FileAnalysisStatus.PARSED_WITH_ERRORS
            if parsed.has_errors
            else FileAnalysisStatus.PARSED
        )
        outcome = writer.FileOutcome(
            file.id,
            status,
            module,
            symbol_count=len(parsed.symbols),
            error_count=len(parsed.problems),
            parse_ms=parse_ms,
        )
        return outcome, parsed


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
