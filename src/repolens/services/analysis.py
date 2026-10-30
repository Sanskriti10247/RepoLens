"""Analysis requests and the worker-side ingestion pipeline.

Request (API process): validate the ref, create an analysis run and enqueue a job. The same
(repository, ref, engine, config) request while one is already queued or running returns the
existing run instead of creating a duplicate.

Execution (worker process), each step committed separately so progress is observable:
1. preflight with the provider (public, size limit) and check free disk space;
2. fetch the bare mirror and resolve the ref to a full commit SHA;
3. reuse or create the immutable snapshot (inventory written atomically with it);
4. reuse an equivalent completed analysis if one exists;
5. run the analyzer and persist its output together with the final status.

Throughout, a ``ProgressReporter`` records the current stage, files done and the file being
read in ``analysis_progress`` (see services/progress.py) for the web app's process view.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from repolens.config import Settings
from repolens.domain.enums import AnalysisStatus, JobKind, JobStatus, SnapshotStatus
from repolens.observability.logging import get_logger
from repolens.persistence import jobs as queue
from repolens.persistence.db import transaction
from repolens.persistence.models import (
    AnalysisRun,
    Job,
    Repository,
    RepositorySnapshot,
    SourceFile,
)
from repolens.repository.git import GitError, GitPolicy, GitRunner, GitTimeout
from repolens.repository.inventory import Inventory, InventoryTooLarge, build_inventory
from repolens.repository.mirror import (
    MirrorError,
    MirrorStore,
    ResolvedRevision,
    RevisionAmbiguous,
    RevisionNotFound,
)
from repolens.repository.providers import (
    GitHubProvider,
    LocalDirectoryProvider,
    ProviderError,
    RepositoryProvider,
)
from repolens.repository.validation import ValidationError, validate_ref
from repolens.services.errors import Conflict, InvalidInput, NotFound
from repolens.services.jobs import JobContext, PermanentJobError, RetryableJobError
from repolens.services.progress import NULL_PROGRESS, ProgressReporter, ProgressSink
from repolens.services.repositories import get_repository
from repolens.services.snapshots import SnapshotAnalyzer, SnapshotFile
from repolens.services.test_planning import ensure_test_catalog

log = get_logger(__name__)

ENGINE_VERSION = "1"
# Runs that own analysis results (REUSED runs point at one of these).
USABLE_STATUSES = (AnalysisStatus.SUCCEEDED.value, AnalysisStatus.PARTIAL.value)
MAX_STORED_ISSUES = 500
_TERMINAL_RUN = {
    AnalysisStatus.SUCCEEDED,
    AnalysisStatus.PARTIAL,
    AnalysisStatus.REUSED,
    AnalysisStatus.FAILED,
    AnalysisStatus.CANCELLED,
}


def default_analyzer(settings: Settings) -> SnapshotAnalyzer:
    """The analyzer used by workers; its version is part of every analysis key."""
    from repolens.services.code_analysis import PythonSnapshotAnalyzer  # noqa: PLC0415

    return PythonSnapshotAnalyzer(
        max_file_bytes=settings.max_indexed_file_bytes,
        parse_cache=settings.parse_cache_enabled,
    )


# -- configuration ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AnalysisConfig:
    max_indexed_file_bytes: int

    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True).encode()
        return hashlib.sha256(payload).hexdigest()[:16]


def analysis_config(settings: Settings) -> AnalysisConfig:
    return AnalysisConfig(max_indexed_file_bytes=settings.max_indexed_file_bytes)


@dataclass
class IngestionDeps:
    settings: Settings
    providers: dict[str, RepositoryProvider]
    runner: GitRunner
    mirrors: MirrorStore
    analyzer: SnapshotAnalyzer

    @property
    def provider_list(self) -> list[RepositoryProvider]:
        return list(self.providers.values())


def build_providers(settings: Settings) -> dict[str, RepositoryProvider]:
    providers: dict[str, RepositoryProvider] = {"github": GitHubProvider()}
    if settings.allow_local_repositories:
        providers["local"] = LocalDirectoryProvider(settings.local_repository_roots)
    return providers


def build_ingestion_deps(
    settings: Settings, analyzer: SnapshotAnalyzer | None = None
) -> IngestionDeps:
    providers = build_providers(settings)
    protocols = sorted({p for provider in providers.values() for p in provider.protocols})
    runner = GitRunner(
        settings.git_home_dir,
        GitPolicy(allowed_protocols=tuple(protocols), timeout_seconds=settings.git_timeout_seconds),
    )
    return IngestionDeps(
        settings=settings,
        providers=providers,
        runner=runner,
        mirrors=MirrorStore(settings.mirrors_dir, runner),
        analyzer=analyzer or default_analyzer(settings),
    )


# -- request side -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AnalysisRequestResult:
    run: AnalysisRun
    job_id: uuid.UUID
    created: bool


def request_analysis(
    session: Session,
    repository_id: uuid.UUID,
    ref: str,
    *,
    settings: Settings,
    parser_version: str,
) -> AnalysisRequestResult:
    repo = get_repository(session, repository_id)
    try:
        ref = validate_ref(ref)
    except ValidationError as exc:
        raise InvalidInput(str(exc), code="invalid_ref") from exc
    config = analysis_config(settings).fingerprint()
    run_id = uuid.uuid4()
    key = f"analyze:{repo.id}:{ref}:{ENGINE_VERSION}:{parser_version}:{config}"
    job_id, created = queue.enqueue(
        session,
        JobKind.ANALYZE_REPOSITORY,
        payload={"ref": ref},
        repository_id=repo.id,
        subject_id=run_id,
        idempotency_key=key,
    )
    if created:
        run = AnalysisRun(
            id=run_id,
            repository_id=repo.id,
            requested_ref=ref,
            status=AnalysisStatus.QUEUED,
            parser_version=parser_version,
            engine_version=ENGINE_VERSION,
            config_fingerprint=config,
        )
        session.add(run)
        session.flush()
        return AnalysisRequestResult(run=run, job_id=job_id, created=True)
    existing_subject = session.execute(select(Job.subject_id).where(Job.id == job_id)).scalar_one()
    existing = session.get(AnalysisRun, existing_subject)
    if existing is None:  # pragma: no cover - job and run are created together
        raise Conflict("Analysis job exists without its run")
    return AnalysisRequestResult(run=existing, job_id=job_id, created=False)


def cancel_analysis(session: Session, run_id: uuid.UUID) -> AnalysisRun:
    run = session.get(AnalysisRun, run_id)
    if run is None:
        raise NotFound("Analysis not found")
    job_id = latest_job_id(session, run_id)
    if job_id is not None:
        status = queue.request_cancel(session, job_id)
        if status is JobStatus.CANCELLED:
            _mark_run_terminal(session, run_id, AnalysisStatus.CANCELLED, "cancelled before start")
    session.refresh(run)
    return run


def latest_job_id(session: Session, run_id: uuid.UUID) -> uuid.UUID | None:
    return session.execute(
        select(Job.id).where(Job.subject_id == run_id).order_by(Job.created_at.desc()).limit(1)
    ).scalar_one_or_none()


def effective_run_id(session: Session, run_id: uuid.UUID) -> uuid.UUID:
    """Follow REUSED runs to the run that owns the results."""
    current = run_id
    for _ in range(4):
        reused = session.execute(
            select(AnalysisRun.reused_run_id).where(AnalysisRun.id == current)
        ).scalar_one_or_none()
        if reused is None:
            return current
        current = reused
    return current


def _mark_run_terminal(
    session: Session, run_id: uuid.UUID, status: AnalysisStatus, error: str | None
) -> None:
    session.execute(
        update(AnalysisRun)
        .where(
            AnalysisRun.id == run_id,
            AnalysisRun.status.in_([AnalysisStatus.QUEUED.value, AnalysisStatus.RUNNING.value]),
        )
        .values(status=status.value, error=error, finished_at=func.now())
    )


# -- worker side --------------------------------------------------------------------------


class AnalyzeRepositoryHandler:
    timeout: timedelta | None = timedelta(minutes=30)

    def __init__(self, deps: IngestionDeps) -> None:
        self.deps = deps
        self.pipeline = AnalysisPipeline(deps)

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
        run_status = (
            AnalysisStatus.CANCELLED if status is JobStatus.CANCELLED else AnalysisStatus.FAILED
        )
        message = f"{category}: {detail}" if detail else category
        _mark_run_terminal(session, job.subject_id, run_status, message[:2000])

    def run(self, ctx: JobContext) -> dict[str, Any]:
        timings: dict[str, float] = {}
        run_id = ctx.job.subject_id
        if run_id is None:
            raise PermanentJobError("Job has no analysis run", category="invalid_job")
        factory = ctx.session_factory

        with transaction(factory) as session:
            run = session.get(AnalysisRun, run_id, with_for_update=True)
            if run is None:
                raise PermanentJobError("Analysis run not found", category="invalid_job")
            if AnalysisStatus(run.status) in _TERMINAL_RUN:
                return {"skipped": f"run already {run.status}"}
            repo = session.get(Repository, run.repository_id)
            if repo is None:
                raise PermanentJobError("Repository not found", category="invalid_job")
            run.status = AnalysisStatus.RUNNING
            run.started_at = func.now()
            ref, repo_id, provider_name, canonical = (
                run.requested_ref,
                repo.id,
                repo.provider,
                repo.canonical_url,
            )

        progress = ProgressReporter(factory, run_id)
        progress.stage("fetch", current=canonical, detail=f"ref {ref}")
        with _timed(timings, "fetch_ms"):
            mirror, default_branch = self.pipeline.prepare_mirror(
                ctx, repo_id, provider_name, canonical
            )
            resolved = self.pipeline.resolve(mirror, ref, default_branch)
        ctx.checkpoint()
        progress.stage("snapshot", current=resolved.commit_sha)
        with _timed(timings, "snapshot_ms"):
            snapshot_id = self.pipeline.ensure_snapshot(
                ctx, repo_id, mirror, resolved, progress=progress
            )

        with transaction(factory) as session:
            run = session.get(AnalysisRun, run_id, with_for_update=True)
            assert run is not None  # noqa: S101 - locked above; runs are never deleted mid-job
            run.snapshot_id = snapshot_id
            run.resolved_ref = resolved.full_ref
            run.resolved_ref_kind = resolved.kind.value
            if default_branch:
                session.execute(
                    update(Repository)
                    .where(Repository.id == repo_id)
                    .values(default_branch=default_branch)
                )
            reusable = _find_reusable_run(session, run)
            if reusable is not None:
                _mark_reused(run, reusable)
        if reusable is not None:
            progress.finish(detail="results reused from an identical analysis of this commit")
            return {"reused_run_id": str(reusable), "commit_sha": resolved.commit_sha}

        ctx.checkpoint()
        return self.pipeline.analyze_run(
            ctx,
            run_id=run_id,
            snapshot_id=snapshot_id,
            mirror=mirror,
            resolved=resolved,
            timings=timings,
            progress=progress,
        )


class AnalysisPipeline:
    """Reusable ingestion and analysis steps, shared by analysis and change-analysis jobs."""

    def __init__(self, deps: IngestionDeps) -> None:
        self.deps = deps

    def _provider(self, name: str) -> RepositoryProvider:
        provider = self.deps.providers.get(name)
        if provider is None:
            raise PermanentJobError(
                f"Repository provider '{name}' is not enabled", category="provider_disabled"
            )
        return provider

    def prepare_mirror(
        self,
        ctx: JobContext,
        repo_id: uuid.UUID,
        provider_name: str,
        canonical_url: str,
    ) -> tuple[Path, str | None]:
        """Vet the repository with its provider, then create or update its bare mirror."""
        provider = self._provider(provider_name)
        settings = self.deps.settings
        locator = provider.locate(canonical_url)
        try:
            meta = provider.preflight(locator)
        except ProviderError as exc:
            error_type = RetryableJobError if exc.retryable else PermanentJobError
            raise error_type(str(exc), category="provider_error") from exc
        if meta.is_private:
            raise PermanentJobError(
                "Private repositories are not supported", category="repository_private"
            )
        max_bytes = settings.max_repository_size_mb * 1024 * 1024
        if meta.size_bytes is not None and meta.size_bytes > max_bytes:
            raise PermanentJobError(
                f"Repository is larger than {settings.max_repository_size_mb} MiB",
                category="repository_too_large",
            )
        _require_free_disk(settings, meta.size_bytes or 0)
        ctx.checkpoint()
        try:
            mirror = self.deps.mirrors.fetch(
                repo_id,
                provider.fetch_url(locator),
                max_bytes=max_bytes,
                timeout=settings.git_timeout_seconds,
            )
        except MirrorError as exc:
            error_type = RetryableJobError if exc.retryable else PermanentJobError
            raise error_type(str(exc), category="fetch_failed") from exc
        return mirror, meta.default_branch or self.deps.mirrors.default_branch(mirror)

    def resolve(self, mirror: Path, ref: str, default_branch: str | None) -> ResolvedRevision:
        try:
            return self.deps.mirrors.resolve(mirror, ref, default_branch=default_branch)
        except (RevisionNotFound, RevisionAmbiguous) as exc:
            raise PermanentJobError(str(exc), category="revision_not_resolved") from exc

    def ensure_snapshot(
        self,
        ctx: JobContext,
        repo_id: uuid.UUID,
        mirror: Path,
        resolved: ResolvedRevision,
        *,
        progress: ProgressSink = NULL_PROGRESS,
    ) -> uuid.UUID:
        factory = ctx.session_factory
        with transaction(factory) as session:
            existing = _snapshot_id(session, repo_id, resolved.commit_sha)
        if existing is not None:
            progress.advance(detail="snapshot already recorded")
            return existing

        settings = self.deps.settings
        try:
            info = self.deps.mirrors.commit_info(mirror, resolved.commit_sha)
            inventory = build_inventory(
                self.deps.runner,
                mirror,
                resolved.commit_sha,
                max_files=settings.max_file_count,
                max_hash_bytes=settings.max_hashed_file_bytes,
                checkpoint=ctx.checkpoint,
                on_file=lambda done, total, path: progress.advance(done, total=total, current=path),
            )
        except InventoryTooLarge as exc:
            raise PermanentJobError(str(exc), category="repository_too_large") from exc
        except GitTimeout as exc:
            raise RetryableJobError(str(exc), category="git_timeout") from exc
        except GitError as exc:
            raise PermanentJobError(str(exc), category="git_error") from exc

        snapshot_id = uuid.uuid4()
        with transaction(factory) as session:
            inserted = session.execute(
                pg_insert(RepositorySnapshot)
                .values(
                    id=snapshot_id,
                    repository_id=repo_id,
                    commit_sha=resolved.commit_sha,
                    tree_sha=info.tree_sha,
                    committed_at=info.committed_at,
                    status=SnapshotStatus.READY.value,
                    file_count=len(inventory.entries),
                    total_bytes=inventory.total_bytes,
                    language_inventory=inventory.language_counts(),
                    inventory_sha256=inventory.fingerprint(),
                    inventory_issues=_issues_document(inventory),
                    completed_at=func.now(),
                )
                .on_conflict_do_nothing(index_elements=["repository_id", "commit_sha"])
                .returning(RepositorySnapshot.id)
            ).scalar_one_or_none()
            if inserted is None:  # another worker created it concurrently
                winner = _snapshot_id(session, repo_id, resolved.commit_sha)
                assert winner is not None  # noqa: S101 - conflict implies the row exists
                return winner
            if inventory.entries:
                session.execute(
                    insert(SourceFile),
                    [
                        {
                            "snapshot_id": snapshot_id,
                            "path": e.path,
                            "blob_sha": e.blob_sha,
                            "content_sha256": e.content_sha256,
                            "size_bytes": e.size_bytes,
                            "mode": e.mode.value,
                            "language": e.language,
                            "role": e.role.value,
                            "is_binary": e.is_binary,
                        }
                        for e in inventory.entries
                    ],
                )
        log.info(
            "snapshot_created",
            snapshot_id=str(snapshot_id),
            commit_sha=resolved.commit_sha,
            files=len(inventory.entries),
        )
        return snapshot_id

    def analyze_run(
        self,
        ctx: JobContext,
        *,
        run_id: uuid.UUID,
        snapshot_id: uuid.UUID,
        mirror: Path,
        resolved: ResolvedRevision,
        timings: dict[str, float],
        progress: ProgressSink = NULL_PROGRESS,
    ) -> dict[str, Any]:
        factory = ctx.session_factory
        with transaction(factory) as session:
            files = load_snapshot_files(session, snapshot_id)
        # Written before the run row is locked below, so the progress row exists from here on.
        progress.stage("parse", total=len(files))
        try:
            with self.deps.runner.blob_reader(mirror) as reader, transaction(factory) as session:
                run = session.get(AnalysisRun, run_id, with_for_update=True)
                assert run is not None  # noqa: S101
                started = time.perf_counter()
                counts = self.deps.analyzer.analyze(
                    session, run, files, reader.read, ctx, progress=progress
                )
                session.flush()
                progress.stage("tests")
                tests_started = time.perf_counter()
                tests = ensure_test_catalog(session, run.id)
                if isinstance(counts.get("analysis_timings_ms"), dict):
                    counts["analysis_timings_ms"]["tests_ms"] = round(
                        (time.perf_counter() - tests_started) * 1000, 1
                    )
                timings["analyze_ms"] = round((time.perf_counter() - started) * 1000, 1)
                ctx.checkpoint()
                counts = {"files": len(files), **counts, "tests_discovered": len(tests)}
                run.status = (
                    AnalysisStatus.PARTIAL
                    if counts.get("incomplete_files", 0)
                    else AnalysisStatus.SUCCEEDED
                )
                run.counts = counts
                run.timings_ms = timings
                run.finished_at = func.now()
                session.flush()
        except IntegrityError:
            # An equivalent run completed first (unique result key); point at it instead.
            with transaction(factory) as session:
                run = session.get(AnalysisRun, run_id, with_for_update=True)
                assert run is not None  # noqa: S101
                reusable = _find_reusable_run(session, run)
                if reusable is None:
                    raise
                _mark_reused(run, reusable)
            progress.finish(detail="results reused from an identical analysis of this commit")
            return {"reused_run_id": str(reusable), "commit_sha": resolved.commit_sha}
        progress.finish()
        return {"commit_sha": resolved.commit_sha, "counts": counts, "timings_ms": timings}

    def ensure_analysis(
        self,
        ctx: JobContext,
        repo_id: uuid.UUID,
        mirror: Path,
        resolved: ResolvedRevision,
    ) -> uuid.UUID:
        """Return a run with usable results for this commit, analysing it if necessary."""
        snapshot_id = self.ensure_snapshot(ctx, repo_id, mirror, resolved)
        config = analysis_config(self.deps.settings).fingerprint()
        version = self.deps.analyzer.version
        with transaction(ctx.session_factory) as session:
            existing = session.execute(
                select(AnalysisRun.id)
                .where(
                    AnalysisRun.snapshot_id == snapshot_id,
                    AnalysisRun.parser_version == version,
                    AnalysisRun.engine_version == ENGINE_VERSION,
                    AnalysisRun.config_fingerprint == config,
                    AnalysisRun.status.in_(
                        [AnalysisStatus.SUCCEEDED.value, AnalysisStatus.PARTIAL.value]
                    ),
                )
                .limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            run = AnalysisRun(
                id=uuid.uuid4(),
                repository_id=repo_id,
                requested_ref=resolved.commit_sha,
                resolved_ref=resolved.full_ref,
                resolved_ref_kind=resolved.kind.value,
                snapshot_id=snapshot_id,
                status=AnalysisStatus.RUNNING,
                parser_version=version,
                engine_version=ENGINE_VERSION,
                config_fingerprint=config,
                started_at=func.now(),
            )
            session.add(run)
            run_id = run.id
        result = self.analyze_run(
            ctx,
            run_id=run_id,
            snapshot_id=snapshot_id,
            mirror=mirror,
            resolved=resolved,
            timings={},
            progress=ProgressReporter(ctx.session_factory, run_id),
        )
        reused = result.get("reused_run_id")
        return uuid.UUID(reused) if reused else run_id


def load_snapshot_files(session: Session, snapshot_id: uuid.UUID) -> list[SnapshotFile]:
    rows = session.execute(
        select(
            SourceFile.id,
            SourceFile.path,
            SourceFile.blob_sha,
            SourceFile.size_bytes,
            SourceFile.language,
            SourceFile.role,
            SourceFile.mode,
            SourceFile.is_binary,
        )
        .where(SourceFile.snapshot_id == snapshot_id)
        .order_by(SourceFile.path)
    ).all()
    return [SnapshotFile(*row) for row in rows]


def _snapshot_id(session: Session, repo_id: uuid.UUID, commit_sha: str) -> uuid.UUID | None:
    return session.execute(
        select(RepositorySnapshot.id).where(
            RepositorySnapshot.repository_id == repo_id,
            RepositorySnapshot.commit_sha == commit_sha,
            RepositorySnapshot.status == SnapshotStatus.READY.value,
        )
    ).scalar_one_or_none()


def _find_reusable_run(session: Session, run: AnalysisRun) -> uuid.UUID | None:
    return session.execute(
        select(AnalysisRun.id).where(
            AnalysisRun.snapshot_id == run.snapshot_id,
            AnalysisRun.parser_version == run.parser_version,
            AnalysisRun.engine_version == run.engine_version,
            AnalysisRun.config_fingerprint == run.config_fingerprint,
            AnalysisRun.status.in_([AnalysisStatus.SUCCEEDED.value, AnalysisStatus.PARTIAL.value]),
            AnalysisRun.id != run.id,
        )
    ).scalar_one_or_none()


def _mark_reused(run: AnalysisRun, source_id: uuid.UUID) -> None:
    run.status = AnalysisStatus.REUSED
    run.reused_run_id = source_id
    run.finished_at = func.now()


def _issues_document(inventory: Inventory) -> dict[str, Any]:
    counts = Counter(issue.code for issue in inventory.issues)
    return {
        "counts": dict(sorted(counts.items())),
        "truncated": len(inventory.issues) > MAX_STORED_ISSUES,
        "items": [
            {
                "code": i.code,
                "severity": i.severity.value,
                "message": i.message,
                "path": i.path,
            }
            for i in inventory.issues[:MAX_STORED_ISSUES]
        ],
    }


def _require_free_disk(settings: Settings, expected_bytes: int) -> None:
    settings.mirrors_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(settings.mirrors_dir).free
    needed = settings.min_free_disk_mb * 1024 * 1024 + 2 * expected_bytes
    if free < needed:
        raise RetryableJobError(
            f"Insufficient disk space: {free // 2**20} MiB free, {needed // 2**20} MiB required",
            category="insufficient_disk",
        )


class _timed:
    def __init__(self, timings: dict[str, float], key: str) -> None:
        self.timings = timings
        self.key = key

    def __enter__(self) -> None:
        self.started = time.perf_counter()

    def __exit__(self, *exc: object) -> None:
        self.timings[self.key] = round((time.perf_counter() - self.started) * 1000, 1)
