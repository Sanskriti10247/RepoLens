"""Sandboxed test execution: request a run, execute it in the worker, record the evidence."""

from __future__ import annotations

import json
import shutil
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.orm import Session

from repolens.config import Settings
from repolens.domain.enums import JobKind, JobStatus
from repolens.execution.docker import DockerSandbox, SandboxRequest, SandboxUnavailable
from repolens.execution.outcomes import Classification, RunOutcome, classify, node_id_key
from repolens.execution.policy import ExecutionPolicy, PolicyViolation, SandboxLimits
from repolens.persistence import jobs as queue
from repolens.persistence.db import transaction
from repolens.persistence.models import (
    AnalysisRun,
    CoverageRecord,
    Job,
    Repository,
    RepositorySnapshot,
    TestCaseRecord,
    TestPlan,
    TestPlanItem,
    TestResultRecord,
    TestRun,
)
from repolens.repository.git import GitError
from repolens.repository.materialize import MaterializeError, materialize
from repolens.services.analysis import (
    USABLE_STATUSES,
    AnalysisPipeline,
    IngestionDeps,
    effective_run_id,
)
from repolens.services.errors import InvalidInput, NotFound
from repolens.services.jobs import (
    JobContext,
    PermanentJobError,
    PolicyBlockedError,
    RetryableJobError,
)

MAX_STORED_TAIL = 64 * 1024
MAX_COVERAGE_ROWS = 50_000


def sandbox_limits(settings: Settings) -> SandboxLimits:
    return SandboxLimits(
        memory_mb=settings.sandbox_memory_mb,
        cpus=settings.sandbox_cpus,
        pids=settings.sandbox_pids,
        timeout_seconds=settings.sandbox_timeout_seconds,
        workspace_mb=settings.sandbox_workspace_mb,
    )


def execution_policy(settings: Settings) -> ExecutionPolicy:
    return ExecutionPolicy(
        approved_images=frozenset({settings.sandbox_image}), limits=sandbox_limits(settings)
    )


# -- request side -------------------------------------------------------------------------


def request_test_run(
    session: Session,
    settings: Settings,
    *,
    plan_id: uuid.UUID | None = None,
    analysis_id: uuid.UUID | None = None,
    full_suite: bool = False,
    coverage: bool = True,
) -> tuple[TestRun, uuid.UUID]:
    """Queue a run of a test plan's selection, or of a whole suite for an analysis."""
    if (plan_id is None) == (analysis_id is None):
        raise InvalidInput("Give exactly one of plan_id or analysis_id", code="invalid_request")
    node_ids: list[str] = []
    if plan_id is not None:
        plan = session.get(TestPlan, plan_id)
        if plan is None:
            raise NotFound("Test plan not found")
        run_id = plan.analysis_run_id
        if plan.scope == "FULL_SUITE" or full_suite:
            full_suite = True
        else:
            node_ids = list(
                session.execute(
                    select(TestPlanItem.node_id)
                    .where(TestPlanItem.plan_id == plan_id, TestPlanItem.selected.is_(True))
                    .order_by(TestPlanItem.rank)
                ).scalars()
            )
            if not node_ids:
                raise InvalidInput("The plan selects no tests", code="empty_plan")
    else:
        assert analysis_id is not None  # noqa: S101 - checked above
        run_id = analysis_id
        if not full_suite:
            raise InvalidInput("Without a plan only full-suite runs are possible", code="no_plan")
    if session.get(AnalysisRun, run_id) is None:
        raise NotFound("Analysis not found")
    analysis = session.get(AnalysisRun, effective_run_id(session, run_id))
    if analysis is None or analysis.snapshot_id is None or analysis.status not in USABLE_STATUSES:
        raise InvalidInput("The analysis has no usable results", code="analysis_not_ready")
    snapshot = session.get(RepositorySnapshot, analysis.snapshot_id)
    assert snapshot is not None  # noqa: S101 - FK
    try:
        execution_policy(settings).check_node_ids(node_ids)
    except PolicyViolation as exc:
        raise InvalidInput(str(exc), code="policy_violation") from exc
    test_run = TestRun(
        id=uuid.uuid4(),
        repository_id=analysis.repository_id,
        analysis_run_id=analysis.id,
        snapshot_id=snapshot.id,
        commit_sha=snapshot.commit_sha,
        plan_id=plan_id,
        mode="FULL_SUITE" if full_suite else "SELECTED",
        requested_node_ids=node_ids,
        coverage=coverage,
        status="QUEUED",
        image=settings.sandbox_image,
        limits=_limits_json(sandbox_limits(settings)),
    )
    session.add(test_run)
    session.flush()
    job_id, _ = queue.enqueue(
        session,
        JobKind.RUN_TESTS,
        repository_id=analysis.repository_id,
        subject_id=test_run.id,
        max_attempts=2,
    )
    return test_run, job_id


