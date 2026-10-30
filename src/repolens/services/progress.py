"""Live progress of a running analysis, for the web app's process view.

The worker reports where it is (stage, files done, the file being read) through a
``ProgressReporter``. Stage boundaries are written straight away; updates inside a stage are
throttled to one write per ``interval`` seconds. Every write is its own short transaction
against the one-row ``analysis_progress`` table, so it never joins or waits on the analysis
transaction, and a failed write is logged and skipped: progress is a view, not a result.

Only the latest snapshot is kept. When the run finishes, the reporter writes a final ``done``
snapshot; when it fails, the last snapshot stays and shows the stage it stopped in.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from sqlalchemy import CursorResult, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from repolens.observability.logging import get_logger
from repolens.persistence.db import transaction
from repolens.persistence.models import AnalysisProgress

log = get_logger(__name__)

# The stages of a repository analysis, in order (see AnalyzeRepositoryHandler):
# fetch     vet the repository with its provider, update the bare mirror, resolve the ref
# snapshot  list and hash the files of the commit (skipped when the snapshot exists)
# parse     parse every Python file (parse cache hits included)
# resolve   resolve names across the snapshot into references and edges
# persist   bulk-write symbols, references, edges, file outcomes and warnings
# tests     discover pytest tests statically
ANALYSIS_STAGES: tuple[str, ...] = ("fetch", "snapshot", "parse", "resolve", "persist", "tests")
DONE = "done"
DEFAULT_INTERVAL_SECONDS = 0.75
MAX_CURRENT_CHARS = 300
# A progress write must never hold up the job; give up quickly if the row is busy.
_LOCK_TIMEOUT = "2s"


class ProgressSink(Protocol):
    """What analysis code needs from a progress reporter."""

    def stage(
        self,
        name: str,
        *,
        total: int | None = None,
        current: str | None = None,
        detail: str | None = None,
    ) -> None: ...

    def advance(
        self,
        done: int | None = None,
        *,
        total: int | None = None,
        current: str | None = None,
        detail: str | None = None,
    ) -> None: ...

    def finish(self, *, detail: str | None = None) -> None: ...


class NullProgress:
    """A sink that records nothing (analyses run outside a worker, tests)."""

    def stage(
        self,
        name: str,
        *,
        total: int | None = None,
        current: str | None = None,
        detail: str | None = None,
    ) -> None:
        return None

    def advance(
        self,
        done: int | None = None,
        *,
        total: int | None = None,
        current: str | None = None,
        detail: str | None = None,
    ) -> None:
        return None

    def finish(self, *, detail: str | None = None) -> None:
        return None


NULL_PROGRESS = NullProgress()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class ProgressReporter:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        run_id: uuid.UUID,
        *,
        stages: Sequence[str] = ANALYSIS_STAGES,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session_factory = session_factory
        self.run_id = run_id
        self.stages = tuple(stages)
        self.interval = interval
        self.clock = clock
        self.writes = 0
        self._stage: str | None = None
        self._stage_started = 0.0
        self._stage_started_at: str | None = None
        self._durations: dict[str, float] = {}
        self._done: int | None = None
        self._total: int | None = None
        self._current: str | None = None
        self._detail: str | None = None
        self._last_write: float | None = None

    # -- reporting -----------------------------------------------------------------------

    def stage(
        self,
        name: str,
        *,
        total: int | None = None,
        current: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Enter stage ``name``: closes the previous stage's clock and writes immediately."""
        if name not in self.stages:
            raise ValueError(f"unknown stage {name!r}")
        self._close_stage()
        self._stage = name
        self._stage_started = self.clock()
        self._stage_started_at = _now_iso()
        self._done = 0 if total is not None else None
        self._total = total
        self._current = _clip(current)
        self._detail = detail
        self._write()

    def advance(
        self,
        done: int | None = None,
        *,
        total: int | None = None,
        current: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Record progress inside the current stage; written at most once per interval."""
        if total is not None:
            self._total = total
        if done is not None:
            self._done = done
        if current is not None:
            self._current = _clip(current)
        if detail is not None:
            self._detail = detail
        last = self._last_write
        if last is None or self.clock() - last >= self.interval:
            self._write()

    def finish(self, *, detail: str | None = None) -> None:
        """Write the final snapshot once the run's results are committed."""
        self._close_stage()
        self._stage = DONE
        self._stage_started_at = _now_iso()
        self._done = self._total
        self._current = None
        self._detail = detail
        self._write()

    # -- internals -----------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        stage = self._stage or self.stages[0]
        index = len(self.stages) if stage == DONE else self.stages.index(stage)
        return {
            "stage": stage,
            "stage_index": index,
            "stage_count": len(self.stages),
            "stages": list(self.stages),
            "files_done": self._done,
            "files_total": self._total,
            "current": self._current,
            "detail": self._detail,
            "stage_started_at": self._stage_started_at,
            "durations_ms": dict(self._durations),
            "updated_at": _now_iso(),
        }

    def _close_stage(self) -> None:
        if self._stage is not None and self._stage != DONE:
            elapsed = (self.clock() - self._stage_started) * 1000
            self._durations[self._stage] = round(elapsed, 1)

    def _write(self) -> None:
        document = self.snapshot()
        self._last_write = self.clock()
        try:
            with transaction(self.session_factory) as session:
                session.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
                # Update first: once the row exists this never checks the foreign key, so
                # it does not wait on the run row the analysis transaction holds.
                updated = cast(
                    CursorResult[Any],
                    session.execute(
                        update(AnalysisProgress)
                        .where(AnalysisProgress.analysis_run_id == self.run_id)
                        .values(progress=document, updated_at=datetime.now(UTC))
                    ),
                )
                if updated.rowcount == 0:
                    session.execute(
                        pg_insert(AnalysisProgress)
                        .values(analysis_run_id=self.run_id, progress=document)
                        .on_conflict_do_update(
                            index_elements=[AnalysisProgress.analysis_run_id],
                            set_={"progress": document, "updated_at": datetime.now(UTC)},
                        )
                    )
            self.writes += 1
        except SQLAlchemyError as exc:
            log.warning(
                "progress_write_failed", run_id=str(self.run_id), error_type=type(exc).__name__
            )


def _clip(value: str | None) -> str | None:
    if value is None or len(value) <= MAX_CURRENT_CHARS:
        return value
    return "…" + value[-(MAX_CURRENT_CHARS - 1) :]
