"""Worker runtime: claim a job, run its handler with a heartbeat, record the outcome.

Outcome rules:
- Handler returns → SUCCEEDED.
- ``RetryableJobError`` (infrastructure trouble) → requeued with backoff until attempts run out.
- ``PermanentJobError`` → FAILED; ``PolicyBlockedError`` → BLOCKED; ``JobCancelled`` → CANCELLED.
- Any other exception is a defect, not a transient condition → FAILED (``internal_error``),
  without retry.
- If the lease is lost, nothing is written: the recovery sweeper or another worker owns the job.
"""

from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from repolens.domain.enums import JobKind, JobStatus
from repolens.observability.logging import bind_context, clear_context, get_logger
from repolens.persistence import jobs as queue
from repolens.persistence.db import transaction
from repolens.services.jobs import (
    JobCancelled,
    JobContext,
    JobError,
    JobHandler,
    JobTimedOut,
    LeaseLost,
    PermanentJobError,
    PolicyBlockedError,
    RetryableJobError,
)

log = get_logger(__name__)


# ``None`` means "lease lost: write nothing". QUEUED means "retry if attempts remain".
_OUTCOME_FOR_ERROR: dict[type[JobError], JobStatus | None] = {
    LeaseLost: None,
    JobCancelled: JobStatus.CANCELLED,
    JobTimedOut: JobStatus.TIMED_OUT,
    PolicyBlockedError: JobStatus.BLOCKED,
    RetryableJobError: JobStatus.QUEUED,
    PermanentJobError: JobStatus.FAILED,
    JobError: JobStatus.FAILED,
}


