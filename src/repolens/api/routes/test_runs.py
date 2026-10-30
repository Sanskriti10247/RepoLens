"""Sandboxed test execution endpoints. No endpoint accepts commands or arbitrary arguments."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status
from sqlalchemy import func, literal, select

from repolens.api.dependencies import Authorized, SessionDep, SettingsDep, UserDep
from repolens.api.pagination import Pagination
from repolens.api.schemas import (
    CoverageFileOut,
    CoverageSummary,
    JobOut,
    PageMeta,
    TestResultOut,
    TestResultPage,
    TestRunCreate,
    TestRunLogs,
    TestRunOut,
    TestRunPage,
)
from repolens.persistence.models import CoverageRecord, Job, TestResultRecord, TestRun
from repolens.services import execution, ownership
from repolens.services.errors import NotFound

router = APIRouter(tags=["test execution"])


def _run(session: SessionDep, run_id: uuid.UUID) -> TestRun:
    run = session.get(TestRun, run_id)
    if run is None:
        raise NotFound("Test run not found")
    return run


def _run_out(session: SessionDep, run: TestRun) -> TestRunOut:
    job = session.execute(
        select(Job).where(Job.subject_id == run.id).order_by(Job.created_at.desc()).limit(1)
    ).scalar_one_or_none()
    out = TestRunOut.model_validate(run)
    out.job = JobOut.model_validate(job) if job else None
    return out


@router.post(
    "/test-runs",
    response_model=TestRunOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Authorized],
)
def create_test_run(body: TestRunCreate, session: SessionDep, settings: SettingsDep) -> TestRunOut:
    """Queue a sandboxed run of a test plan's selection, or of an analysis' full suite."""
    with session.begin():
        run, _ = execution.request_test_run(
            session,
            settings,
            plan_id=body.plan_id,
            analysis_id=body.analysis_id,
            full_suite=body.full_suite,
            coverage=body.coverage,
        )
        run_id = run.id
    return _run_out(session, _run(session, run_id))


@router.get("/test-runs", response_model=TestRunPage)
def list_test_runs(
    session: SessionDep, page: Pagination, user: UserDep, repository_id: uuid.UUID | None = None
) -> TestRunPage:
    stmt = select(TestRun)
    if user is not None:
        stmt = stmt.where(TestRun.repository_id.in_(ownership.owned_repository_ids(user)))
    if repository_id is not None:
        stmt = stmt.where(TestRun.repository_id == repository_id)
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    runs = session.execute(
        stmt.order_by(TestRun.created_at.desc(), TestRun.id).limit(page.limit).offset(page.offset)
    ).scalars()
    return TestRunPage(
        items=[TestRunOut.model_validate(r) for r in runs],
        page=PageMeta(total=total, limit=page.limit, offset=page.offset),
    )


@router.get("/test-runs/{run_id}", response_model=TestRunOut)
def get_test_run(run_id: uuid.UUID, session: SessionDep) -> TestRunOut:
    return _run_out(session, _run(session, run_id))


@router.post("/test-runs/{run_id}/cancel", response_model=TestRunOut, dependencies=[Authorized])
def cancel_test_run(run_id: uuid.UUID, session: SessionDep) -> TestRunOut:
    with session.begin():
        execution.cancel_test_run(session, run_id)
    return _run_out(session, _run(session, run_id))


@router.get("/test-runs/{run_id}/results", response_model=TestResultPage)
def test_run_results(
    run_id: uuid.UUID, session: SessionDep, page: Pagination, outcome: str | None = None
) -> TestResultPage:
    _run(session, run_id)
    stmt = select(TestResultRecord).where(TestResultRecord.test_run_id == run_id)
    if outcome:
        stmt = stmt.where(TestResultRecord.outcome == outcome)
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = session.execute(
        stmt.order_by(TestResultRecord.node_id).limit(page.limit).offset(page.offset)
    ).scalars()
    return TestResultPage(
        items=[TestResultOut.model_validate(r) for r in rows],
        page=PageMeta(total=total, limit=page.limit, offset=page.offset),
    )


@router.get("/test-runs/{run_id}/logs", response_model=TestRunLogs)
def test_run_logs(run_id: uuid.UUID, session: SessionDep) -> TestRunLogs:
    run = _run(session, run_id)
    return TestRunLogs(
        stdout_tail=run.stdout_tail,
        stderr_tail=run.stderr_tail,
        output_truncated=run.output_truncated,
    )


@router.get("/test-runs/{run_id}/coverage", response_model=CoverageSummary)
def test_run_coverage(run_id: uuid.UUID, session: SessionDep) -> CoverageSummary:
    _run(session, run_id)
    line = func.unnest(CoverageRecord.lines).table_valued("line").render_derived()
    rows = session.execute(
        select(
            CoverageRecord.path,
            func.count(func.distinct(CoverageRecord.test_node_id)),
            func.count(func.distinct(line.c.line)),
        )
        .select_from(CoverageRecord)
        .join(line, literal(True))
        .where(CoverageRecord.test_run_id == run_id)
        .group_by(CoverageRecord.path)
        .order_by(CoverageRecord.path)
    ).all()
    files = [
        CoverageFileOut(path=path, lines_covered=int(lines), tests=int(tests))
        for path, tests, lines in rows
    ]
    return CoverageSummary(test_run_id=run_id, files=files)


@router.post(
    "/analyses/{analysis_id}/collect",
    response_model=TestRunOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Authorized],
)
def collect_tests(analysis_id: uuid.UUID, session: SessionDep, settings: SettingsDep) -> TestRunOut:
    """Run ``pytest --collect-only`` in the sandbox to confirm the statically discovered tests."""
    with session.begin():
        run, _ = execution.request_collection(session, settings, analysis_id)
        run_id = run.id
    return _run_out(session, _run(session, run_id))
