"""Sandboxed execution of selected and full test suites (requires Docker + sandbox image)."""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from repolens.config import Settings
from repolens.execution.docker import DockerSandbox
from repolens.worker.handlers import build_handlers
from repolens.worker.runtime import Worker
from tests.integration.conftest import AUTH, analyze, register, run_change
from tests.support import projects
from tests.support.gitrepo import FixtureRepo

pytestmark = [pytest.mark.integration, pytest.mark.docker]


@pytest.fixture(autouse=True)
def require_sandbox(settings: Settings) -> None:
    sandbox = DockerSandbox()
    if not sandbox.available() or not sandbox.image_present(settings.sandbox_image):
        if os.environ.get("REPOLENS_REQUIRE_DOCKER") == "1":
            pytest.fail("Docker sandbox required but unavailable")
        pytest.skip("Docker or the sandbox image is unavailable")


def plan(client: TestClient, impact_id: str) -> dict[str, Any]:
    response = client.post(f"/api/impact/{impact_id}/test-plan", json={}, headers=AUTH)
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


def execute(client: TestClient, worker: Worker, **body: Any) -> dict[str, Any]:
    response = client.post("/api/test-runs", json=body, headers=AUTH)
    assert response.status_code == 202, response.text
    run = response.json()
    assert run["status"] == "QUEUED"
    assert worker.run_once()
    finished = client.get(f"/api/test-runs/{run['id']}").json()
    assert finished["status"] == "COMPLETED", finished
    return finished  # type: ignore[no-any-return]


def results(client: TestClient, run_id: str) -> dict[str, str]:
    page = client.get(f"/api/test-runs/{run_id}/results", params={"limit": 100}).json()
    return {r["node_id"]: r["outcome"] for r in page["items"]}


def test_selected_tests_pass_with_results_and_coverage(
    client: TestClient, worker: Worker, inventory: FixtureRepo
) -> None:
    impact_id = run_change(client, worker, inventory, projects.CHANGE_WITH_TAX_BODY)
    test_plan = plan(client, impact_id)
    run = execute(client, worker, plan_id=test_plan["id"])
    assert run["outcome"] == "PASSED", run
    assert run["mode"] == "SELECTED" and run["counts"]["passed"] == 3
    assert run["job"]["status"] == "SUCCEEDED" and run["cleanup_ok"] is True
    assert results(client, run["id"]) == {
        "tests/test_pricing.py::test_with_tax": "PASSED",
        "tests/test_pricing.py::test_quote": "PASSED",
        "tests/test_reports.py::test_summary": "PASSED",
    }
    coverage = client.get(f"/api/test-runs/{run['id']}/coverage").json()
    covered = {f["path"]: f for f in coverage["files"]}
    assert covered["inventory/pricing.py"]["lines_covered"] >= 2
    assert "inventory/util.py" not in covered or covered["inventory/util.py"]["tests"] <= 1
    logs = client.get(f"/api/test-runs/{run['id']}/logs").json()
    assert "3 passed" in logs["stdout_tail"]


def test_selected_run_catches_regression_like_full_suite(
    client: TestClient, worker: Worker, inventory: FixtureRepo
) -> None:
    impact_id = run_change(client, worker, inventory, projects.REGRESSION_WITH_TAX)
    test_plan = plan(client, impact_id)
    selected = execute(client, worker, plan_id=test_plan["id"])
    assert selected["outcome"] == "FAILED"
    selected_failures = {n for n, o in results(client, selected["id"]).items() if o == "FAILED"}

    target_run = client.get(f"/api/impact/{impact_id}").json()["target_run_id"]
    full = execute(client, worker, analysis_id=target_run, full_suite=True)
    assert full["outcome"] == "FAILED" and full["mode"] == "FULL_SUITE"
    full_results = results(client, full["id"])
    full_failures = {n for n, o in full_results.items() if o == "FAILED"}
    assert len(full_results) == 6
    assert full_failures == selected_failures  # no relevant failure missed by selection
    assert full_results["tests/test_util.py::test_slugify"] == "PASSED"


