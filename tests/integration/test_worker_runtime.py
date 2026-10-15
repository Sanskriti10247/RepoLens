"""Worker runtime behaviour with real queue semantics."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from repolens.domain.enums import JobKind, JobStatus
from repolens.persistence import jobs as queue
from repolens.persistence.db import transaction
from repolens.persistence.models import Job
from repolens.services.jobs import (
    JobContext,
    PermanentJobError,
    PolicyBlockedError,
    RetryableJobError,
)
from repolens.worker.runtime import Worker

pytestmark = pytest.mark.integration
KIND = JobKind.ANALYZE_REPOSITORY


@dataclass
class FakeHandler:
    behaviour: Any
    timeout: timedelta | None = None
    failures: list[tuple[uuid.UUID, JobStatus, str]] = field(default_factory=list)

    def run(self, ctx: JobContext) -> dict[str, Any] | None:
        result: dict[str, Any] | None = self.behaviour(ctx)
        return result

    def on_terminal_failure(
        self,
        session: Session,
        job: queue.ClaimedJob | queue.RecoveredJob,
        status: JobStatus,
        category: str,
        detail: str | None,
    ) -> None:
        self.failures.append((job.id, status, category))


def _job(factory: sessionmaker[Session], **kwargs: Any) -> uuid.UUID:
    with transaction(factory) as session:
        job_id, _ = queue.enqueue(session, KIND, **kwargs)
    return job_id


def _row(factory: sessionmaker[Session], job_id: uuid.UUID) -> Job:
    with transaction(factory) as session:
        return session.execute(select(Job).where(Job.id == job_id)).scalar_one()


def _worker(factory: sessionmaker[Session], handler: FakeHandler, **kwargs: Any) -> Worker:
    return Worker(
        factory, {KIND: handler}, worker_id="test-worker", retry_backoff=timedelta(0), **kwargs
    )


def test_success_records_result(session_factory: sessionmaker[Session]) -> None:
    job_id = _job(session_factory)
    handler = FakeHandler(lambda ctx: {"seen": str(ctx.job.id)})
    assert _worker(session_factory, handler).run_once()
    row = _row(session_factory, job_id)
    assert row.status == JobStatus.SUCCEEDED
    assert row.result == {"seen": str(job_id)}
    assert row.lease_owner is None and row.finished_at is not None
    assert handler.failures == []


def test_no_job_returns_false(session_factory: sessionmaker[Session]) -> None:
    assert not _worker(session_factory, FakeHandler(lambda ctx: None)).run_once()


@pytest.mark.parametrize(
    ("error", "status", "category"),
    [
        (PermanentJobError("bad ref", category="invalid_ref"), JobStatus.FAILED, "invalid_ref"),
        (PolicyBlockedError("not allowed"), JobStatus.BLOCKED, "policy_blocked"),
        (KeyError("bug"), JobStatus.FAILED, "internal_error"),
    ],
)
def test_errors_map_to_terminal_states_without_retry(
    session_factory: sessionmaker[Session],
    error: Exception,
    status: JobStatus,
    category: str,
) -> None:
    job_id = _job(session_factory, max_attempts=3)

    def fail(ctx: JobContext) -> None:
        raise error

    handler = FakeHandler(fail)
    _worker(session_factory, handler).run_once()
    row = _row(session_factory, job_id)
    assert (row.status, row.error_category, row.attempts) == (status, category, 1)
    assert handler.failures == [(job_id, status, category)]


def test_retryable_errors_retry_until_exhausted(session_factory: sessionmaker[Session]) -> None:
    job_id = _job(session_factory, max_attempts=2)

    def flaky(ctx: JobContext) -> None:
        raise RetryableJobError("clone timed out")

    handler = FakeHandler(flaky)
    worker = _worker(session_factory, handler)
    worker.run_once()
    assert _row(session_factory, job_id).status == JobStatus.QUEUED
    assert handler.failures == []
    worker.run_once()
    row = _row(session_factory, job_id)
    assert (row.status, row.attempts) == (JobStatus.FAILED, 2)
    assert handler.failures == [(job_id, JobStatus.FAILED, "infrastructure_error")]


def test_cancellation_is_observed_through_heartbeat(
    session_factory: sessionmaker[Session],
) -> None:
    job_id = _job(session_factory)

    def long_running(ctx: JobContext) -> None:
        with transaction(session_factory) as session:
            queue.request_cancel(session, ctx.job.id)
        for _ in range(200):
            ctx.checkpoint()
            time.sleep(0.01)
        raise AssertionError("cancellation was never observed")

    handler = FakeHandler(long_running)
    _worker(session_factory, handler, lease=timedelta(milliseconds=150)).run_once()
    assert _row(session_factory, job_id).status == JobStatus.CANCELLED
    assert handler.failures[0][1] is JobStatus.CANCELLED


def test_timeout_is_enforced_at_checkpoints(session_factory: sessionmaker[Session]) -> None:
    job_id = _job(session_factory)

    def slow(ctx: JobContext) -> None:
        while True:
            ctx.checkpoint()
            time.sleep(0.01)

    handler = FakeHandler(slow, timeout=timedelta(milliseconds=100))
    _worker(session_factory, handler).run_once()
    assert _row(session_factory, job_id).status == JobStatus.TIMED_OUT


def test_lost_lease_discards_result(session_factory: sessionmaker[Session]) -> None:
    job_id = _job(session_factory)

    def stolen(ctx: JobContext) -> dict[str, str]:
        # Simulate the sweeper reclaiming the job while this worker is still busy.
        with transaction(session_factory) as session:
            session.execute(
                update(Job)
                .where(Job.id == ctx.job.id)
                .values(lease_expires_at=Job.lease_expires_at - timedelta(hours=1))
            )
            queue.recover_expired(session, backoff=timedelta(hours=1))
        time.sleep(0.3)  # let the heartbeat notice
        return {"should": "not be written"}

    handler = FakeHandler(stolen)
    _worker(session_factory, handler, lease=timedelta(milliseconds=150)).run_once()
    row = _row(session_factory, job_id)
    assert row.status == JobStatus.QUEUED
    assert row.result is None


def test_recover_notifies_handler_of_terminal_failures(
    session_factory: sessionmaker[Session],
) -> None:
    job_id = _job(session_factory, max_attempts=1)
    with transaction(session_factory) as session:
        queue.claim(session, "dead-worker", timedelta(seconds=30))
        session.execute(update(Job).where(Job.id == job_id).values(lease_expires_at=Job.created_at))
    handler = FakeHandler(lambda ctx: None)
    recovered = _worker(session_factory, handler).recover()
    assert [r.status for r in recovered] == [JobStatus.FAILED]
    assert handler.failures == [(job_id, JobStatus.FAILED, "lease_expired")]


def test_run_forever_stops_on_event(session_factory: sessionmaker[Session]) -> None:
    for _ in range(3):
        _job(session_factory)
    done: list[uuid.UUID] = []
    stop = threading.Event()

    def record(ctx: JobContext) -> None:
        done.append(ctx.job.id)
        if len(done) == 3:
            stop.set()

    worker = _worker(session_factory, FakeHandler(record), poll_interval=0.01)
    thread = threading.Thread(target=worker.run_forever, args=(stop,))
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert len(done) == 3