def _outcome_for(error: JobError) -> JobStatus | None:
    for cls in type(error).__mro__:
        if cls in _OUTCOME_FOR_ERROR:
            return _OUTCOME_FOR_ERROR[cls]
    return JobStatus.FAILED


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class Worker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        handlers: Mapping[JobKind, JobHandler],
        *,
        worker_id: str | None = None,
        lease: timedelta = timedelta(seconds=60),
        retry_backoff: timedelta = timedelta(seconds=10),
        poll_interval: float = 1.0,
        recovery_interval: float = 15.0,
    ) -> None:
        self.session_factory = session_factory
        self.handlers = dict(handlers)
        self.worker_id = worker_id or default_worker_id()
        self.lease = lease
        self.retry_backoff = retry_backoff
        self.poll_interval = poll_interval
        self.recovery_interval = recovery_interval
        self._last_recovery = 0.0

    # -- public API ---------------------------------------------------------------------

    def run_forever(self, stop: threading.Event) -> None:
        log.info("worker_started", worker_id=self.worker_id, kinds=sorted(self.handlers))
        while not stop.is_set():
            try:
                if time.monotonic() - self._last_recovery >= self.recovery_interval:
                    self.recover()
                if not self.run_once():
                    stop.wait(self.poll_interval)
            except Exception:
                # Database outages and similar must not kill the worker loop.
                log.exception("worker_loop_error", worker_id=self.worker_id)
                stop.wait(min(self.poll_interval * 5, 30))
        log.info("worker_stopped", worker_id=self.worker_id)

    def recover(self) -> list[queue.RecoveredJob]:
        self._last_recovery = time.monotonic()
        with transaction(self.session_factory) as session:
            recovered = queue.recover_expired(session, backoff=self.retry_backoff)
            for job in recovered:
                log.warning("job_lease_expired", job_id=str(job.id), new_status=job.status)
                handler = self.handlers.get(job.kind)
                if job.status is not JobStatus.QUEUED and handler is not None:
                    handler.on_terminal_failure(
                        session, job, job.status, "lease_expired", "worker stopped heartbeating"
                    )
        return recovered

    def run_once(self) -> bool:
        with transaction(self.session_factory) as session:
            claimed = queue.claim(session, self.worker_id, self.lease, kinds=list(self.handlers))
        if claimed is None:
            return False
        self._execute(claimed)
        return True

    # -- internals ----------------------------------------------------------------------

    def _execute(self, job: queue.ClaimedJob) -> None:
        clear_context()
        bind_context(job_id=str(job.id), job_kind=job.kind.value, worker_id=self.worker_id)
        started = time.monotonic()
        timeout = self.handlers[job.kind].timeout
        ctx = JobContext(
            job=job,
            worker_id=self.worker_id,
            session_factory=self.session_factory,
            deadline=started + timeout.total_seconds() if timeout else None,
        )
        with transaction(self.session_factory) as session:
            if not queue.start(session, job.id, self.worker_id):
                log.warning("job_start_lost_lease")
                clear_context()
                return
        log.info("job_started", attempt=job.attempts)
        done = threading.Event()
        beat = threading.Thread(target=self._heartbeat, args=(ctx, done), daemon=True)
        beat.start()
        try:
            outcome = self._run_handler(ctx)
        finally:
            done.set()
            beat.join(timeout=5)
        self._record(ctx, outcome, duration=time.monotonic() - started)
        clear_context()

    def _run_handler(
        self, ctx: JobContext
    ) -> tuple[JobStatus | None, dict[str, Any] | None, JobError | None]:
        handler = self.handlers[ctx.job.kind]
        try:
            result = handler.run(ctx)
            ctx.checkpoint()  # a cancellation or lease loss during the last step still counts
        except JobError as exc:
            return _outcome_for(exc), None, exc
        except Exception as exc:
            log.exception("job_handler_crashed", error_type=type(exc).__name__)
            crash = PermanentJobError(f"{type(exc).__name__}: {exc}", category="internal_error")
            return JobStatus.FAILED, None, crash
        return JobStatus.SUCCEEDED, result, None

    def _record(
        self,
        ctx: JobContext,
        outcome: tuple[JobStatus | None, dict[str, Any] | None, JobError | None],
        *,
        duration: float,
    ) -> None:
        status, result, error = outcome
        duration_ms = round(duration * 1000, 1)
        if status is None or ctx.lost_event.is_set():
            log.warning("job_lease_lost_result_discarded", duration_ms=duration_ms)
            return
        job = ctx.job
        handler = self.handlers[job.kind]
        with transaction(self.session_factory) as session:
            if status is JobStatus.QUEUED and error is not None:
                new_status = queue.retry_or_fail(
                    session,
                    job.id,
                    self.worker_id,
                    error_category=error.category,
                    error_detail=str(error),
                    backoff=self.retry_backoff * job.attempts,
                )
                if new_status is JobStatus.FAILED:
                    handler.on_terminal_failure(
                        session, job, new_status, error.category, str(error)
                    )
                written = new_status is not None
                final = new_status
            else:
                written = queue.finish(
                    session,
                    job.id,
                    self.worker_id,
                    status,
                    result=result,
                    error_category=error.category if error else None,
                    error_detail=str(error) if error else None,
                )
                if written and status is not JobStatus.SUCCEEDED:
                    handler.on_terminal_failure(
                        session,
                        job,
                        status,
                        error.category if error else status.value,
                        str(error) if error else None,
                    )
                final = status
        if not written:
            log.warning("job_lease_lost_result_discarded", duration_ms=duration_ms)
            return
        log.info(
            "job_finished",
            status=final,
            duration_ms=duration_ms,
            error_category=error.category if error else None,
        )

    def _heartbeat(self, ctx: JobContext, done: threading.Event) -> None:
        interval = max(self.lease.total_seconds() / 3, 0.05)
        while not done.wait(interval):
            try:
                with transaction(self.session_factory) as session:
                    beat = queue.heartbeat(session, ctx.job.id, self.worker_id, self.lease)
            except Exception:
                log.exception("heartbeat_failed")
                continue  # the lease may still be valid; retry on the next tick
            if not beat.lease_held:
                log.warning("job_lease_lost")
                ctx.lost_event.set()
                return
            if beat.cancel_requested and not ctx.cancel_event.is_set():
                log.info("job_cancel_requested")
                ctx.cancel_event.set()
