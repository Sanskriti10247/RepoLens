"""REPOLENS_SANDBOX_ENABLED=false: test runs and collection are refused, nothing calls Docker."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from repolens.api.app import create_app
from repolens.config import Settings
from repolens.services import execution

UNREACHABLE_DB = "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none"
ZERO_ID = "00000000-0000-0000-0000-000000000000"
AUTH = {"Authorization": "Bearer t0ken-value"}


def _settings(*, sandbox: bool, **extra: object) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=UNREACHABLE_DB,
        api_token=SecretStr("t0ken-value"),
        sandbox_enabled=sandbox,
        **extra,  # type: ignore[arg-type]
    )


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(_settings(sandbox=False)), raise_server_exceptions=False) as c:
        yield c


def test_health_reports_the_sandbox(client: TestClient) -> None:
    assert client.get("/api/health").json()["sandbox"] is False
    with TestClient(create_app(_settings(sandbox=True))) as enabled:
        assert enabled.get("/api/health").json()["sandbox"] is True


def test_test_runs_are_refused(client: TestClient) -> None:
    response = client.post("/api/test-runs", json={"plan_id": ZERO_ID}, headers=AUTH)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "sandbox_unavailable"
    assert "sandbox" in error["message"]


def test_collection_is_refused(client: TestClient) -> None:
    response = client.post(f"/api/analyses/{ZERO_ID}/collect", headers=AUTH)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "sandbox_unavailable"


def test_the_token_is_still_checked_first(client: TestClient) -> None:
    response = client.post("/api/test-runs", json={"plan_id": ZERO_ID})
    assert response.status_code == 401


def test_startup_cleanup_never_runs_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_docker(*_: object) -> list[str]:
        raise AssertionError("docker was called")

    monkeypatch.setattr(execution.DockerSandbox, "remove_stale", _no_docker)
    settings = _settings(sandbox=False, data_dir=tmp_path)
    stale = tmp_path / "workspaces" / "old"
    stale.mkdir(parents=True)
    execution.cleanup_stale_execution(settings, older_than_seconds=-1)
    assert not stale.exists()
