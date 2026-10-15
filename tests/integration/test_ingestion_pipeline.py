"""Repository registration → analysis job → worker → snapshot, through the real API and DB."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from repolens.api.app import create_app
from repolens.config import Settings
from repolens.persistence.db import transaction
from repolens.persistence.models import Job, SourceFile
from repolens.worker.handlers import build_handlers
from repolens.worker.runtime import Worker
from tests.support.gitrepo import FixtureRepo

pytestmark = pytest.mark.integration
TOKEN = "integration-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def settings(tmp_path: Path, database_url: str) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=database_url,
        api_token=SecretStr(TOKEN),
        data_dir=tmp_path / "data",
        allow_local_repositories=True,
        local_repository_roots=[tmp_path / "repos"],
        min_free_disk_mb=0,
    )


@pytest.fixture
def client(
    settings: Settings, engine: Engine, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    with TestClient(create_app(settings, engine=engine)) as test_client:
        yield test_client


@pytest.fixture
def worker(settings: Settings, session_factory: sessionmaker[Session]) -> Worker:
    return Worker(
        session_factory,
        build_handlers(settings),
        worker_id="it-worker",
        retry_backoff=timedelta(0),
    )


@pytest.fixture
def source(tmp_path: Path) -> FixtureRepo:
    repo = FixtureRepo(tmp_path / "repos" / "demo")
    repo.commit(
        "initial",
        {
            "demo/__init__.py": "",
            "demo/core.py": "def answer():\n    return 42\n",
            "tests/test_core.py": "from demo.core import answer\n\n\ndef test_answer():\n"
            "    assert answer() == 42\n",
            "pyproject.toml": "[project]\nname = 'demo'\n",
            "README.md": "# demo\n",
        },
    )
    repo.symlink("link-out", "/etc/passwd")
    repo.commit("add link")
    return repo


def _register(client: TestClient, url: str) -> dict[str, Any]:
    response = client.post("/api/repositories", json={"url": url}, headers=AUTH)
    assert response.status_code in (200, 201), response.text
    return response.json()  # type: ignore[no-any-return]


def _analyze(client: TestClient, repo_id: str, ref: str = "HEAD") -> dict[str, Any]:
    response = client.post(f"/api/repositories/{repo_id}/analyses", json={"ref": ref}, headers=AUTH)
    assert response.status_code == 202, response.text
    return response.json()  # type: ignore[no-any-return]


def test_end_to_end_ingestion(
    client: TestClient,
    worker: Worker,
    source: FixtureRepo,
    session_factory: sessionmaker[Session],
) -> None:
    repo = _register(client, source.url)
    assert _register(client, source.url)["id"] == repo["id"]

    queued = _analyze(client, repo["id"])
    assert queued["status"] == "QUEUED" and queued["job"]["status"] == "QUEUED"
    assert _analyze(client, repo["id"])["id"] == queued["id"]  # in-flight request deduplicated

    assert worker.run_once()
    done = client.get(f"/api/analyses/{queued['id']}").json()
    assert done["status"] == "SUCCEEDED", done
    assert done["job"]["status"] == "SUCCEEDED"
    assert done["resolved_ref"] == "refs/heads/main" and done["resolved_ref_kind"] == "BRANCH"
    snapshot = done["snapshot"]
    assert snapshot["commit_sha"] == source.git("rev-parse", "HEAD")
    assert snapshot["file_count"] == 6
    assert snapshot["language_inventory"] == {"markdown": 1, "python": 3, "toml": 1}
    assert done["counts"]["files"] == 6
    assert set(done["timings_ms"]) >= {"fetch_ms", "snapshot_ms"}

    with transaction(session_factory) as session:
        roles = dict(
            session.execute(
                select(SourceFile.path, SourceFile.role).where(
                    SourceFile.snapshot_id == snapshot["id"]
                )
            ).all()
        )
    assert roles["tests/test_core.py"] == "TEST" and roles["link-out"] == "OTHER"

    listing = client.get("/api/repositories").json()
    assert listing["items"][0]["latest_analysis"]["status"] == "SUCCEEDED"
    assert listing["items"][0]["default_branch"] == "main"


def test_same_commit_is_reused(client: TestClient, worker: Worker, source: FixtureRepo) -> None:
    repo = _register(client, source.url)
    first = _analyze(client, repo["id"], "main")
    worker.run_once()
    second = _analyze(client, repo["id"], source.git("rev-parse", "HEAD"))
    assert second["id"] != first["id"]
    worker.run_once()
    reused = client.get(f"/api/analyses/{second['id']}").json()
    assert reused["status"] == "REUSED"
    assert reused["reused_run_id"] == first["id"] == reused["results_run_id"]
    assert (
        reused["snapshot"]["id"]
        == client.get(f"/api/analyses/{first['id']}").json()["snapshot"]["id"]
    )


def test_new_commit_creates_new_snapshot(
    client: TestClient, worker: Worker, source: FixtureRepo
) -> None:
    repo = _register(client, source.url)
    first = _analyze(client, repo["id"])
    worker.run_once()
    source.commit("more", {"demo/extra.py": "X = 1\n"})
    second = _analyze(client, repo["id"])
    worker.run_once()
    a = client.get(f"/api/analyses/{first['id']}").json()
    b = client.get(f"/api/analyses/{second['id']}").json()
    assert b["status"] == "SUCCEEDED"
    assert a["snapshot"]["commit_sha"] != b["snapshot"]["commit_sha"]
    assert b["snapshot"]["file_count"] == a["snapshot"]["file_count"] + 1


def test_missing_revision_fails_without_retry(
    client: TestClient, worker: Worker, source: FixtureRepo
) -> None:
    repo = _register(client, source.url)
    run = _analyze(client, repo["id"], "no-such-branch")
    worker.run_once()
    failed = client.get(f"/api/analyses/{run['id']}").json()
    assert failed["status"] == "FAILED"
    assert failed["error"].startswith("revision_not_resolved")
    assert failed["job"]["attempts"] == 1 and failed["job"]["status"] == "FAILED"


def test_cancel_queued_analysis(client: TestClient, worker: Worker, source: FixtureRepo) -> None:
    repo = _register(client, source.url)
    run = _analyze(client, repo["id"])
    cancelled = client.post(f"/api/analyses/{run['id']}/cancel", headers=AUTH).json()
    assert cancelled["status"] == "CANCELLED" and cancelled["job"]["status"] == "CANCELLED"
    assert not worker.run_once()


def test_crashed_worker_marks_run_failed_after_retries(
    client: TestClient,
    worker: Worker,
    source: FixtureRepo,
    session_factory: sessionmaker[Session],
) -> None:
    repo = _register(client, source.url)
    run = _analyze(client, repo["id"])
    with transaction(session_factory) as session:
        session.execute(update(Job).values(max_attempts=1))
        # A worker claims the job and dies without heartbeating.
        session.execute(
            update(Job).values(
                status="RUNNING",
                attempts=1,
                lease_owner="dead",
                lease_expires_at=func.now() - timedelta(minutes=5),
            )
        )
    worker.recover()
    failed = client.get(f"/api/analyses/{run['id']}").json()
    assert failed["status"] == "FAILED" and failed["error"].startswith("lease_expired")


def test_api_validation_and_auth(client: TestClient, source: FixtureRepo) -> None:
    assert client.post("/api/repositories", json={"url": source.url}).status_code == 401
    bad = client.post("/api/repositories", json={"url": "https://evil.com/a/b"}, headers=AUTH)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_repository_url"
    repo = _register(client, source.url)
    bad_ref = client.post(
        f"/api/repositories/{repo['id']}/analyses", json={"ref": "--upload-pack=x"}, headers=AUTH
    )
    assert bad_ref.status_code == 422 and bad_ref.json()["error"]["code"] == "invalid_ref"
    missing = client.get("/api/repositories/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404
    assert client.get("/api/repositories/not-a-uuid").status_code == 422
    assert client.get("/api/repositories?limit=1000").status_code == 422


def test_local_repositories_rejected_when_disabled(
    engine: Engine, database_url: str, source: FixtureRepo
) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=database_url,
        api_token=SecretStr(TOKEN),
    )
    with TestClient(create_app(settings, engine=engine)) as client:
        response = client.post("/api/repositories", json={"url": source.url}, headers=AUTH)
    assert response.status_code == 422
