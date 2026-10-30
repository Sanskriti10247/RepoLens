"""Job inspection and queue metrics (read-only)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter
from sqlalchemy import ColumnElement, func, select, true

from repolens.api.dependencies import SessionDep, UserDep
from repolens.api.pagination import Pagination
from repolens.api.schemas import JobDetailOut, JobPage, KindMetrics, PageMeta, QueueMetrics
from repolens.domain.enums import JobKind
from repolens.persistence.models import Job
from repolens.services import ownership
from repolens.services.errors import NotFound

router = APIRouter(tags=["jobs"])


@router.get("/jobs", response_model=JobPage)
def list_jobs(
    session: SessionDep,
    page: Pagination,
    user: UserDep,
    status: str | None = None,
    kind: str | None = None,
    repository_id: uuid.UUID | None = None,
) -> JobPage:
    stmt = select(Job).where(_visible(user))
    if status:
        stmt = stmt.where(Job.status == status)
    if kind:
        stmt = stmt.where(Job.kind == kind)
    if repository_id:
        stmt = stmt.where(Job.repository_id == repository_id)
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    jobs = session.execute(
        stmt.order_by(Job.created_at.desc(), Job.id).limit(page.limit).offset(page.offset)
    ).scalars()
    return JobPage(
        items=[JobDetailOut.model_validate(j) for j in jobs],
        page=PageMeta(total=total, limit=page.limit, offset=page.offset),
    )


@router.get("/jobs/{job_id}", response_model=JobDetailOut)
def get_job(job_id: uuid.UUID, session: SessionDep) -> JobDetailOut:
    job = session.get(Job, job_id)
    if job is None:
        raise NotFound("Job not found")
    return JobDetailOut.model_validate(job)


def _visible(user: uuid.UUID | None) -> ColumnElement[bool]:
    """All jobs for the operator; in user mode only jobs of the user's repositories."""
    if user is None:
        return true()
    return Job.repository_id.in_(ownership.owned_repository_ids(user))


def _ms(expr: ColumnElement[Any]) -> ColumnElement[Any]:
    return func.extract("epoch", expr) * 1000


@router.get("/metrics/queue", response_model=QueueMetrics)
def queue_metrics(session: SessionDep, user: UserDep) -> QueueMetrics:
    """Queue health from the jobs table: wait and run times are over the last 24 hours.

    In user mode the figures cover only the signed-in user's jobs.
    """
    visible = _visible(user)
    since = func.now() - func.make_interval(0, 0, 0, 1)
    kinds: list[KindMetrics] = []
    for kind in JobKind:
        by_status = dict(
            session.execute(
                select(Job.status, func.count())
                .where(visible, Job.kind == kind.value)
                .group_by(Job.status)
            ).all()
        )
        recent = visible & (Job.kind == kind.value) & (Job.finished_at >= since)
        wait = _ms(Job.claimed_at - Job.created_at)
        run = _ms(Job.finished_at - Job.started_at)
        stats = session.execute(
            select(
                func.count(),
                func.percentile_cont(0.5).within_group(wait),
                func.percentile_cont(0.95).within_group(wait),
                func.percentile_cont(0.5).within_group(run),
                func.percentile_cont(0.95).within_group(run),
                func.count().filter(Job.attempts > 1),
            ).where(recent)
        ).one()
        failures = dict(
            session.execute(
                select(Job.error_category, func.count())
                .where(recent, Job.error_category.is_not(None))
                .group_by(Job.error_category)
            ).all()
        )
        kinds.append(
            KindMetrics(
                kind=kind.value,
                by_status={k: int(v) for k, v in by_status.items()},
                completed_last_24h=int(stats[0]),
                median_queue_wait_ms=_round(stats[1]),
                p95_queue_wait_ms=_round(stats[2]),
                median_run_ms=_round(stats[3]),
                p95_run_ms=_round(stats[4]),
                retried_jobs=int(stats[5]),
                failure_categories={str(k): int(v) for k, v in failures.items()},
            )
        )
    active = visible & Job.status.in_(["CLAIMED", "RUNNING"])
    active_leases = session.execute(select(func.count()).where(active)).scalar_one()
    expired = session.execute(
        select(func.count()).where(active, Job.lease_expires_at < func.now())
    ).scalar_one()
    return QueueMetrics(kinds=kinds, active_leases=active_leases, expired_leases=expired)


def _round(value: Any) -> float | None:
    return None if value is None else round(float(value), 1)
