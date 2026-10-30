"""Change analysis: compare two revisions and map the differences to symbols.

The job resolves both refs to commits, makes sure each commit has a usable analysis (reusing
existing results or analysing on demand), computes the file diff in the bare mirror, derives
changed line ranges, attributes them to symbols and stores the change set. Later stages
(impact propagation, test selection) build on the stored change set.
"""

from __future__ import annotations

import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.orm import Session

from repolens.domain.enums import (
    ChangeMappingStatus,
    ChangeSetStatus,
    FileAnalysisStatus,
    JobKind,
    JobStatus,
    SymbolKind,
)
from repolens.domain.spans import Span
from repolens.impact.changes import (
    FileDiff,
    LineChanges,
    SymbolChange,
    SymbolVersion,
    line_changes,
    map_symbol_changes,
    parse_raw_diff,
)
from repolens.parsers.treesitter import PARSED_LANGUAGES
from repolens.persistence import jobs as queue
from repolens.persistence.db import transaction
from repolens.persistence.models import (
    AnalysisFile,
    AnalysisRun,
    ChangedFile,
    ChangedSymbol,
    ChangeSet,
    Job,
    Repository,
    SourceFile,
    Symbol,
)
from repolens.repository.git import BlobReader, GitError, GitOutputLimitExceeded
from repolens.repository.mirror import ResolvedRevision, RevisionAmbiguous, RevisionNotFound
from repolens.repository.validation import ValidationError, is_full_sha, validate_ref
from repolens.services.analysis import AnalysisPipeline, IngestionDeps
from repolens.services.errors import InvalidInput, NotFound
from repolens.services.jobs import JobContext, PermanentJobError

DIFF_ALGORITHM = "git-raw-M50+difflib"
MAX_CHANGED_FILES = 5000
_SYMLINK_OR_SUBMODULE = {"120000", "160000"}
_TERMINAL = {ChangeSetStatus.SUCCEEDED, ChangeSetStatus.FAILED, ChangeSetStatus.CANCELLED}

# A follow-up stage (impact analysis) can be plugged in; it runs in the same job.
ChangeStage = Callable[[JobContext, uuid.UUID], dict[str, Any]]


# -- request side -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChangeRequestResult:
    change_set: ChangeSet
    job_id: uuid.UUID
    created: bool


def request_change_analysis(
    session: Session, repository_id: uuid.UUID, base_ref: str, target_ref: str
) -> ChangeRequestResult:
    if session.get(Repository, repository_id) is None:
        raise NotFound("Repository not found")
    try:
        base_ref, target_ref = validate_ref(base_ref), validate_ref(target_ref)
    except ValidationError as exc:
        raise InvalidInput(str(exc), code="invalid_ref") from exc
    change_id = uuid.uuid4()
    job_id, created = queue.enqueue(
        session,
        JobKind.ANALYZE_CHANGE,
        payload={"base_ref": base_ref, "target_ref": target_ref},
        repository_id=repository_id,
        subject_id=change_id,
        idempotency_key=f"change:{repository_id}:{base_ref}..{target_ref}",
    )
    if not created:
        subject = session.execute(select(Job.subject_id).where(Job.id == job_id)).scalar_one()
        existing = session.get(ChangeSet, subject)
        if existing is not None:
            return ChangeRequestResult(existing, job_id, False)
    change = ChangeSet(
        id=change_id,
        repository_id=repository_id,
        base_ref=base_ref,
        target_ref=target_ref,
        status=ChangeSetStatus.QUEUED,
    )
    session.add(change)
    session.flush()
    return ChangeRequestResult(change, job_id, True)


def cancel_change_analysis(session: Session, change_id: uuid.UUID) -> None:
    if session.get(ChangeSet, change_id) is None:
        raise NotFound("Impact analysis not found")
    job_id = session.execute(
        select(Job.id).where(Job.subject_id == change_id).order_by(Job.created_at.desc()).limit(1)
    ).scalar_one_or_none()
    if job_id is not None and queue.request_cancel(session, job_id) is JobStatus.CANCELLED:
        _mark_terminal(session, change_id, ChangeSetStatus.CANCELLED, "cancelled before start")


def _mark_terminal(
    session: Session, change_id: uuid.UUID, status: ChangeSetStatus, error: str | None
) -> None:
    session.execute(
        update(ChangeSet)
        .where(
            ChangeSet.id == change_id,
            ChangeSet.status.in_([ChangeSetStatus.QUEUED.value, ChangeSetStatus.RUNNING.value]),
        )
        .values(status=status.value, error=error, finished_at=func.now())
    )


# -- worker side --------------------------------------------------------------------------


