"""Live analysis progress: what the worker writes, when, and what the API returns."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from repolens.persistence.db import transaction
from repolens.persistence.models import AnalysisProgress, AnalysisRun
from repolens.services import analysis as analysis_service
from repolens.services.progress import ANALYSIS_STAGES, ProgressReporter
from repolens.worker.runtime import Worker
from tests.integration.conftest import analyze, register
from tests.support.gitrepo import FixtureRepo

pytestmark = pytest.mark.integration


@pytest.fixture
def source(tmp_path: Path) -> FixtureRepo:
    repo = FixtureRepo(tmp_path / "repos" / "demo")
    repo.commit(
        "initial",
        {
            "demo/__init__.py": "",
            "demo/core.py": "def answer():\n    return 42\n",
            "demo/cli.py": "from demo.core import answer\n\n\ndef main():\n    print(answer())\n",
            "tests/test_core.py": "from demo.core import answer\n\n\ndef test_answer():\n"
            "    assert answer() == 42\n",
            "README.md": "# demo\n",
        },
    )
    return repo


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _stored(factory: sessionmaker[Session], run_id: uuid.UUID) -> dict[str, Any] | None:
    with transaction(factory) as session:
        return session.execute(
            select(AnalysisProgress.progress).where(AnalysisProgress.analysis_run_id == run_id)
        ).scalar_one_or_none()


def _queued_run(client: TestClient, source: FixtureRepo) -> uuid.UUID:
    repo = register(client, source.url)
    return uuid.UUID(analyze(client, repo["id"])["id"])


def test_queued_analysis_has_no_progress(client: TestClient, source: FixtureRepo) -> None:
    repo = register(client, source.url)
    queued = analyze(client, repo["id"])
    assert queued["progress"] is None
    assert client.get(f"/api/analyses/{queued['id']}").json()["progress"] is None


def test_completed_analysis_keeps_a_final_snapshot(
    client: TestClient,
    worker: Worker,
    source: FixtureRepo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered: list[str] = []

    class Recording(ProgressReporter):
        def stage(self, name: str, **kwargs: Any) -> None:
            entered.append(name)
            super().stage(name, **kwargs)

    monkeypatch.setattr(analysis_service, "ProgressReporter", Recording)
    run_id = _queued_run(client, source)
    assert worker.run_once()

    done = client.get(f"/api/analyses/{run_id}").json()
    assert done["status"] == "SUCCEEDED", done
    assert entered == list(ANALYSIS_STAGES)
    progress = done["progress"]
    assert progress["stage"] == "done"
    assert progress["stages"] == list(ANALYSIS_STAGES)
    assert progress["stage_index"] == progress["stage_count"] == len(ANALYSIS_STAGES)
    assert set(progress["durations_ms"]) == set(ANALYSIS_STAGES)
    assert all(ms >= 0 for ms in progress["durations_ms"].values())
    assert progress["current"] is None
    # The test-discovery stage is timed separately from parsing and persisting.
    assert "tests_ms" in done["counts"]["analysis_timings_ms"]


def test_reused_analysis_finishes_after_the_snapshot_stage(
    client: TestClient, worker: Worker, source: FixtureRepo
) -> None:
    repo = register(client, source.url)
    analyze(client, repo["id"])
    worker.run_once()
    second = analyze(client, repo["id"], "main")  # same commit, different ref
    worker.run_once()
    reused = client.get(f"/api/analyses/{second['id']}").json()
    assert reused["status"] == "REUSED"
    assert reused["progress"]["stage"] == "done"
    assert set(reused["progress"]["durations_ms"]) == {"fetch", "snapshot"}
    assert "reused" in reused["progress"]["detail"]


def test_failed_analysis_keeps_the_stage_it_stopped_in(
    client: TestClient, worker: Worker, source: FixtureRepo
) -> None:
    repo = register(client, source.url)
    run = analyze(client, repo["id"], "no-such-branch")
    worker.run_once()
    failed = client.get(f"/api/analyses/{run['id']}").json()
    assert failed["status"] == "FAILED"
    assert failed["progress"]["stage"] == "fetch"
    assert failed["progress"]["stage_index"] == 0


def test_updates_inside_a_stage_are_throttled(
    client: TestClient, source: FixtureRepo, session_factory: sessionmaker[Session]
) -> None:
    run_id = _queued_run(client, source)
    clock = FakeClock()
    reporter = ProgressReporter(session_factory, run_id, interval=1.0, clock=clock)

    reporter.stage("parse", total=40)
    assert reporter.writes == 1  # stage boundaries are written at once
    for done in range(1, 12):
        clock.now += 0.25
        reporter.advance(done, current=f"pkg/mod_{done}.py")
    # 2.75 simulated seconds at one write per second at most.
    assert reporter.writes == 3
    stored = _stored(session_factory, run_id)
    assert stored is not None
    assert stored["stage"] == "parse" and stored["files_total"] == 40
    assert stored["files_done"] == 8 and stored["current"] == "pkg/mod_8.py"

    clock.now += 0.5
    reporter.stage("resolve")
    stored = _stored(session_factory, run_id)
    assert stored is not None
    assert stored["stage"] == "resolve" and stored["stage_index"] == 3
    assert stored["durations_ms"] == {"parse": 3250.0}
    assert stored["files_done"] is None and stored["files_total"] is None


def test_progress_is_written_while_the_run_row_is_locked(
    client: TestClient, source: FixtureRepo, session_factory: sessionmaker[Session]
) -> None:
    run_id = _queued_run(client, source)
    reporter = ProgressReporter(session_factory, run_id, interval=0)
    reporter.stage("parse", total=3)  # creates the row before the lock, as the pipeline does

    # The analysis transaction holds the run row FOR UPDATE while it parses and persists.
    with transaction(session_factory) as analysis:
        analysis.get(AnalysisRun, run_id, with_for_update=True)
        reporter.advance(2, current="demo/core.py")
        stored = _stored(session_factory, run_id)
    assert reporter.writes == 2
    assert stored is not None and stored["files_done"] == 2


def test_a_failed_write_does_not_interrupt_the_job(
    session_factory: sessionmaker[Session],
) -> None:
    reporter = ProgressReporter(session_factory, uuid.uuid4())  # no such run: FK violation
    reporter.stage("fetch")
    reporter.finish()
    assert reporter.writes == 0


def test_long_paths_are_shortened(
    client: TestClient, source: FixtureRepo, session_factory: sessionmaker[Session]
) -> None:
    run_id = _queued_run(client, source)
    reporter = ProgressReporter(session_factory, run_id)
    reporter.stage("snapshot", current="a/" * 400 + "end.py")
    stored = _stored(session_factory, run_id)
    assert stored is not None
    assert len(stored["current"]) == 300
    assert stored["current"].endswith("end.py") and stored["current"].startswith("…")


def test_unknown_stage_is_a_programming_error(session_factory: sessionmaker[Session]) -> None:
    reporter = ProgressReporter(session_factory, uuid.uuid4())
    with pytest.raises(ValueError, match="unknown stage"):
        reporter.stage("compile")
