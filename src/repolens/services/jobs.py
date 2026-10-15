"""The contract between job handlers (services) and the worker runtime."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol

from sqlalchemy.orm import Session, sessionmaker

from repolens.domain.enums import JobStatus
from repolens.persistence import jobs as queue


class JobError(Exception):
    category = "job_error"

    def __init__(self, message: str, *, category: str | None = None) -> None:
        super().__init__(message)
        if category is not None:
            self.category = category


class RetryableJobError(JobError):
    category = "infrastructure_error"


class PermanentJobError(JobError):
    category = "permanent_error"


class PolicyBlockedError(JobError):
    category = "policy_blocked"


class JobCancelled(JobError):
    category = "cancelled"


class LeaseLost(JobError):
    category = "lease_lost"


class JobTimedOut(JobError):
    category = "timed_out"


@dataclass
class JobContext:
    job: queue.ClaimedJob
    worker_id: str
    session_factory: sessionmaker[Session]
    deadline: float | None = None  # time.monotonic() value
    cancel_event: threading.Event = field(default_factory=threading.Event)
    lost_event: threading.Event = field(default_factory=threading.Event)

    def checkpoint(self) -> None:
        """Cooperative interruption point; handlers call this between units of work."""
        if self.lost_event.is_set():
            raise LeaseLost("job lease lost")
        if self.cancel_event.is_set():
            raise JobCancelled("cancellation requested")
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise JobTimedOut("job exceeded its time limit")


class JobHandler(Protocol):
    timeout: timedelta | None

    def run(self, ctx: JobContext) -> dict[str, Any] | None: ...

    def on_terminal_failure(
        self,
        session: Session,
        job: queue.ClaimedJob | queue.RecoveredJob,
        status: JobStatus,
        category: str,
        detail: str | None,
    ) -> None:
        """Bring the job's subject (e.g. an analysis run) into a matching terminal state."""
        ...
