"""Job state machine.

QUEUED → CLAIMED → RUNNING → SUCCEEDED, with FAILED / CANCELLED / TIMED_OUT / BLOCKED as the
other terminal states. A claimed or running job may return to QUEUED only through lease
expiry or a retryable failure. Terminal states are final.
"""

from __future__ import annotations

from repolens.domain.enums import JobStatus

TERMINAL_JOB_STATUSES: frozenset[JobStatus] = frozenset(
    {
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.TIMED_OUT,
        JobStatus.BLOCKED,
    }
)
ACTIVE_JOB_STATUSES: frozenset[JobStatus] = frozenset({JobStatus.CLAIMED, JobStatus.RUNNING})

_ALLOWED: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.CLAIMED, JobStatus.CANCELLED, JobStatus.BLOCKED}),
    JobStatus.CLAIMED: frozenset(
        {JobStatus.RUNNING, JobStatus.QUEUED, JobStatus.CANCELLED, JobStatus.FAILED}
    ),
    JobStatus.RUNNING: frozenset(
        {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.TIMED_OUT,
            JobStatus.BLOCKED,
            JobStatus.QUEUED,
        }
    ),
}


def allowed_transitions(current: JobStatus) -> frozenset[JobStatus]:
    return _ALLOWED.get(current, frozenset())


def can_transition(current: JobStatus, target: JobStatus) -> bool:
    return target in allowed_transitions(current)


def sources_for(target: JobStatus) -> frozenset[JobStatus]:
    """All states from which ``target`` may be entered (used in conditional UPDATEs)."""
    return frozenset(state for state, targets in _ALLOWED.items() if target in targets)
