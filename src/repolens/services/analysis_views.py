"""Read-side queries for analyses (no side effects)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from repolens.persistence.models import AnalysisRun, Job, RepositorySnapshot
from repolens.services.analysis import effective_run_id
from repolens.services.errors import NotFound
from repolens.services.repositories import Page


@dataclass(frozen=True, slots=True)
class AnalysisView:
    run: AnalysisRun
    results_run_id: uuid.UUID
    snapshot: RepositorySnapshot | None
    job: Job | None


def get_analysis(session: Session, run_id: uuid.UUID) -> AnalysisView:
    run = session.get(AnalysisRun, run_id)
    if run is None:
        raise NotFound("Analysis not found")
    snapshot = session.get(RepositorySnapshot, run.snapshot_id) if run.snapshot_id else None
    job = session.execute(
        select(Job).where(Job.subject_id == run.id).order_by(Job.created_at.desc()).limit(1)
    ).scalar_one_or_none()
    return AnalysisView(run, effective_run_id(session, run.id), snapshot, job)


def list_analyses(
    session: Session, repository_id: uuid.UUID, *, limit: int, offset: int
) -> Page[tuple[AnalysisRun, str | None]]:
    total = session.execute(
        select(func.count()).where(AnalysisRun.repository_id == repository_id)
    ).scalar_one()
    rows = session.execute(
        select(AnalysisRun, RepositorySnapshot.commit_sha)
        .outerjoin(RepositorySnapshot, RepositorySnapshot.id == AnalysisRun.snapshot_id)
        .where(AnalysisRun.repository_id == repository_id)
        .order_by(AnalysisRun.created_at.desc(), AnalysisRun.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return Page(items=[(r[0], r[1]) for r in rows], total=total, limit=limit, offset=offset)


def latest_analyses(
    session: Session, repository_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[AnalysisRun, str | None]]:
    """Most recent analysis per repository, in one query (no N+1)."""
    if not repository_ids:
        return {}
    rows = session.execute(
        select(AnalysisRun, RepositorySnapshot.commit_sha)
        .outerjoin(RepositorySnapshot, RepositorySnapshot.id == AnalysisRun.snapshot_id)
        .where(AnalysisRun.repository_id.in_(repository_ids))
        .ext(distinct_on(AnalysisRun.repository_id))
        .order_by(AnalysisRun.repository_id, AnalysisRun.created_at.desc())
    ).all()
    return {row[0].repository_id: (row[0], row[1]) for row in rows}