class AnalyzeChangeHandler:
    timeout: timedelta | None = timedelta(minutes=45)

    def __init__(self, deps: IngestionDeps, stages: list[ChangeStage] | None = None) -> None:
        self.deps = deps
        self.pipeline = AnalysisPipeline(deps)
        self.stages = stages or []

    def on_terminal_failure(
        self,
        session: Session,
        job: queue.ClaimedJob | queue.RecoveredJob,
        status: JobStatus,
        category: str,
        detail: str | None,
    ) -> None:
        if job.subject_id is None:
            return
        final = (
            ChangeSetStatus.CANCELLED if status is JobStatus.CANCELLED else ChangeSetStatus.FAILED
        )
        message = f"{category}: {detail}" if detail else category
        _mark_terminal(session, job.subject_id, final, message[:2000])

    def run(self, ctx: JobContext) -> dict[str, Any]:
        change_id = ctx.job.subject_id
        if change_id is None:
            raise PermanentJobError("Job has no change set", category="invalid_job")
        timings: dict[str, float] = {}
        with transaction(ctx.session_factory) as session:
            change = session.get(ChangeSet, change_id, with_for_update=True)
            if change is None:
                raise PermanentJobError("Change set not found", category="invalid_job")
            if ChangeSetStatus(change.status) in _TERMINAL:
                return {"skipped": f"change set already {change.status}"}
            repo = session.get(Repository, change.repository_id)
            if repo is None:
                raise PermanentJobError("Repository not found", category="invalid_job")
            change.status = ChangeSetStatus.RUNNING
            change.started_at = func.now()
            repo_id, provider, url = repo.id, repo.provider, repo.canonical_url
            base_ref, target_ref = change.base_ref, change.target_ref

        started = time.perf_counter()
        mirror, base, target, fetched = self._revisions(
            ctx, repo_id, provider, url, base_ref, target_ref
        )
        timings["fetch_ms"] = _ms(started)
        if not fetched:
            timings["fetch_skipped"] = 1

        started = time.perf_counter()
        base_run = self.pipeline.ensure_analysis(ctx, repo_id, mirror, base)
        ctx.checkpoint()
        target_run = self.pipeline.ensure_analysis(ctx, repo_id, mirror, target)
        timings["analysis_ms"] = _ms(started)
        ctx.checkpoint()

        started = time.perf_counter()
        with transaction(ctx.session_factory) as session:
            change = session.get(ChangeSet, change_id, with_for_update=True)
            assert change is not None  # noqa: S101 - locked above
            change.base_sha, change.target_sha = base.commit_sha, target.commit_sha
            change.base_run_id, change.target_run_id = base_run, target_run
            change.diff_algorithm = DIFF_ALGORITHM
            summary = compute_and_store_changes(
                session,
                self.deps,
                mirror,
                change,
                max_file_bytes=self.deps.settings.max_indexed_file_bytes,
            )
            change.summary = summary  # later stages read it (e.g. "too_large")
        timings["diff_ms"] = _ms(started)

        for stage in self.stages:
            ctx.checkpoint()
            started = time.perf_counter()
            summary.update(stage(ctx, change_id))
            timings[f"{getattr(stage, '__name__', 'stage')}_ms"] = _ms(started)

        ctx.checkpoint()
        with transaction(ctx.session_factory) as session:
            change = session.get(ChangeSet, change_id, with_for_update=True)
            assert change is not None  # noqa: S101
            change.summary = summary
            change.timings_ms = timings
            change.status = ChangeSetStatus.SUCCEEDED
            change.finished_at = func.now()
        return {"base_sha": base.commit_sha, "target_sha": target.commit_sha, **summary}

    def _revisions(
        self,
        ctx: JobContext,
        repo_id: uuid.UUID,
        provider: str,
        url: str,
        base_ref: str,
        target_ref: str,
    ) -> tuple[Path, ResolvedRevision, ResolvedRevision, bool]:
        """Resolve both refs, fetching first unless both are commit ids already mirrored.

        Commits are immutable, so fetching again cannot change what a full SHA names.
        """
        if is_full_sha(base_ref) and is_full_sha(target_ref):
            mirrors = self.deps.mirrors
            with mirrors.locked(repo_id) as mirror:
                if (mirror / "HEAD").exists():
                    try:
                        base = mirrors.resolve(mirror, base_ref, default_branch=None)
                        target = mirrors.resolve(mirror, target_ref, default_branch=None)
                    except (RevisionNotFound, RevisionAmbiguous):
                        pass
                    else:
                        return mirror, base, target, False
        mirror, default_branch = self.pipeline.prepare_mirror(ctx, repo_id, provider, url)
        base = self.pipeline.resolve(mirror, base_ref, default_branch)
        target = self.pipeline.resolve(mirror, target_ref, default_branch)
        return mirror, base, target, True


