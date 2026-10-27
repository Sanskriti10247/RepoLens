"""Test plans built from impact reports of controlled changes."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from repolens.config import Settings
from repolens.worker.handlers import build_handlers
from repolens.worker.runtime import Worker
from tests.integration.conftest import AUTH, register, run_change
from tests.support import projects
from tests.support.gitrepo import FixtureRepo

pytestmark = pytest.mark.integration

ALL_TESTS = {
    "tests/test_models.py::test_item_total",
    "tests/test_models.py::test_perishable_total",
    "tests/test_pricing.py::test_with_tax",
    "tests/test_pricing.py::test_quote",
    "tests/test_reports.py::test_summary",
    "tests/test_util.py::test_slugify",
}


def plan_for(
    client: TestClient, impact_id: str, **body: Any
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    response = client.post(f"/api/impact/{impact_id}/test-plan", json=body, headers=AUTH)
    assert response.status_code == 201, response.text
    plan = response.json()
    items = client.get(f"/api/test-plans/{plan['id']}/items", params={"limit": 100}).json()
    return plan, {i["node_id"]: i for i in items["items"]}


def selected(items: dict[str, dict[str, Any]]) -> set[str]:
    return {node for node, item in items.items() if item["selected"]}


def test_static_catalog(client: TestClient, worker: Worker, inventory: FixtureRepo) -> None:
    impact_id = run_change(client, worker, inventory, projects.CHANGE_WITH_TAX_BODY)
    target_run = client.get(f"/api/impact/{impact_id}").json()["target_run_id"]
    tests = client.get(f"/api/analyses/{target_run}/tests").json()
    assert {t["node_id"] for t in tests["items"]} == ALL_TESTS
    assert {t["collection_status"] for t in tests["items"]} == {"STATIC"}


def test_targeted_selection_with_evidence(
    client: TestClient, worker: Worker, inventory: FixtureRepo
) -> None:
    impact_id = run_change(client, worker, inventory, projects.CHANGE_WITH_TAX_BODY)
    plan, items = plan_for(client, impact_id)
    assert plan["scope"] == "TARGETED" and plan["policy"] == "CONSERVATIVE"
    assert selected(items) == {
        "tests/test_pricing.py::test_with_tax",
        "tests/test_pricing.py::test_quote",
        "tests/test_reports.py::test_summary",
    }
    assert (plan["total_tests"], plan["selected_tests"]) == (6, 3)
    with_tax = items["tests/test_pricing.py::test_with_tax"]
    assert with_tax["rank"] == 1 and with_tax["evidence"][0]["kind"] == "STATIC_DIRECT"
    summary = items["tests/test_reports.py::test_summary"]
    assert summary["evidence"][0]["kind"] == "STATIC_TRANSITIVE"
    assert [step["to"] for step in summary["evidence"][0]["path"]] == [
        "inventory.pricing.with_tax",
        "inventory.pricing.quote",
        "inventory.reports.summary",
    ]
    assert items["tests/test_util.py::test_slugify"]["exclusion_reason"] == "NO_EVIDENCE"
    assert plan["evidence_sources"] == {
        "static_graph": True,
        "collection": False,
        "coverage": False,
        "history": False,
        "excluded_not_collected": [],
    }


def test_broad_scope_adds_module_dependency_evidence(
    client: TestClient, worker: Worker, inventory: FixtureRepo
) -> None:
    impact_id = run_change(client, worker, inventory, projects.CHANGE_ITEM_TOTAL)
    plan, items = plan_for(client, impact_id)
    assert plan["scope"] == "BROAD"
    chosen = selected(items)
    assert {
        "tests/test_models.py::test_item_total",
        "tests/test_models.py::test_perishable_total",
        "tests/test_pricing.py::test_quote",
        "tests/test_reports.py::test_summary",
    } <= chosen
    assert "tests/test_util.py::test_slugify" not in chosen


def test_config_change_falls_back_to_full_suite(
    client: TestClient, worker: Worker, inventory: FixtureRepo
) -> None:
    impact_id = run_change(client, worker, inventory, projects.CHANGE_CONFIG_ONLY)
    plan, items = plan_for(client, impact_id)
    assert plan["scope"] == "FULL_SUITE" and "pyproject.toml" in plan["fallback_reason"]
    assert selected(items) == ALL_TESTS
    assert all(i["evidence"][0]["kind"] == "FULL_SUITE_FALLBACK" for i in items.values())


def test_untraceable_change_without_linked_tests_runs_full_suite(
    client: TestClient, worker: Worker, inventory: FixtureRepo
) -> None:
    impact_id = run_change(client, worker, inventory, projects.CHANGE_DATA_ONLY)
    plan, items = plan_for(client, impact_id)
    assert plan["scope"] == "FULL_SUITE"
    assert "no test linked" in plan["fallback_reason"]
    assert selected(items) == ALL_TESTS


def test_budget_excludes_explicitly(
    client: TestClient, worker: Worker, inventory: FixtureRepo
) -> None:
    impact_id = run_change(client, worker, inventory, projects.CHANGE_WITH_TAX_BODY)
    plan, items = plan_for(client, impact_id, policy="BUDGETED", max_tests=1)
    assert plan["budget_limited"] and plan["selected_tests"] == 1
    assert selected(items) == {"tests/test_pricing.py::test_with_tax"}
    excluded = {n for n, i in items.items() if i["exclusion_reason"] == "BUDGET"}
    assert excluded == {"tests/test_pricing.py::test_quote", "tests/test_reports.py::test_summary"}
    missing_budget = client.post(
        f"/api/impact/{impact_id}/test-plan", json={"policy": "BUDGETED"}, headers=AUTH
    )
    assert missing_budget.status_code == 422


def test_plan_requires_completed_impact(client: TestClient, inventory: FixtureRepo) -> None:
    repo_id = register(client, inventory.url)["id"]
    queued = client.post(
        f"/api/repositories/{repo_id}/impact", json={"base": "base", "target": "base"}, headers=AUTH
    ).json()
    response = client.post(f"/api/impact/{queued['id']}/test-plan", json={}, headers=AUTH)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "impact_not_ready"


def test_plans_work_without_a_sandbox(
    client: TestClient,
    worker: Worker,
    inventory: FixtureRepo,
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    impact_id = run_change(client, worker, inventory, projects.CHANGE_WITH_TAX_BODY)
    plan, items = plan_for(client, impact_id)
    assert selected(items)
    # A run queued before the sandbox was switched off fails without touching Docker.
    response = client.post("/api/test-runs", json={"plan_id": plan["id"]}, headers=AUTH)
    assert response.status_code == 202, response.text
    off = settings.model_copy(update={"sandbox_enabled": False, "docker_binary": "/nonexistent"})
    off_worker = Worker(
        session_factory, build_handlers(off), worker_id="no-sandbox", retry_backoff=timedelta(0)
    )
    assert off_worker.run_once()
    run = client.get(f"/api/test-runs/{response.json()['id']}").json()
    assert run["status"] == "COMPLETED"
    assert run["outcome"] == "INFRASTRUCTURE_ERROR"
    assert "sandbox_unavailable" in run["outcome_reason"]
    assert run["job"]["status"] == "FAILED"