def request_collection(
    session: Session, settings: Settings, analysis_id: uuid.UUID
) -> tuple[TestRun, uuid.UUID]:
    """Queue sandboxed ``pytest --collect-only`` (collection imports repository code)."""
    if session.get(AnalysisRun, analysis_id) is None:
        raise NotFound("Analysis not found")
    analysis = session.get(AnalysisRun, effective_run_id(session, analysis_id))
    if analysis is None or analysis.snapshot_id is None or analysis.status not in USABLE_STATUSES:
        raise InvalidInput("The analysis has no usable results", code="analysis_not_ready")
    snapshot = session.get(RepositorySnapshot, analysis.snapshot_id)
    assert snapshot is not None  # noqa: S101 - FK
    test_run = TestRun(
        id=uuid.uuid4(),
        repository_id=analysis.repository_id,
        analysis_run_id=analysis.id,
        snapshot_id=snapshot.id,
        commit_sha=snapshot.commit_sha,
        mode="COLLECT",
        requested_node_ids=[],
        coverage=False,
        status="QUEUED",
        image=settings.sandbox_image,
        limits=_limits_json(sandbox_limits(settings)),
    )
    session.add(test_run)
    session.flush()
    job_id, _ = queue.enqueue(
        session,
        JobKind.RUN_TESTS,
        repository_id=analysis.repository_id,
        subject_id=test_run.id,
        max_attempts=2,
    )
    return test_run, job_id


def cancel_test_run(session: Session, run_id: uuid.UUID) -> None:
    if session.get(TestRun, run_id) is None:
        raise NotFound("Test run not found")
    job_id = session.execute(
        select(Job.id).where(Job.subject_id == run_id).order_by(Job.created_at.desc()).limit(1)
    ).scalar_one_or_none()
    if job_id is not None and queue.request_cancel(session, job_id) is JobStatus.CANCELLED:
        _complete_if_open(session, run_id, RunOutcome.CANCELLED, "cancelled before start")


def _complete_if_open(
    session: Session, run_id: uuid.UUID, outcome: RunOutcome, reason: str
) -> None:
    session.execute(
        update(TestRun)
        .where(TestRun.id == run_id, TestRun.status != "COMPLETED")
        .values(
            status="COMPLETED",
            outcome=outcome.value,
            outcome_reason=reason[:2000],
            finished_at=func.now(),
        )
    )


# -- worker side --------------------------------------------------------------------------


