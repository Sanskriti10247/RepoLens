"""Fixtures for API + worker integration tests (real PostgreSQL, real git)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from repolens.api.app import create_app
from repolens.config import Settings
from repolens.worker.handlers import build_handlers
from repolens.worker.runtime import Worker
from tests.support import projects
from tests.support.gitrepo import FixtureRepo

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


def register(client: TestClient, url: str) -> dict[str, Any]:
    response = client.post("/api/repositories", json={"url": url}, headers=AUTH)
    assert response.status_code in (200, 201), response.text
    return response.json()  # type: ignore[no-any-return]


def analyze(client: TestClient, repo_id: str, ref: str = "HEAD") -> dict[str, Any]:
    response = client.post(f"/api/repositories/{repo_id}/analyses", json={"ref": ref}, headers=AUTH)
    assert response.status_code == 202, response.text
    return response.json()  # type: ignore[no-any-return]


@pytest.fixture
def inventory(tmp_path: Path) -> FixtureRepo:
    repo = FixtureRepo(tmp_path / "repos" / "inventory")
    repo.commit("base", projects.INVENTORY)
    repo.tag("base")
    return repo


def run_change(
    client: TestClient, worker: Worker, repo: FixtureRepo, change: dict[str, str]
) -> str:
    """Commit ``change`` on a branch from ``base`` and run a completed impact analysis."""
    repo.git("checkout", "-q", "-B", "change", "base")
    repo.commit("change", change)
    repo_id = register(client, repo.url)["id"]
    response = client.post(
        f"/api/repositories/{repo_id}/impact",
        json={"base": "base", "target": "change"},
        headers=AUTH,
    )
    assert response.status_code == 202, response.text
    impact_id: str = response.json()["id"]
    assert worker.run_once()
    status = client.get(f"/api/impact/{impact_id}").json()
    assert status["status"] == "SUCCEEDED", status
    return impact_id


def run_impact(
    client: TestClient, worker: Worker, repo: FixtureRepo, change: dict[str, str]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    impact_id = run_change(client, worker, repo, change)
    report = client.get(f"/api/impact/{impact_id}/report").json()
    items = client.get(f"/api/impact/{impact_id}/items", params={"limit": 100}).json()["items"]
    return report, {i["qualified_name"]: i for i in items}
