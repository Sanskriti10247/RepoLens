"""User mode: each user sees and acts only on the repositories they added.

The route list comes from the application's OpenAPI schema, so a new route that takes an identifier
fails here until it is covered.
"""

from __future__ import annotations

import shutil
import time
import uuid
from collections.abc import Iterator
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from repolens.api.app import create_app
from repolens.config import Settings
from repolens.persistence.models import AnalysisRun, RepositorySnapshot, TestRun
from repolens.worker.runtime import Worker
from tests.integration.conftest import AUTH, analyze, register
from tests.support.gitrepo import FixtureRepo

pytestmark = pytest.mark.integration

SUPABASE_URL = "http://127.0.0.1:9"  # never contacted: tokens below use the HS256 path
SECRET = "integration-legacy-secret-0123456789abcdef"
ALICE, BOB = uuid.uuid4(), uuid.uuid4()
PUBLIC = {"/api/health", "/api/ready"}
LISTS = {"/api/repositories", "/api/jobs", "/api/test-runs", "/api/metrics/queue"}


def bearer(user: uuid.UUID) -> dict[str, str]:
    claims = {
        "sub": str(user),
        "aud": "authenticated",
        "iss": f"{SUPABASE_URL}/auth/v1",
        "exp": int(time.time()) + 600,
    }
    return {"Authorization": f"Bearer {jwt.encode(claims, SECRET, algorithm='HS256')}"}


