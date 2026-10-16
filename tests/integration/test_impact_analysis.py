"""Impact reports for controlled changes to the inventory fixture project."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from repolens.worker.runtime import Worker
from tests.integration.conftest import AUTH, register
from tests.support import projects
from tests.support.gitrepo import FixtureRepo

pytestmark = pytest.mark.integration


@pytest.fixture
def inventory(tmp_path: Path) -> FixtureRepo:
    repo = FixtureRepo(tmp_path / "repos" / "inventory")
    repo.commit("base", projects.INVENTORY)
    repo.tag("base")
    return repo


def run_impact(
    client: TestClient, worker: Worker, repo: FixtureRepo, change: dict[str, str]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    repo.git("checkout", "-q", "-B", "change", "base")
    repo.commit("change", change)
    repo_id = register(client, repo.url)["id"]
    response = client.post(
        f"/api/repositories/{repo_id}/impact",
        json={"base": "base", "target": "change"},
        headers=AUTH,
    )
    assert response.status_code == 202, response.text
    impact_id = response.json()["id"]
    assert worker.run_once()
    status = client.get(f"/api/impact/{impact_id}").json()
    assert status["status"] == "SUCCEEDED", status
    report = client.get(f"/api/impact/{impact_id}/report").json()
    items = client.get(f"/api/impact/{impact_id}/items", params={"limit": 100}).json()["items"]
    return report, {i["qualified_name"]: i for i in items}


def test_function_body_change(client: TestClient, worker: Worker, inventory: FixtureRepo) -> None:
    report, items = run_impact(client, worker, inventory, projects.CHANGE_WITH_TAX_BODY)
    assert items["inventory.pricing.with_tax"]["strength"] == "CHANGED"
    assert items["inventory.pricing.quote"]["strength"] == "DIRECT"
    assert items["inventory.reports.summary"]["strength"] == "TRANSITIVE"
    assert items["test_pricing.test_with_tax"]["strength"] == "DIRECT"
    assert items["test_reports.test_summary"]["depth"] == 3
    explanation = items["test_reports.test_summary"]["explanation"]
    assert [(e["from"], e["to"]) for e in explanation] == [
        ("inventory.pricing.quote", "inventory.pricing.with_tax"),
        ("inventory.reports.summary", "inventory.pricing.quote"),
        ("test_reports.test_summary", "inventory.reports.summary"),
    ]
    impacted_tests = {name for name, item in items.items() if item["is_test"]}
    assert impacted_tests == {
        "test_pricing.test_with_tax",
        "test_pricing.test_quote",
        "test_reports.test_summary",
    }
    assert "test_util.test_slugify" not in items and "test_models.test_item_total" not in items
    assert report["completeness"] == "COMPLETE"
    assert report["recommended_scope"] == "TARGETED"
    assert report["counts"]["impacted_tests"] == 3
    assert "not a probability" in report["disclaimer"]


def test_method_change_reaches_overrides_and_hidden_callers(
    client: TestClient, worker: Worker, inventory: FixtureRepo
) -> None:
    report, items = run_impact(client, worker, inventory, projects.CHANGE_ITEM_TOTAL)
    assert items["inventory.models.Item.total"]["strength"] == "CHANGED"
    # Perishable.total calls super().total (heuristic MRO resolution).
    assert items["inventory.models.Perishable.total"]["strength"] == "HEURISTIC"
    # Instantiating Item reaches the changed method through the container heuristic.
    assert items["test_models.test_item_total"]["strength"] in ("DIRECT", "HEURISTIC")
    # quote() calls item.total(qty) on a parameter: unresolved, matched by name.
    assert items["inventory.pricing.quote"]["seed_reason"] == "UNRESOLVED_NAME_MATCH"
    hidden = report["uncertainty"]["possible_hidden_callers"]
    # Calls on parameters and on constructor results are unresolved but match by name.
    assert {h["source"] for h in hidden} == {
        "inventory.pricing.quote",
        "test_models.test_item_total",
        "test_models.test_perishable_total",
    }
    assert report["recommended_scope"] == "BROAD"
    assert any("unresolved call sites" in r for r in report["scope_reasons"])
    assert "test_util.test_slugify" not in items


def test_unrelated_change_selects_only_its_test(
    client: TestClient, worker: Worker, inventory: FixtureRepo
) -> None:
    _, items = run_impact(client, worker, inventory, projects.CHANGE_UTIL_ONLY)
    assert {name for name, item in items.items() if item["is_test"]} == {"test_util.test_slugify"}


def test_config_change_requires_full_suite(
    client: TestClient, worker: Worker, inventory: FixtureRepo
) -> None:
    report, items = run_impact(client, worker, inventory, projects.CHANGE_CONFIG_ONLY)
    assert items == {}
    assert report["recommended_scope"] == "FULL_SUITE"
    assert report["completeness"] == "PARTIAL"
    assert report["completeness_reasons"] == [
        {"reason": "FILE_NON_PYTHON", "path": "pyproject.toml"}
    ]
    assert any(f["factor"] == "unmapped_changes" for f in report["risk_factors"])


def test_documentation_change_needs_no_tests(
    client: TestClient, worker: Worker, inventory: FixtureRepo
) -> None:
    report, items = run_impact(client, worker, inventory, projects.CHANGE_DOCS_ONLY)
    assert items == {}
    assert report["completeness"] == "COMPLETE"
    assert report["recommended_scope"] == "NONE"
    assert report["risk_level"] == "LOW"


def test_module_level_constant_change(
    client: TestClient, worker: Worker, inventory: FixtureRepo
) -> None:
    report, items = run_impact(client, worker, inventory, projects.CHANGE_TAX_CONSTANT)
    assert items["inventory.pricing"]["strength"] == "CHANGED"
    assert items["inventory.pricing"]["seed_reason"] == "MODULE_LEVEL_CHANGE"
    # Functions in the same module may read the constant: included heuristically.
    assert items["inventory.pricing.with_tax"]["strength"] == "HEURISTIC"
    assert items["inventory.pricing.with_tax"]["seed_reason"] == "SAME_MODULE_AS_CHANGE"
    assert "test_pricing.test_with_tax" in items
    assert "test_util.test_slugify" not in items
    assert report["risk_level"] in ("LOW", "MEDIUM", "HIGH")