# -- diff and mapping -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FileInfo:
    id: int
    path: str
    blob_sha: str
    size_bytes: int
    language: str | None
    role: str
    is_binary: bool
    status: str | None  # analysis status in the corresponding run
    module: str | None


def compute_and_store_changes(
    session: Session,
    deps: IngestionDeps,
    mirror: Path,
    change: ChangeSet,
    *,
    max_file_bytes: int,
) -> dict[str, Any]:
    assert change.base_sha and change.target_sha  # noqa: S101 - set by the caller
    assert change.base_run_id and change.target_run_id  # noqa: S101
    raw = deps.runner.run(
        [
            "diff",
            "--raw",
            "-z",
            "--no-abbrev",
            "-M50%",
            "--no-ext-diff",
            "--no-textconv",
            "--end-of-options",
            change.base_sha,
            change.target_sha,
        ],
        git_dir=mirror,
    )
    diffs = parse_raw_diff(raw)
    if len(diffs) > MAX_CHANGED_FILES:
        return {
            "too_large": True,
            "changed_files": len(diffs),
            "message": f"More than {MAX_CHANGED_FILES} changed files; symbol mapping skipped",
        }
    base_files = _files(session, change.base_run_id, [d.old_path for d in diffs if d.old_path])
    target_files = _files(session, change.target_run_id, [d.new_path for d in diffs if d.new_path])
    base_symbols = _symbols(session, change.base_run_id, [f.id for f in base_files.values()])
    target_symbols = _symbols(session, change.target_run_id, [f.id for f in target_files.values()])

    file_rows: list[dict[str, Any]] = []
    symbol_changes: list[list[SymbolChange]] = []
    with deps.runner.blob_reader(mirror) as reader:
        for diff in diffs:
            old = base_files.get(diff.old_path) if diff.old_path else None
            new = target_files.get(diff.new_path) if diff.new_path else None
            status = _mapping_status(diff, old, new)
            lines = _line_changes(
                reader, diff, old=old, new=new, status=status, max_bytes=max_file_bytes
            )
            changes: list[SymbolChange] = []
            if (
                status
                in (
                    ChangeMappingStatus.MAPPED,
                    ChangeMappingStatus.MAPPED_WITH_PARSE_ERRORS,
                )
                and lines is not None
            ):
                changes = map_symbol_changes(
                    base_symbols.get(old.id, []) if old else [],
                    target_symbols.get(new.id, []) if new else [],
                    lines,
                    base_module=old.module if old else None,
                    target_module=new.module if new else None,
                )
            reference = new or old
            file_rows.append(
                {
                    "change_set_id": change.id,
                    "change_type": diff.change_type.value,
                    "old_path": diff.old_path,
                    "new_path": diff.new_path,
                    "old_file_id": old.id if old else None,
                    "new_file_id": new.id if new else None,
                    "language": reference.language if reference else None,
                    "role": reference.role if reference else None,
                    "mapping_status": status.value,
                    "similarity": diff.similarity,
                    "added_lines": lines.added if lines else 0,
                    "deleted_lines": lines.deleted if lines else 0,
                    "old_ranges": [list(r) for r in lines.old_ranges] if lines else [],
                    "new_ranges": [list(r) for r in lines.new_ranges] if lines else [],
                }
            )
            symbol_changes.append(changes)

    file_ids: list[int] = []
    if file_rows:
        file_ids = list(
            session.execute(
                insert(ChangedFile).returning(ChangedFile.id, sort_by_parameter_order=True),
                file_rows,
            ).scalars()
        )
    symbol_rows = [
        {
            "change_set_id": change.id,
            "changed_file_id": file_id,
            "change_kind": c.kind.value,
            "symbol_kind": c.symbol_kind.value,
            "qualified_name": c.qualified_name,
            "base_symbol_id": c.base.id if c.base else None,
            "target_symbol_id": c.target.id if c.target else None,
            "signature_changed": c.signature_changed,
            "body_changed": c.body_changed,
            "decorators_changed": c.decorators_changed,
            "bases_changed": c.bases_changed,
            "changed_lines": c.changed_lines,
        }
        for file_id, changes in zip(file_ids, symbol_changes, strict=True)
        for c in changes
    ]
    if symbol_rows:
        session.execute(insert(ChangedSymbol), symbol_rows)
    return {
        "changed_files": len(file_rows),
        "files_by_change_type": dict(sorted(Counter(r["change_type"] for r in file_rows).items())),
        "files_by_mapping_status": dict(
            sorted(Counter(r["mapping_status"] for r in file_rows).items())
        ),
        "changed_symbols": len(symbol_rows),
        "symbols_by_change_kind": dict(
            sorted(Counter(r["change_kind"] for r in symbol_rows).items())
        ),
        "added_lines": sum(r["added_lines"] for r in file_rows),
        "deleted_lines": sum(r["deleted_lines"] for r in file_rows),
    }