class RunTestsHandler:
    def __init__(self, deps: IngestionDeps, sandbox: DockerSandbox | None = None) -> None:
        self.deps = deps
        self.settings = deps.settings
        self.pipeline = AnalysisPipeline(deps)
        self.sandbox = sandbox or DockerSandbox(self.settings.docker_binary)
        self.policy = execution_policy(self.settings)
        # The job may outlive the sandbox timeout by the time needed to prepare and record.
        self.timeout: timedelta | None = timedelta(
            seconds=self.settings.sandbox_timeout_seconds + 600
        )

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
        outcome = {
            JobStatus.CANCELLED: RunOutcome.CANCELLED,
            JobStatus.BLOCKED: RunOutcome.SECURITY_POLICY_BLOCKED,
            JobStatus.TIMED_OUT: RunOutcome.TIMED_OUT,
        }.get(status, RunOutcome.INFRASTRUCTURE_ERROR)
        reason = f"{category}: {detail}" if detail else category
        _complete_if_open(session, job.subject_id, outcome, reason)

    def run(self, ctx: JobContext) -> dict[str, Any]:
        run_id = ctx.job.subject_id
        if run_id is None:
            raise PermanentJobError("Job has no test run", category="invalid_job")
        with transaction(ctx.session_factory) as session:
            test_run = session.get(TestRun, run_id, with_for_update=True)
            if test_run is None:
                raise PermanentJobError("Test run not found", category="invalid_job")
            if test_run.status == "COMPLETED":
                return {"skipped": "already completed"}
            repo = session.get(Repository, test_run.repository_id)
            assert repo is not None  # noqa: S101 - FK
            test_run.status = "RUNNING"
            test_run.started_at = func.now()
            spec = {
                "repo_id": repo.id,
                "provider": repo.provider,
                "url": repo.canonical_url,
                "commit": test_run.commit_sha,
                "node_ids": list(test_run.requested_node_ids),
                "coverage": test_run.coverage,
                "image": test_run.image,
                "analysis_run_id": test_run.analysis_run_id,
                "mode": test_run.mode,
            }

        self._check_policy(spec)
        workspace = self.settings.workspaces_dir / str(run_id)
        try:
            source = self._prepare_workspace(ctx, spec, workspace)
            ctx.checkpoint()
            result = self._execute(ctx, run_id, spec, source)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
        classification = classify(result)
        ctx.checkpoint()  # never record results for a job this worker no longer owns
        with transaction(ctx.session_factory) as session:
            self._record(session, run_id, spec, result, classification)
        return {
            "outcome": classification.outcome.value,
            "counts": classification.counts,
            "duration_seconds": result.duration_seconds,
        }

    # -- steps ----------------------------------------------------------------------------

    def _check_policy(self, spec: dict[str, Any]) -> None:
        try:
            self.policy.check_image(spec["image"])
            self.policy.check_limits(self.policy.limits)
            self.policy.check_node_ids(spec["node_ids"])
        except PolicyViolation as exc:
            raise PolicyBlockedError(str(exc)) from exc

    def _prepare_workspace(self, ctx: JobContext, spec: dict[str, Any], workspace: Path) -> Path:
        shutil.rmtree(workspace, ignore_errors=True)
        workspace.mkdir(parents=True, mode=0o700)
        mirror = self.deps.mirrors.path_for(spec["repo_id"])
        if not self._has_commit(mirror, spec["commit"]):
            mirror, _ = self.pipeline.prepare_mirror(
                ctx, spec["repo_id"], spec["provider"], spec["url"]
            )
            if not self._has_commit(mirror, spec["commit"]):
                raise PermanentJobError("Commit is no longer available", category="commit_missing")
        source = workspace / "src"
        try:
            materialize(
                self.deps.runner,
                mirror,
                spec["commit"],
                source,
                max_bytes=self.settings.max_repository_size_mb * 1024 * 1024,
                max_files=self.settings.max_file_count,
            )
        except MaterializeError as exc:
            raise PermanentJobError(str(exc), category="workspace_too_large") from exc
        except GitError as exc:
            raise RetryableJobError(str(exc), category="git_error") from exc
        return source

    def _has_commit(self, mirror: Path, commit: str) -> bool:
        if not (mirror / "HEAD").exists():
            return False
        try:
            self.deps.runner.run(
                ["cat-file", "-e", "--end-of-options", f"{commit}^{{commit}}"], git_dir=mirror
            )
        except GitError:
            return False
        return True

    def _execute(
        self, ctx: JobContext, run_id: uuid.UUID, spec: dict[str, Any], source: Path
    ) -> Any:
        arguments = ["collect"] if spec["mode"] == "COLLECT" else ["run"]
        if spec["coverage"] and spec["mode"] != "COLLECT":
            arguments.append("--coverage")
        if spec["node_ids"]:
            arguments += ["--", *spec["node_ids"]]

        def should_stop() -> str | None:
            if ctx.lost_event.is_set():
                return "lease_lost"
            if ctx.cancel_event.is_set():
                return "cancelled"
            return None

        name = f"repolens-run-{run_id}"
        # A previous attempt may have left its container behind (worker crash, lost lease).
        self.sandbox.remove(name)
        try:
            return self.sandbox.run(
                SandboxRequest(
                    name=name,
                    image=spec["image"],
                    source_dir=source,
                    arguments=arguments,
                    limits=self.policy.limits,
                    labels={"repolens.run": str(run_id), "repolens.job": str(ctx.job.id)},
                ),
                should_stop,
            )
        except SandboxUnavailable as exc:
            raise RetryableJobError(str(exc), category="sandbox_unavailable") from exc

    def _record(
        self,
        session: Session,
        run_id: uuid.UUID,
        spec: dict[str, Any],
        result: Any,
        classification: Classification,
    ) -> None:
        catalog = {
            node_id_key(node_id): node_id
            for node_id in session.execute(
                select(TestCaseRecord.node_id).where(
                    TestCaseRecord.analysis_run_id == spec["analysis_run_id"]
                )
            ).scalars()
        }
        rows = []
        for item in classification.results:
            base_name = item.name.split("[", 1)[0]
            node_id = catalog.get((item.classname, base_name))
            suffix = item.name[len(base_name) :]
            node_id = f"{node_id}{suffix}" if node_id else _synthesise(item.classname, item.name)
            rows.append(
                {
                    "test_run_id": run_id,
                    "node_id": node_id,
                    "outcome": item.outcome.value,
                    "duration_ms": round(item.duration_seconds * 1000, 2),
                    "message": item.message,
                }
            )
        if rows:
            session.execute(insert(TestResultRecord), rows)
        collected: list[str] | None = None
        if spec["mode"] == "COLLECT" and classification.outcome is RunOutcome.PASSED:
            collected = _record_collection(session, spec["analysis_run_id"], result.envelope)
        coverage_rows = _coverage_rows(run_id, result.envelope, session, spec["analysis_run_id"])
        if coverage_rows:
            session.execute(insert(CoverageRecord), coverage_rows)
        envelope = result.envelope or {}
        session.execute(
            update(TestRun)
            .where(TestRun.id == run_id)
            .values(
                status="COMPLETED",
                outcome=classification.outcome.value,
                outcome_reason=classification.reason[:2000],
                pytest_exit_code=classification.pytest_exit_code,
                container_exit_code=result.container_exit_code,
                counts=classification.counts or None,
                duration_ms=round(result.duration_seconds * 1000, 1),
                stdout_tail=str(envelope.get("stdout_tail") or "")[-MAX_STORED_TAIL:] or None,
                stderr_tail=(
                    str(envelope.get("stderr_tail") or result.stderr_tail or "")[-MAX_STORED_TAIL:]
                    or None
                ),
                output_truncated=result.output_truncated,
                cleanup_ok=result.cleanup_ok,
                collected_node_ids=collected,
                finished_at=func.now(),
            )
        )


