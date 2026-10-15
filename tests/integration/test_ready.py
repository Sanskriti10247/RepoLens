import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from repolens.api.app import create_app
from repolens.config import Settings

pytestmark = pytest.mark.integration


def test_ready_when_database_is_migrated(engine: Engine, database_url: str) -> None:
    settings = Settings(_env_file=None, database_url=database_url)  # type: ignore[call-arg]
    with TestClient(create_app(settings, engine=engine)) as client:
        response = client.get("/api/ready")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ready"
