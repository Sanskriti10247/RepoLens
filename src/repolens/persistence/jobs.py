"""PostgreSQL-backed job queue.

Concurrency model:
- Claiming uses ``SELECT … FOR UPDATE SKIP LOCKED`` so concurrent workers never claim the
  same row.
- Every state change is a conditional ``UPDATE`` that checks the current status *and* that the
  caller still holds an unexpired lease. Row locks serialise a worker's final write against the
  recovery sweeper; whichever commits first wins and the other matches zero rows. A worker
  whose lease expired therefore can never overwrite a newer state.
- Lease times use the database clock (``now()``) so worker clock skew does not matter.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from repolens.domain.enums import JobKind, JobStatus
from repolens.domain.jobs import ACTIVE_JOB_STATUSES, TERMINAL_JOB_STATUSES, sources_for
from repolens.persistence.models import JOB_NOT_TERMINAL_SQL, Job, Repository

_ACTIVE = [s.value for s in ACTIVE_JOB_STATUSES]
_NON_TERMINAL = [JobStatus.QUEUED.value, *_ACTIVE]


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: uuid.UUID
    kind: JobKind
    payload: dict[str, Any]
    repository_id: uuid.UUID | None
    subject_id: uuid.UUID | None
    attempts: int
    max_attempts: int
    lease_expires_at: datetime
    queue_wait_ms: float | None = None


@dataclass(frozen=True, slots=True)
class HeartbeatResult:
    lease_held: bool
    cancel_requested: bool


@dataclass(frozen=True, slots=True)
class RecoveredJob:
    id: uuid.UUID
    status: JobStatus
    subject_id: uuid.UUID | None
    kind: JobKind


def enqueue(
    session: Session,
    kind: JobKind,
    *,
    payload: dict[str, Any] | None = None,
    repository_id: uuid.UUID | None = None,
    subject_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
    priority: int = 100,
    max_attempts: int = 3,
) -> tuple[uuid.UUID, bool]:
    """Insert a job. Returns ``(job_id, created)``.

    With an idempotency key, an existing *active* job with the same key is returned instead
    of creating a duplicate.
    """
    job_id = uuid.uuid4()
    stmt = (
        insert(Job)
        .values(
            id=job_id,
            kind=kind.value,
            status=JobStatus.QUEUED.value,
            payload=payload or {},
            repository_id=repository_id,
            subject_id=subject_id,
            idempotency_key=idempotency_key,
            priority=priority,
            max_attempts=max_attempts,
            attempts=0,
        )
        .on_conflict_do_nothing(
            index_elements=["idempotency_key"],
            index_where=text(JOB_NOT_TERMINAL_SQL),
        )
        .returning(Job.id)
    )
    inserted = session.execute(stmt).scalar_one_or_none()
    if inserted is not None:
        return inserted, True
    existing = session.execute(
        select(Job.id).where(Job.idempotency_key == idempotency_key, Job.status.in_(_NON_TERMINAL))
    ).scalar_one()
    return existing, False


@dataclass(frozen=True, slots=True)
class ClaimLimits:
    """Concurrency limits enforced at claim time (``None`` = unlimited)."""

    per_repository: int | None = None
    per_kind: Mapping[JobKind, int] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.per_repository is not None or bool(self.per_kind)


_RETURNING = (
    Job.id,
    Job.kind,
    Job.payload,
    Job.repository_id,
    Job.subject_id,
    Job.attempts,
    Job.max_attempts,
    Job.lease_expires_at,
    (func.extract("epoch", func.now() - Job.created_at) * 1000).label("queue_wait_ms"),
)
MAX_CLAIM_CANDIDATES = 20


def claim(
    session: Session,
    worker_id: str,
    lease: timedelta,
    kinds: list[JobKind] | None = None,
    limits: ClaimLimits | None = None,
) -> ClaimedJob | None:
    """Claim the next eligible job, respecting concurrency limits.

    Limits are checked *after* taking a lock that serialises competing claims (an advisory
    lock per job kind, a row lock on the repository), so two workers can never both see
    "one slot left" and exceed the limit. Candidates over a limit are skipped in favour of
    the next eligible job rather than blocking the queue.
    """
    excluded_kinds: set[str] = set()
    excluded_repos: set[uuid.UUID] = set()
    for _ in range(MAX_CLAIM_CANDIDATES):
        candidate = _next_candidate(session, kinds, excluded_kinds, excluded_repos)
        if candidate is None:
            return None
        job_id, kind, repository_id = candidate
        if limits is not None and limits.active:
            limit = limits.per_kind.get(JobKind(kind))
            if limit is not None:
                session.execute(
                    select(func.pg_advisory_xact_lock(func.hashtext(f"repolens:kind:{kind}")))
                )
                if _active_count(session, Job.kind == kind) >= limit:
                    excluded_kinds.add(kind)
                    continue
            if limits.per_repository is not None and repository_id is not None:
                session.execute(
                    select(Repository.id).where(Repository.id == repository_id).with_for_update()
                )
                if _active_count(session, Job.repository_id == repository_id) >= (
                    limits.per_repository
                ):
                    excluded_repos.add(repository_id)
                    continue
        row = session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == JobStatus.QUEUED.value)
            .values(
                status=JobStatus.CLAIMED.value,
                lease_owner=worker_id,
                lease_expires_at=func.now() + lease,
                heartbeat_at=func.now(),
                claimed_at=func.now(),
                attempts=Job.attempts + 1,
            )
            .returning(*_RETURNING)
        ).one_or_none()
        if row is not None:
            return ClaimedJob(
                id=row.id,
                kind=JobKind(row.kind),
                payload=row.payload,
                repository_id=row.repository_id,
                subject_id=row.subject_id,
                attempts=row.attempts,
                max_attempts=row.max_attempts,
                lease_expires_at=row.lease_expires_at,
                queue_wait_ms=round(float(row.queue_wait_ms), 1),
            )
    return None


def _next_candidate(
    session: Session,
    kinds: list[JobKind] | None,
    excluded_kinds: set[str],
    excluded_repos: set[uuid.UUID],
) -> tuple[uuid.UUID, str, uuid.UUID | None] | None:
    stmt = (
        select(Job.id, Job.kind, Job.repository_id)
        .where(Job.status == JobStatus.QUEUED.value, Job.run_after <= func.now())
        .order_by(Job.priority, Job.run_after, Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if kinds:
        stmt = stmt.where(Job.kind.in_([k.value for k in kinds]))
    if excluded_kinds:
        stmt = stmt.where(Job.kind.not_in(sorted(excluded_kinds)))
    if excluded_repos:
        stmt = stmt.where(
            or_(Job.repository_id.is_(None), Job.repository_id.not_in(sorted(excluded_repos)))
        )
    row = session.execute(stmt).one_or_none()
    return None if row is None else (row.id, row.kind, row.repository_id)


def _active_count(session: Session, condition: Any) -> int:
    count: int = session.execute(
        select(func.count()).select_from(Job).where(Job.status.in_(_ACTIVE), condition)
    ).scalar_one()
    return count


def _held_by(job_id: uuid.UUID, worker_id: str) -> Any:
    return and_(
        Job.id == job_id,
        Job.lease_owner == worker_id,
        Job.lease_expires_at > func.now(),
    )


def start(session: Session, job_id: uuid.UUID, worker_id: str) -> bool:
    result = session.execute(
        update(Job)
        .where(_held_by(job_id, worker_id), Job.status == JobStatus.CLAIMED.value)
        .values(status=JobStatus.RUNNING.value, started_at=func.now())
        .returning(Job.id)
    )
    return result.scalar_one_or_none() is not None


def heartbeat(
    session: Session, job_id: uuid.UUID, worker_id: str, lease: timedelta
) -> HeartbeatResult:
    """Extend the lease. A lease that already expired is *not* renewed: the job is lost."""
    row = session.execute(
        update(Job)
        .where(_held_by(job_id, worker_id), Job.status.in_(_ACTIVE))
        .values(lease_expires_at=func.now() + lease, heartbeat_at=func.now())
        .returning(Job.cancel_requested_at)
    ).one_or_none()
    if row is None:
        return HeartbeatResult(lease_held=False, cancel_requested=False)
    return HeartbeatResult(lease_held=True, cancel_requested=row.cancel_requested_at is not None)


def finish(
    session: Session,
    job_id: uuid.UUID,
    worker_id: str,
    status: JobStatus,
    *,
    result: dict[str, Any] | None = None,
    error_category: str | None = None,
    error_detail: str | None = None,
) -> bool:
    """Write a terminal state. Returns False if the lease was lost (nothing is written)."""
    if status not in TERMINAL_JOB_STATUSES:
        raise ValueError(f"{status} is not terminal")
    written = session.execute(
        update(Job)
        .where(_held_by(job_id, worker_id), Job.status.in_([s.value for s in sources_for(status)]))
        .values(
            status=status.value,
            result=result,
            error_category=error_category,
            error_detail=_truncate(error_detail),
            finished_at=func.now(),
            lease_owner=None,
            lease_expires_at=None,
        )
        .returning(Job.id)
    )
    return written.scalar_one_or_none() is not None


def retry_or_fail(
    session: Session,
    job_id: uuid.UUID,
    worker_id: str,
    *,
    error_category: str,
    error_detail: str | None,
    backoff: timedelta,
) -> JobStatus | None:
    """Requeue after a retryable error, or fail once attempts are exhausted.

    Returns the new status, or None if the lease was lost.
    """
    row = session.execute(
        update(Job)
        .where(_held_by(job_id, worker_id), Job.status.in_(_ACTIVE))
        .values(
            status=_retry_status_expr(),
            run_after=func.now() + backoff,
            finished_at=_finished_if_failed_expr(),
            error_category=error_category,
            error_detail=_truncate(error_detail),
            lease_owner=None,
            lease_expires_at=None,
        )
        .returning(Job.status)
    ).one_or_none()
    return None if row is None else JobStatus(row.status)


def request_cancel(session: Session, job_id: uuid.UUID) -> JobStatus | None:
    """Cancel a queued job immediately; flag an active one for cooperative cancellation."""
    queued = session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == JobStatus.QUEUED.value)
        .values(
            status=JobStatus.CANCELLED.value,
            cancel_requested_at=func.now(),
            finished_at=func.now(),
            error_category="cancelled",
        )
        .returning(Job.status)
    ).one_or_none()
    if queued is not None:
        return JobStatus.CANCELLED
    session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status.in_(_ACTIVE), Job.cancel_requested_at.is_(None))
        .values(cancel_requested_at=func.now())
    )
    status = session.execute(select(Job.status).where(Job.id == job_id)).scalar_one_or_none()
    return None if status is None else JobStatus(status)


def recover_expired(
    session: Session, *, backoff: timedelta, limit: int = 100
) -> list[RecoveredJob]:
    """Reclaim jobs whose worker stopped heartbeating (crashed, killed, partitioned).

    Cancelled-while-running jobs become CANCELLED; others are requeued while attempts remain,
    otherwise FAILED. Safe to run concurrently from several workers (SKIP LOCKED).
    """
    expired = (
        select(Job.id)
        .where(Job.status.in_(_ACTIVE), Job.lease_expires_at <= func.now())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    new_status = _recovery_status_expr()
    rows = session.execute(
        update(Job)
        .where(
            Job.id.in_(expired.scalar_subquery()),
            Job.status.in_(_ACTIVE),
            Job.lease_expires_at <= func.now(),
        )
        .values(
            status=new_status,
            run_after=func.now() + backoff,
            finished_at=_finished_if_terminal_expr(new_status),
            error_category="lease_expired",
            error_detail="worker lease expired before the job finished",
            lease_owner=None,
            lease_expires_at=None,
        )
        .returning(Job.id, Job.status, Job.subject_id, Job.kind)
    ).all()
    return [
        RecoveredJob(
            id=r.id, status=JobStatus(r.status), subject_id=r.subject_id, kind=JobKind(r.kind)
        )
        for r in rows
    ]


def _retry_status_expr() -> Any:
    return case(
        (Job.attempts >= Job.max_attempts, JobStatus.FAILED.value),
        else_=JobStatus.QUEUED.value,
    )


def _finished_if_failed_expr() -> Any:
    return case((Job.attempts >= Job.max_attempts, func.now()), else_=None)


def _recovery_status_expr() -> Any:
    return case(
        (Job.cancel_requested_at.is_not(None), JobStatus.CANCELLED.value),
        (Job.attempts >= Job.max_attempts, JobStatus.FAILED.value),
        else_=JobStatus.QUEUED.value,
    )


def _finished_if_terminal_expr(status_expr: Any) -> Any:
    return case((status_expr != JobStatus.QUEUED.value, func.now()), else_=None)


def _truncate(detail: str | None, limit: int = 4000) -> str | None:
    if detail is None or len(detail) <= limit:
        return detail
    return detail[: limit - 15] + "… [truncated]"


__all__ = [
    "ClaimLimits",
    "ClaimedJob",
    "HeartbeatResult",
    "RecoveredJob",
    "claim",
    "enqueue",
    "finish",
    "heartbeat",
    "recover_expired",
    "request_cancel",
    "retry_or_fail",
    "start",
]