def _record_collection(
    session: Session, analysis_run_id: uuid.UUID, envelope: dict[str, Any] | None
) -> list[str]:
    """Reconcile the static catalog with what pytest actually collected."""
    raw = (envelope or {}).get("collected") or []
    collected = sorted({str(n) for n in raw if isinstance(n, str) and "::" in n})[:50_000]
    bases = {n.split("[", 1)[0] for n in collected}
    cases = list(
        session.execute(
            select(TestCaseRecord).where(TestCaseRecord.analysis_run_id == analysis_run_id)
        ).scalars()
    )
    known = {case.node_id for case in cases}
    for case in cases:
        case.collection_status = "COLLECTED" if case.node_id in bases else "NOT_COLLECTED"
    for node_id in sorted(bases - known):
        path = node_id.split("::", 1)[0]
        session.add(
            TestCaseRecord(
                analysis_run_id=analysis_run_id,
                symbol_id=None,
                node_id=node_id,
                path=path,
                qualified_name=node_id,
                collection_status="COLLECTED",
            )
        )
    session.flush()
    return collected


def _synthesise(classname: str, name: str) -> str:
    parts = classname.split(".")
    return "::".join(["/".join(parts) + ".py", name]) if parts else name


def _coverage_rows(
    run_id: uuid.UUID,
    envelope: dict[str, Any] | None,
    session: Session,
    analysis_run_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Per-test executed lines from coverage.py's dynamic ``test_function`` contexts."""
    if not envelope or not isinstance(envelope.get("coverage"), str):
        return []
    try:
        data = json.loads(envelope["coverage"])
    except json.JSONDecodeError:
        return []
    by_qualified: dict[str, str] = {
        str(qualified): str(node_id)
        for qualified, node_id in session.execute(
            select(TestCaseRecord.qualified_name, TestCaseRecord.node_id).where(
                TestCaseRecord.analysis_run_id == analysis_run_id
            )
        ).all()
    }
    grouped: dict[tuple[str | None, str], set[int]] = {}
    for path, info in (data.get("files") or {}).items():
        contexts = info.get("contexts") or {}
        for line, names in contexts.items():
            if not str(line).isdigit():
                continue
            for name in names or [""]:
                node_id = by_qualified.get(name) if name else None
                grouped.setdefault((node_id, str(path)), set()).add(int(line))
    rows = [
        {"test_run_id": run_id, "test_node_id": node, "path": path, "lines": sorted(lines)}
        for (node, path), lines in sorted(
            grouped.items(), key=lambda kv: (kv[0][0] or "", kv[0][1])
        )
    ]
    return rows[:MAX_COVERAGE_ROWS]


def cleanup_stale_execution(settings: Settings, *, older_than_seconds: float | None = None) -> None:
    """Remove containers and workspaces left behind by crashed workers (run at startup)."""
    age = older_than_seconds or settings.sandbox_timeout_seconds + 900
    DockerSandbox(settings.docker_binary).remove_stale(age)
    root = settings.workspaces_dir
    if root.exists():
        now = time.time()
        for child in root.iterdir():
            try:
                if now - child.stat().st_mtime > age:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                continue


def _limits_json(limits: SandboxLimits) -> dict[str, Any]:
    return {
        "memory_mb": limits.memory_mb,
        "cpus": limits.cpus,
        "pids": limits.pids,
        "timeout_seconds": limits.timeout_seconds,
        "workspace_mb": limits.workspace_mb,
        "tmp_mb": limits.tmp_mb,
        "network": "none",
    }
