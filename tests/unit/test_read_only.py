"""REPOLENS_READ_ONLY: every mutation is refused and nothing needs a worker or Docker."""

import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from repolens.api.app import create_app
from repolens.config import Settings

UNREACHABLE_DB = "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none"
ZERO_ID = "00000000-0000-0000-0000-000000000000"


def _client(*, read_only: bool, token: str | None) -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=UNREACHABLE_DB,
        read_only=read_only,
        api_token=SecretStr(token) if token else None,
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield from _client(read_only=True, token=None)


def _mutating_routes(client: TestClient) -> list[tuple[str, str]]:
    paths = client.app.openapi()["paths"]  # type: ignore[attr-defined]
    return [
        (method.upper(), re.sub(r"\{[^}]+\}", ZERO_ID, path))
        for path, operations in paths.items()
        for method in operations
        if method.upper() not in {"GET", "HEAD", "OPTIONS"}
    ]


def test_the_app_has_mutating_routes_to_check(client: TestClient) -> None:
    assert len(_mutating_routes(client)) >= 8


def test_every_mutating_route_is_refused(client: TestClient) -> None:
    for method, path in _mutating_routes(client):
        response = client.request(method, path, json={})
        assert response.status_code == 403, (method, path, response.text)
        error = response.json()["error"]
        assert error["code"] == "read_only"
        assert error["request_id"] == response.headers["X-Request-ID"]


def test_a_valid_token_does_not_unlock_mutations() -> None:
    for client in _client(read_only=True, token="t0ken-value"):
        for method, path in _mutating_routes(client):
            response = client.request(
                method, path, json={}, headers={"Authorization": "Bearer t0ken-value"}
            )
            assert response.status_code == 403, (method, path)
            assert response.json()["error"]["code"] == "read_only"


def test_refusal_wins_over_request_validation(client: TestClient) -> None:
    response = client.post("/api/repositories", content="not json")
    assert response.status_code == 403


def test_health_reports_read_only_without_worker_or_database(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["read_only"] is True


def test_starts_without_a_token(client: TestClient) -> None:
    assert client.app.state.settings.api_token is None  # type: ignore[attr-defined]


def test_normal_mode_is_unchanged() -> None:
    for client in _client(read_only=False, token=None):
        assert client.get("/api/health").json()["read_only"] is False
        response = client.post("/api/repositories", json={"url": "https://github.com/a/b"})
        assert response.json()["error"]["code"] == "mutations_disabled"
    for client in _client(read_only=False, token="t0ken-value"):
        response = client.post("/api/repositories", json={"url": "https://github.com/a/b"})
        assert response.status_code == 401