def test_full_suite_on_base_passes(
    client: TestClient, worker: Worker, inventory: FixtureRepo
) -> None:
    impact_id = run_change(client, worker, inventory, projects.CHANGE_DOCS_ONLY)
    base_run = client.get(f"/api/impact/{impact_id}").json()["base_run_id"]
    run = execute(client, worker, analysis_id=base_run, full_suite=True)
    assert run["outcome"] == "PASSED" and run["counts"]["passed"] == 6


def test_unapproved_image_is_blocked(
    client: TestClient,
    inventory: FixtureRepo,
    worker: Worker,
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    impact_id = run_change(client, worker, inventory, projects.CHANGE_WITH_TAX_BODY)
    test_plan = plan(client, impact_id)
    response = client.post("/api/test-runs", json={"plan_id": test_plan["id"]}, headers=AUTH)
    run_id = response.json()["id"]
    strict = settings.model_copy(update={"sandbox_image": "some-other-image:1"})
    strict_worker = Worker(
        session_factory, build_handlers(strict), worker_id="strict", retry_backoff=timedelta(0)
    )
    assert strict_worker.run_once()
    blocked = client.get(f"/api/test-runs/{run_id}").json()
    assert blocked["outcome"] == "SECURITY_POLICY_BLOCKED"
    assert blocked["job"]["status"] == "BLOCKED"


def test_cancel_and_validation(client: TestClient, worker: Worker, inventory: FixtureRepo) -> None:
    impact_id = run_change(client, worker, inventory, projects.CHANGE_DOCS_ONLY)
    docs_plan = plan(client, impact_id)
    empty = client.post("/api/test-runs", json={"plan_id": docs_plan["id"]}, headers=AUTH)
    assert empty.status_code == 422 and empty.json()["error"]["code"] == "empty_plan"
    both = client.post(
        "/api/test-runs",
        json={"plan_id": docs_plan["id"], "analysis_id": docs_plan["analysis_run_id"]},
        headers=AUTH,
    )
    assert both.status_code == 422
    queued = client.post(
        "/api/test-runs",
        json={"analysis_id": docs_plan["analysis_run_id"], "full_suite": True},
        headers=AUTH,
    ).json()
    cancelled = client.post(f"/api/test-runs/{queued['id']}/cancel", headers=AUTH).json()
    assert cancelled["outcome"] == "CANCELLED" and cancelled["status"] == "COMPLETED"
    assert not worker.run_once()


def test_sandboxed_collection_reconciles_catalog(
    client: TestClient, worker: Worker, tmp_path: Any
) -> None:
    repo = FixtureRepo(tmp_path / "repos" / "collect")
    files = dict(projects.INVENTORY)
    # A parametrized test (statically one test) and a test hidden behind a helper decorator.
    files["tests/test_param.py"] = (
        "import pytest\n\n\n@pytest.mark.parametrize('x', [1, 2])\ndef test_p(x):\n    assert x\n"
    )
    files["tests/test_skipped_file.py"] = "collect_ignore = True\n\n\ndef helper():\n    pass\n"
    repo.commit("base", files)
    repo_id = register(client, repo.url)["id"]
    analysis = analyze(client, repo_id)
    assert worker.run_once()
    response = client.post(f"/api/analyses/{analysis['id']}/collect", headers=AUTH)
    assert response.status_code == 202, response.text
    assert worker.run_once()
    run = client.get(f"/api/test-runs/{response.json()['id']}").json()
    assert run["outcome"] == "PASSED" and run["mode"] == "COLLECT", run
    tests = client.get(f"/api/analyses/{analysis['id']}/tests", params={"limit": 100}).json()
    statuses = {t["node_id"]: t["collection_status"] for t in tests["items"]}
    assert statuses["tests/test_param.py::test_p"] == "COLLECTED"
    assert statuses["tests/test_util.py::test_slugify"] == "COLLECTED"
    assert set(statuses.values()) == {"COLLECTED"}
