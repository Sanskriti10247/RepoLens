"""Worker reliability: concurrency limits, outages and crashed workers."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from repolens.config import Settings
from repolens.domain.enums import JobKind, JobStatus
from repolens.execution.docker import DockerSandbox
from repolens.persistence import jobs as queue
from repolens.persistence.db import transaction
from repolens.persistence.models import Job, Repository
from repolens.services.jobs import JobContext
from repolens.worker.runtime import Worker
from tests.integration.conftest import AUTH, analyze, register
from tests.support.gitrepo import FixtureRepo

pytestmark = pytest.mark.integration
LEASE = timedelta(seconds=30)


def _repo(factory: sessionmaker[Session]) -> uuid.UUID:
    with transaction(factory) as session:
        repo = Repository(
            provider="github",
            host="github.com",
            owner="o",
            name=uuid.uuid4().hex[:8],
            canonical_url=f"https://github.com/o/{uuid.uuid4().hex}",
        )
        session.add(repo)
        session.flush()
        return repo.id


def _enqueue(
    factory: sessionmaker[Session], kind: JobKind, repo: uuid.UUID | None, priority: int = 100
) -> uuid.UUID:
    with transaction(factory) as session:
        job_id, _ = queue.enqueue(session, kind, repository_id=repo, priority=priority)
    return job_id


def _concurrent_claims(
    factory: sessionmaker[Session], threads: int, limits: queue.ClaimLimits
) -> list[queue.ClaimedJob]:
    claimed: list[queue.ClaimedJob] = []
    lock = threading.Lock()
    barrier = threading.Barrier(threads)

    def claim(n: int) -> None:
        barrier.wait()
        with transaction(factory) as session:
            job = queue.claim(session, f"w{n}", LEASE, limits=limits)
        if job is not None:
            with lock:
                claimed.append(job)

    workers = [threading.Thread(target=claim, args=(n,)) for n in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    return claimed


def test_per_kind_limit_holds_under_concurrency(session_factory: sessionmaker[Session]) -> None:
    for _ in range(10):
        _enqueue(session_factory, JobKind.RUN_TESTS, None)
    claimed = _concurrent_claims(
        session_factory, 8, queue.ClaimLimits(per_kind={JobKind.RUN_TESTS: 2})
    )
    assert len(claimed) == 2


def test_per_repository_limit_and_fair_skipping(session_factory: sessionmaker[Session]) -> None:
    busy, other = _repo(session_factory), _repo(session_factory)
    for _ in range(4):
        _enqueue(session_factory, JobKind.ANALYZE_REPOSITORY, busy, priority=1)
    other_job = _enqueue(session_factory, JobKind.ANALYZE_REPOSITORY, other, priority=9)
    claimed = _concurrent_claims(session_factory, 3, queue.ClaimLimits(per_repository=1))
    by_repo = sorted(str(c.repository_id) for c in claimed)
    assert by_repo == sorted([str(busy), str(other)])  # one each; the third claim gets nothing
    assert other_job in {c.id for c in claimed}  # not starved by the busy repository


def test_limits_free_up_when_jobs_finish(session_factory: sessionmaker[Session]) -> None:
    repo = _repo(session_factory)
    first = _enqueue(session_factory, JobKind.ANALYZE_REPOSITORY, repo)
    _enqueue(session_factory, JobKind.ANALYZE_REPOSITORY, repo)
    limits = queue.ClaimLimits(per_repository=1)
    with transaction(session_factory) as session:
        assert queue.claim(session, "w", LEASE, limits=limits) is not None
    with transaction(session_factory) as session:
        assert queue.claim(session, "w", LEASE, limits=limits) is None
        queue.start(session, first, "w")
        queue.finish(session, first, "w", JobStatus.SUCCEEDED)
    with transaction(session_factory) as session:
        assert queue.claim(session, "w", LEASE, limits=limits) is not None


class _FlakyFactory:
    """A session factory that fails like an unreachable database for the first calls."""

    def __init__(self, real: sessionmaker[Session], failures: int) -> None:
        self.real = real
        self.failures = failures

    def __call__(self) -> Session:
        if self.failures > 0:
            self.failures -= 1
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))
        return self.real()


def test_worker_loop_survives_database_outage(session_factory: sessionmaker[Session]) -> None:
    job_id = _enqueue(session_factory, JobKind.ANALYZE_REPOSITORY, None)
    done = threading.Event()

    class Handler:
        timeout = None

        def run(self, ctx: JobContext) -> dict[str, Any]:
            done.set()
            return {"ok": True}

        def on_terminal_failure(self, *args: Any) -> None:
            pass

    worker = Worker(
        _FlakyFactory(session_factory, failures=3),  # type: ignore[arg-type]
        {JobKind.ANALYZE_REPOSITORY: Handler()},  # type: ignore[dict-item]
        poll_interval=0.01,
        recovery_interval=3600,
    )
    stop = threading.Event()
    thread = threading.Thread(target=worker.run_forever, args=(stop,))
    thread.start()
    assert done.wait(timeout=10)
    stop.set()
    thread.join(timeout=10)
    with transaction(session_factory) as session:
        assert session.get(Job, job_id).status == JobStatus.SUCCEEDED  # type: ignore[union-attr]


@pytest.mark.docker
def test_killed_worker_is_recovered_without_false_results(
    client: TestClient,
    worker: Worker,
    settings: Settings,
    database_url: str,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    sandbox = DockerSandbox()
    if not sandbox.available() or not sandbox.image_present(settings.sandbox_image):
        pytest.skip("Docker or the sandbox image is unavailable")
    repo = FixtureRepo(tmp_path / "repos" / "slow")
    repo.commit(
        "slow", {"tests/test_slow.py": "import time\n\n\ndef test_slow():\n    time.sleep(6)\n"}
    )
    analysis = analyze(client, register(client, repo.url)["id"])
    assert worker.run_once()
    run = client.post(
        "/api/test-runs",
        json={"analysis_id": analysis["id"], "full_suite": True, "coverage": False},
        headers=AUTH,
    ).json()

    env = {
        **os.environ,
        "REPOLENS_DATABASE_URL": database_url,
        "REPOLENS_DATA_DIR": str(settings.data_dir),
        "REPOLENS_JOB_LEASE_SECONDS": "5",
        "REPOLENS_ALLOW_LOCAL_REPOSITORIES": "true",
        "REPOLENS_LOCAL_REPOSITORY_ROOTS": json.dumps(
            [str(p) for p in settings.local_repository_roots]
        ),
        "REPOLENS_LOG_FORMAT": "json",
    }
    child = subprocess.Popen(
        [sys.executable, "-m", "repolens.worker"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    container = f"repolens-run-{run['id']}"
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            listing = subprocess.run(
                ["docker", "ps", "--quiet", "--filter", f"name=^{container}$"],
                capture_output=True,
                check=True,
            )
            if listing.stdout.strip():
                break
            time.sleep(0.2)
        else:
            pytest.fail("the external worker never started the sandbox")
        os.killpg(child.pid, signal.SIGKILL)  # the worker dies mid-execution
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)

    mid = client.get(f"/api/test-runs/{run['id']}").json()
    assert mid["status"] == "RUNNING" and mid["outcome"] is None  # never a false PASSED

    time.sleep(5.5)  # let the dead worker's lease expire
    recovered = worker.recover()
    assert [r.status for r in recovered] == [JobStatus.QUEUED]
    assert worker.run_once()  # second attempt removes the orphan container and runs again

    final = client.get(f"/api/test-runs/{run['id']}").json()
    assert final["outcome"] == "PASSED", final
    assert final["job"]["attempts"] == 2
    leftover = subprocess.run(
        ["docker", "ps", "--all", "--quiet", "--filter", f"name=^{container}$"],
        capture_output=True,
        check=True,
    )
    assert leftover.stdout.strip() == b""
    with transaction(session_factory) as session:
        job = session.execute(
            select(Job).where(Job.subject_id == uuid.UUID(run["id"]))
        ).scalar_one()
        assert (job.status, job.attempts) == (JobStatus.SUCCEEDED, 2)
    metrics = client.get("/api/metrics/queue").json()
    run_tests = next(k for k in metrics["kinds"] if k["kind"] == "RUN_TESTS")
    assert run_tests["retried_jobs"] == 1 and run_tests["completed_last_24h"] == 1
    assert run_tests["median_queue_wait_ms"] is not None
    assert metrics["expired_leases"] == 0
