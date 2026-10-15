from collections.abc import Iterator

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from pydantic import SecretStr

from repolens.api.app import create_app
from repolens.api.dependencies import Authorized
from repolens.config import Settings

UNREACHABLE_DB = "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none"


def _client(token: str | None = "t0ken-value") -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=UNREACHABLE_DB,
        api_token=SecretStr(token) if token else None,
    )
    app = create_app(settings)
    router = APIRouter()

    @router.post("/api/_probe", dependencies=[Authorized])
    def probe() -> dict[str, str]:
        return {"ok": "yes"}

    @router.get("/api/_boom")
    def boom() -> None:
        raise RuntimeError("secret internals")

    app.include_router(router)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield from _client()


def test_health_reports_version(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_request_id_is_generated_or_propagated(client: TestClient) -> None:
    generated = client.get("/api/health").headers["X-Request-ID"]
    assert len(generated) == 32
    echoed = client.get("/api/health", headers={"X-Request-ID": "abc12345-req"})
    assert echoed.headers["X-Request-ID"] == "abc12345-req"
    rejected = client.get("/api/health", headers={"X-Request-ID": "bad id\nwith newline"})
    assert rejected.headers["X-Request-ID"] != "bad id\nwith newline"


def test_ready_is_503_when_database_unreachable(client: TestClient) -> None:
    response = client.get("/api/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"][0] == {"name": "database", "ok": False, "detail": "OperationalError"}


def test_mutations_require_valid_token(client: TestClient) -> None:
    assert client.post("/api/_probe").status_code == 401
    wrong = client.post("/api/_probe", headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "unauthorized"
    ok = client.post("/api/_probe", headers={"Authorization": "Bearer t0ken-value"})
    assert ok.status_code == 200


def test_mutations_disabled_without_configured_token() -> None:
    for client in _client(token=None):
        response = client.post("/api/_probe", headers={"Authorization": "Bearer anything"})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "mutations_disabled"


def test_unhandled_errors_do_not_leak_details(client: TestClient) -> None:
    response = client.get("/api/_boom")
    assert response.status_code == 500
    assert "secret internals" not in response.text
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]


def test_unknown_route_uses_error_envelope(client: TestClient) -> None:
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