def _mapping_status(
    diff: FileDiff, old: _FileInfo | None, new: _FileInfo | None
) -> ChangeMappingStatus:
    if (diff.old_mode in _SYMLINK_OR_SUBMODULE) or (diff.new_mode in _SYMLINK_OR_SUBMODULE):
        return ChangeMappingStatus.SPECIAL
    sides = [f for f in (old, new) if f is not None]
    if any(f.is_binary for f in sides):
        return ChangeMappingStatus.BINARY
    if not sides or all(f.language not in PARSED_LANGUAGES for f in sides):
        return ChangeMappingStatus.NON_PYTHON
    statuses = {f.status for f in sides}
    parsed = {FileAnalysisStatus.PARSED.value, FileAnalysisStatus.PARSED_WITH_ERRORS.value}
    if not statuses <= parsed:
        return ChangeMappingStatus.NOT_ANALYZED
    if FileAnalysisStatus.PARSED_WITH_ERRORS.value in statuses:
        return ChangeMappingStatus.MAPPED_WITH_PARSE_ERRORS
    return ChangeMappingStatus.MAPPED


def _line_changes(
    reader: BlobReader,
    diff: FileDiff,
    *,
    old: _FileInfo | None,
    new: _FileInfo | None,
    status: ChangeMappingStatus,
    max_bytes: int,
) -> LineChanges | None:
    if status in (ChangeMappingStatus.BINARY, ChangeMappingStatus.SPECIAL):
        return None

    def text(blob: str | None) -> str | None:
        if blob is None:
            return None
        try:
            return reader.read(blob, max_bytes=max_bytes).decode("utf-8", errors="replace")
        except (GitOutputLimitExceeded, GitError):
            return None

    old_text, new_text = text(diff.old_blob), text(diff.new_blob)
    if (diff.old_blob and old_text is None) or (diff.new_blob and new_text is None):
        return None  # too large or unreadable: the change stays unmapped at line level
    return line_changes(old_text, new_text)


def _files(session: Session, run_id: uuid.UUID, paths: list[str]) -> dict[str, _FileInfo]:
    if not paths:
        return {}
    snapshot_id = session.execute(
        select(AnalysisRun.snapshot_id).where(AnalysisRun.id == run_id)
    ).scalar_one()
    rows = session.execute(
        select(
            SourceFile.id,
            SourceFile.path,
            SourceFile.blob_sha,
            SourceFile.size_bytes,
            SourceFile.language,
            SourceFile.role,
            SourceFile.is_binary,
            AnalysisFile.status,
            AnalysisFile.module_name,
        )
        .outerjoin(
            AnalysisFile,
            (AnalysisFile.file_id == SourceFile.id) & (AnalysisFile.analysis_run_id == run_id),
        )
        .where(SourceFile.snapshot_id == snapshot_id, SourceFile.path.in_(paths))
    ).all()
    return {r.path: _FileInfo(*r) for r in rows}


def _symbols(
    session: Session, run_id: uuid.UUID, file_ids: list[int]
) -> dict[int, list[SymbolVersion]]:
    grouped: dict[int, list[SymbolVersion]] = defaultdict(list)
    if not file_ids:
        return grouped
    rows = session.execute(
        select(
            Symbol.id,
            Symbol.file_id,
            Symbol.symbol_key,
            Symbol.qualified_name,
            Symbol.name,
            Symbol.kind,
            Symbol.start_line,
            Symbol.start_col,
            Symbol.end_line,
            Symbol.end_col,
            Symbol.signature,
            Symbol.body_fingerprint,
            Symbol.decorators,
            Symbol.parent_id,
        ).where(Symbol.analysis_run_id == run_id, Symbol.file_id.in_(file_ids))
    ).all()
    for r in rows:
        grouped[r.file_id].append(
            SymbolVersion(
                id=r.id,
                key=r.symbol_key,
                qualified_name=r.qualified_name,
                name=r.name,
                kind=SymbolKind(r.kind),
                span=Span(r.start_line, r.start_col, r.end_line, r.end_col),
                signature=r.signature,
                body_fingerprint=r.body_fingerprint,
                decorators=tuple(r.decorators or ()),
                parent_id=r.parent_id,
            )
        )
    return grouped


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)