@pytest.fixture
def user_client(
    settings: Settings, engine: Engine, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    user_settings = settings.model_copy(
        update={"supabase_url": SUPABASE_URL, "supabase_jwt_secret": SecretStr(SECRET)}
    )
    with TestClient(create_app(user_settings, engine=engine)) as client:
        yield client


def _alices_data(
    client: TestClient,
    worker: Worker,
    inventory: FixtureRepo,
    session_factory: sessionmaker[Session],
) -> dict[str, Any]:
    inventory.git("checkout", "-q", "-B", "change", "base")
    inventory.commit("change", {"inventory/stock.py": "def restock():\n    return 1\n"})
    a = bearer(ALICE)
    repo = client.post("/api/repositories", json={"url": inventory.url}, headers=a).json()
    analysis = client.post(
        f"/api/repositories/{repo['id']}/analyses", json={"ref": "base"}, headers=a
    ).json()
    impact = client.post(
        f"/api/repositories/{repo['id']}/impact",
        json={"base": "base", "target": "change"},
        headers=a,
    ).json()
    while worker.run_once():
        pass
    assert client.get(f"/api/impact/{impact['id']}", headers=a).json()["status"] == "SUCCEEDED"
    plan = client.post(f"/api/impact/{impact['id']}/test-plan", json={}, headers=a).json()
    symbol = client.get(
        f"/api/repositories/{repo['id']}/symbols",
        params={"analysis_id": analysis["id"]},
        headers=a,
    ).json()["items"][0]
    files = client.get(f"/api/analyses/{analysis['id']}/files", headers=a).json()["items"]
    source_file = next(f for f in files if f["path"].endswith(".py"))
    job = client.get("/api/jobs", headers=a).json()["items"][0]
    with session_factory() as session, session.begin():
        analysis_run = session.get(AnalysisRun, uuid.UUID(plan["analysis_run_id"]))
        assert analysis_run is not None and analysis_run.snapshot_id is not None
        snapshot = session.get(RepositorySnapshot, analysis_run.snapshot_id)
        assert snapshot is not None
        run = TestRun(  # recorded directly: no sandbox is needed to check who may read it
            repository_id=uuid.UUID(repo["id"]),
            analysis_run_id=analysis_run.id,
            snapshot_id=snapshot.id,
            commit_sha=snapshot.commit_sha,
            plan_id=uuid.UUID(plan["id"]),
            mode="SELECTED",
            requested_node_ids=[],
            image="repolens-sandbox:test",
            limits={},
        )
        session.add(run)
        session.flush()
        run_id = run.id
    return {
        "repository_id": repo["id"],
        "analysis_id": analysis["id"],
        "impact_id": impact["id"],
        "plan_id": plan["id"],
        "run_id": str(run_id),
        "job_id": job["id"],
        "symbol_id": str(symbol["id"]),
        "file_id": str(source_file["id"]),
    }


def _routes(client: TestClient) -> list[tuple[str, str]]:
    paths = client.app.openapi()["paths"]  # type: ignore[attr-defined]
    return [
        (method.upper(), path)
        for path, operations in paths.items()
        if path not in PUBLIC
        for method in operations
    ]


def _fill(path: str, ids: dict[str, str]) -> str:
    for name in route_params(path):
        assert name in ids, f"{path}: add '{name}' to the ownership isolation test"
        path = path.replace("{" + name + "}", ids[name])
    return path


def route_params(path: str) -> list[str]:
    return [part[1:-1] for part in path.split("/") if part.startswith("{")]


BODY = {
    "ref": "HEAD",
    "base": "base",
    "target": "change",
    "question": "Who calls restock?",
    "policy": "CONSERVATIVE",
}


def test_users_never_see_each_others_data(
    user_client: TestClient,
    worker: Worker,
    inventory: FixtureRepo,
    session_factory: sessionmaker[Session],
) -> None:
    ids = _alices_data(user_client, worker, inventory, session_factory)
    a, b = bearer(ALICE), bearer(BOB)
    query = {"symbol_id": ids["symbol_id"], "from": ids["symbol_id"], "to": ids["symbol_id"]}
    routes = _routes(user_client)
    assert len(routes) > 30

    for method, template in routes:
        url = _fill(template, ids)
        if method == "GET" and route_params(template):
            # Sanity: the identifiers are real, so Bob's 404 below is about ownership.
            own = {**query, "analysis_id": ids["analysis_id"]}
            assert user_client.get(url, params=own, headers=a).status_code == 200, url
        if template in LISTS:
            continue
        if template == "/api/test-runs" and method == "POST":
            body: dict[str, Any] = {"plan_id": ids["plan_id"]}
        elif template == "/api/repositories" and method == "POST":
            continue  # registering is per user; checked below
        else:
            body = BODY
        response = user_client.request(method, url, params=query, json=body, headers=b)
        assert response.status_code == 404, (method, url, response.status_code, response.text)

    # Lists and metrics show Bob nothing of Alice's.
    for path in sorted(LISTS):
        response = user_client.get(path, headers=b)
        assert response.status_code == 200, path
        assert not any(value in response.text for value in ids.values() if len(value) > 8), path
    assert user_client.get("/api/repositories", headers=b).json()["page"]["total"] == 0
    metrics = user_client.get("/api/metrics/queue", headers=b).json()
    assert all(not k["by_status"] for k in metrics["kinds"])
    for path in ("/api/jobs", "/api/test-runs"):
        response = user_client.get(path, params={"repository_id": ids["repository_id"]}, headers=b)
        assert response.status_code == 404, path

    # Bob may add the same URL: he gets his own repository, and cannot reach Alice's results
    # through it.
    response = user_client.post("/api/repositories", json={"url": inventory.url}, headers=b)
    assert response.status_code == 201, response.text
    bobs = response.json()["id"]
    assert bobs != ids["repository_id"]
    for path, params in (
        (f"/api/repositories/{bobs}/symbols", {"analysis_id": ids["analysis_id"]}),
        (f"/api/repositories/{bobs}/files/{ids['file_id']}/source", {}),
    ):
        assert user_client.get(path, params=params, headers=b).status_code == 404, path
    response = user_client.post(
        f"/api/repositories/{bobs}/questions",
        json={"question": "Who calls restock?", "analysis_id": ids["analysis_id"]},
        headers=b,
    )
    assert response.status_code == 404
    response = user_client.post(
        "/api/test-runs", json={"analysis_id": ids["analysis_id"], "full_suite": True}, headers=b
    )
    assert response.status_code == 404
    assert user_client.get("/api/repositories", headers=a).json()["page"]["total"] == 1


def test_operator_token_is_not_accepted_in_user_mode(user_client: TestClient) -> None:
    response = user_client.get("/api/repositories", headers=AUTH)
    assert response.status_code == 401
    assert user_client.get("/api/health").status_code == 200


def test_missing_mirror_is_fetched_again(
    client: TestClient, worker: Worker, inventory: FixtureRepo, settings: Settings
) -> None:
    repo_id = register(client, inventory.url)["id"]
    analysis_id = analyze(client, repo_id)["id"]
    assert worker.run_once()
    files = client.get(f"/api/analyses/{analysis_id}/files").json()["items"]
    source = f"/api/repositories/{repo_id}/files/{files[0]['id']}/source"
    assert client.get(source).status_code == 200

    shutil.rmtree(settings.mirrors_dir / f"{repo_id}.git")  # e.g. a free-tier restart
    response = client.get(source)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "source_fetching"
    assert client.get(source).status_code == 503  # one fetch job, not one per request
    jobs = client.get("/api/jobs", params={"kind": "FETCH_MIRROR"}).json()["items"]
    assert len(jobs) == 1

    assert worker.run_once()
    assert client.get(f"/api/jobs/{jobs[0]['id']}").json()["status"] == "SUCCEEDED"
    assert client.get(source).status_code == 200
