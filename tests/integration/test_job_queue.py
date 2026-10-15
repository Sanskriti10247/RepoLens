"""Job queue semantics against real PostgreSQL (locking behaviour cannot be faked)."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from repolens.domain.enums import JobKind, JobStatus
from repolens.persistence import jobs as queue
from repolens.persistence.db import transaction
from repolens.persistence.models import Job

pytestmark = pytest.mark.integration
KIND = JobKind.ANALYZE_REPOSITORY
LEASE = timedelta(seconds=30)


def _enqueue(factory: sessionmaker[Session], **kwargs: object) -> uuid.UUID:
    with transaction(factory) as session:
        job_id, _ = queue.enqueue(session, KIND, **kwargs)  # type: ignore[arg-type]
    return job_id


def _status(factory: sessionmaker[Session], job_id: uuid.UUID) -> JobStatus:
    with transaction(factory) as session:
        return JobStatus(session.execute(select(Job.status).where(Job.id == job_id)).scalar_one())


def _claim(factory: sessionmaker[Session], worker: str, lease: timedelta = LEASE):
    with transaction(factory) as session:
        return queue.claim(session, worker, lease)


def _expire_lease(factory: sessionmaker[Session], job_id: uuid.UUID) -> None:
    with transaction(factory) as session:
        session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(lease_expires_at=Job.lease_expires_at - timedelta(hours=1))
        )


def test_happy_path_lifecycle(session_factory: sessionmaker[Session]) -> None:
    job_id = _enqueue(session_factory, payload={"ref": "main"})
    claimed = _claim(session_factory, "w1")
    assert claimed is not None and claimed.id == job_id
    assert claimed.payload == {"ref": "main"} and claimed.attempts == 1
    with transaction(session_factory) as session:
        assert queue.start(session, job_id, "w1")
        assert queue.heartbeat(session, job_id, "w1", LEASE).lease_held
        assert queue.finish(session, job_id, "w1", JobStatus.SUCCEEDED, result={"n": 1})
    assert _status(session_factory, job_id) is JobStatus.SUCCEEDED
    assert _claim(session_factory, "w2") is None


def test_concurrent_claims_never_share_a_job(session_factory: sessionmaker[Session]) -> None:
    job_ids = {_enqueue(session_factory) for _ in range(20)}
    claims: list[uuid.UUID] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker(n: int) -> None:
        barrier.wait()
        while True:
            claimed = _claim(session_factory, f"w{n}")
            if claimed is None:
                return
            with lock:
                claims.append(claimed.id)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(claims) == sorted(job_ids)  # every job exactly once


def test_priority_and_run_after_ordering(session_factory: sessionmaker[Session]) -> None:
    low = _enqueue(session_factory, priority=200)
    high = _enqueue(session_factory, priority=10)
    with transaction(session_factory) as session:
        delayed, _ = queue.enqueue(session, KIND, priority=1)
        session.execute(
            update(Job)
            .where(Job.id == delayed)
            .values(run_after=Job.run_after + timedelta(hours=1))
        )
    assert _claim(session_factory, "w").id == high  # type: ignore[union-attr]
    assert _claim(session_factory, "w").id == low  # type: ignore[union-attr]
    assert _claim(session_factory, "w") is None  # delayed job not yet eligible


def test_idempotent_enqueue_returns_active_job(session_factory: sessionmaker[Session]) -> None:
    with transaction(session_factory) as session:
        first, created_first = queue.enqueue(session, KIND, idempotency_key="analyze:r:main")
        second, created_second = queue.enqueue(session, KIND, idempotency_key="analyze:r:main")
    assert (created_first, created_second) == (True, False)
    assert first == second


def test_expired_worker_cannot_overwrite_newer_result(
    session_factory: sessionmaker[Session],
) -> None:
    job_id = _enqueue(session_factory)
    assert _claim(session_factory, "old") is not None
    with transaction(session_factory) as session:
        assert queue.start(session, job_id, "old")
    _expire_lease(session_factory, job_id)

    with transaction(session_factory) as session:
        recovered = queue.recover_expired(session, backoff=timedelta(0))
    assert [(r.id, r.status) for r in recovered] == [(job_id, JobStatus.QUEUED)]

    new = _claim(session_factory, "new")
    assert new is not None and new.attempts == 2
    with transaction(session_factory) as session:
        assert queue.start(session, job_id, "new")
        assert queue.finish(session, job_id, "new", JobStatus.FAILED, error_category="x")
    # The stale worker wakes up and tries to report success: nothing is written.
    with transaction(session_factory) as session:
        assert not queue.heartbeat(session, job_id, "old", LEASE).lease_held
        assert not queue.finish(session, job_id, "old", JobStatus.SUCCEEDED)
    assert _status(session_factory, job_id) is JobStatus.FAILED


def test_expired_lease_is_not_renewed(session_factory: sessionmaker[Session]) -> None:
    job_id = _enqueue(session_factory)
    _claim(session_factory, "w")
    _expire_lease(session_factory, job_id)
    with transaction(session_factory) as session:
        assert not queue.heartbeat(session, job_id, "w", LEASE).lease_held
        assert not queue.start(session, job_id, "w")


def test_recovery_fails_job_when_attempts_exhausted(
    session_factory: sessionmaker[Session],
) -> None:
    job_id = _enqueue(session_factory, max_attempts=1)
    _claim(session_factory, "w")
    _expire_lease(session_factory, job_id)
    with transaction(session_factory) as session:
        recovered = queue.recover_expired(session, backoff=timedelta(0))
    assert recovered[0].status is JobStatus.FAILED
    assert _status(session_factory, job_id) is JobStatus.FAILED


def test_recovery_honours_pending_cancellation(session_factory: sessionmaker[Session]) -> None:
    job_id = _enqueue(session_factory)
    _claim(session_factory, "w")
    with transaction(session_factory) as session:
        assert queue.request_cancel(session, job_id) is JobStatus.CLAIMED
    _expire_lease(session_factory, job_id)
    with transaction(session_factory) as session:
        assert queue.recover_expired(session, backoff=timedelta(0))[0].status is (
            JobStatus.CANCELLED
        )


def test_retry_backoff_then_exhaustion(session_factory: sessionmaker[Session]) -> None:
    job_id = _enqueue(session_factory, max_attempts=2)
    _claim(session_factory, "w")
    with transaction(session_factory) as session:
        status = queue.retry_or_fail(
            session, job_id, "w", error_category="net", error_detail="x", backoff=LEASE
        )
    assert status is JobStatus.QUEUED
    assert _claim(session_factory, "w") is None  # backoff delays the retry

    with transaction(session_factory) as session:
        session.execute(update(Job).where(Job.id == job_id).values(run_after=Job.created_at))
    assert _claim(session_factory, "w") is not None
    with transaction(session_factory) as session:
        status = queue.retry_or_fail(
            session, job_id, "w", error_category="net", error_detail="x", backoff=LEASE
        )
    assert status is JobStatus.FAILED


def test_cancel_queued_job_is_immediate(session_factory: sessionmaker[Session]) -> None:
    job_id = _enqueue(session_factory)
    with transaction(session_factory) as session:
        assert queue.request_cancel(session, job_id) is JobStatus.CANCELLED
    assert _claim(session_factory, "w") is None


def test_cancel_running_job_is_signalled_by_heartbeat(
    session_factory: sessionmaker[Session],
) -> None:
    job_id = _enqueue(session_factory)
    _claim(session_factory, "w")
    with transaction(session_factory) as session:
        queue.start(session, job_id, "w")
        assert queue.request_cancel(session, job_id) is JobStatus.RUNNING
        assert queue.heartbeat(session, job_id, "w", LEASE).cancel_requested


def test_cancel_after_terminal_state_is_a_no_op(session_factory: sessionmaker[Session]) -> None:
    job_id = _enqueue(session_factory)
    _claim(session_factory, "w")
    with transaction(session_factory) as session:
        queue.start(session, job_id, "w")
        queue.finish(session, job_id, "w", JobStatus.SUCCEEDED)
        assert queue.request_cancel(session, job_id) is JobStatus.SUCCEEDED
    assert _status(session_factory, job_id) is JobStatus.SUCCEEDED


def test_finish_rejects_non_terminal_status(session_factory: sessionmaker[Session]) -> None:
    with transaction(session_factory) as session, pytest.raises(ValueError):
        queue.finish(session, uuid.uuid4(), "w", JobStatus.RUNNING)


def _race_finish_against_recovery(factory: sessionmaker[Session]) -> None:
    job_id = _enqueue(factory)
    _claim(factory, "w", lease=timedelta(milliseconds=200))
    with transaction(factory) as session:
        queue.start(session, job_id, "w")
    time.sleep(0.19)
    results: dict[str, bool] = {}
    barrier = threading.Barrier(2)

    def finisher() -> None:
        barrier.wait()
        with transaction(factory) as session:
            results["finished"] = queue.finish(session, job_id, "w", JobStatus.SUCCEEDED)

    def sweeper() -> None:
        barrier.wait()
        time.sleep(0.01)
        with transaction(factory) as session:
            results["recovered"] = bool(queue.recover_expired(session, backoff=timedelta(0)))

    threads = [threading.Thread(target=finisher), threading.Thread(target=sweeper)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results["finished"] != results["recovered"], results
    expected = JobStatus.SUCCEEDED if results["finished"] else JobStatus.QUEUED
    assert _status(factory, job_id) is expected
    with transaction(factory) as session:  # keep later iterations independent
        session.execute(update(Job).where(Job.id == job_id).values(status="CANCELLED"))


def test_finish_racing_recovery_has_exactly_one_winner(
    session_factory: sessionmaker[Session],
) -> None:
    """A worker finishing as its lease expires vs. the sweeper: exactly one write wins."""
    for _ in range(10):
        _race_finish_against_recovery(session_factory)
