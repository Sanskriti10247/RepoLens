"""Change analysis between two commits, end to end through the API and worker."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from repolens.persistence.db import transaction
from repolens.persistence.models import AnalysisRun
from repolens.worker.runtime import Worker
from tests.integration.conftest import AUTH, register
from tests.support.gitrepo import FixtureRepo

pytestmark = pytest.mark.integration

BASE_FILES = {
    "shop/__init__.py": "",
    "shop/pricing.py": (
        "def total(items):\n    return sum(items)\n\n\n"
        "def discount(items, rate):\n    return total(items) * rate\n\n\n"
        "def legacy():\n    return 0\n"
    ),
    "shop/util.py": "def helper():\n    return 1\n",
    "pyproject.toml": "[project]\nname = 'shop'\n",
}


def _impact(client: TestClient, repo_id: str, base: str, target: str) -> dict[str, Any]:
    response = client.post(
        f"/api/repositories/{repo_id}/impact", json={"base": base, "target": target}, headers=AUTH
    )
    assert response.status_code == 202, response.text
    return response.json()  # type: ignore[no-any-return]


@pytest.fixture
def two_commits(tmp_path: Path) -> tuple[FixtureRepo, str, str]:
    repo = FixtureRepo(tmp_path / "repos" / "shop")
    base = repo.commit("base", BASE_FILES)
    repo.write(
        {
            "shop/pricing.py": (
                "def total(items):\n    # comment only change below\n    return sum(items)\n\n\n"
                "def discount(items, rate, cap=None):\n    value = total(items) * rate\n"
                "    return min(value, cap) if cap else value\n\n\n"
                "def tax(items):\n    return total(items) * 0.2\n"
            ),
            "pyproject.toml": "[project]\nname = 'shop'\nversion = '2'\n",
            "assets/logo.png": b"\x89PNG\x00\x00\x01",
        }
    )
    repo.git("mv", "shop/util.py", "shop/helpers.py")
    target = repo.commit("target")
    return repo, base, target


def test_change_set_maps_files_and_symbols(
    client: TestClient,
    worker: Worker,
    two_commits: tuple[FixtureRepo, str, str],
    session_factory: sessionmaker[Session],
) -> None:
    repo, base, target = two_commits
    repo_id = register(client, repo.url)["id"]
    change = _impact(client, repo_id, base[:12], "main")
    assert change["status"] == "QUEUED"
    assert worker.run_once()

    done = client.get(f"/api/impact/{change['id']}").json()
    assert done["status"] == "SUCCEEDED", done
    assert (done["base_sha"], done["target_sha"]) == (base, target)
    assert done["job"]["status"] == "SUCCEEDED"
    summary = done["summary"]
    assert summary["files_by_change_type"] == {"ADDED": 1, "MODIFIED": 2, "RENAMED": 1}
    assert summary["files_by_mapping_status"] == {"BINARY": 1, "MAPPED": 2, "NON_PYTHON": 1}

    files = {
        (f["new_path"] or f["old_path"]): f
        for f in client.get(f"/api/impact/{change['id']}/files").json()["items"]
    }
    assert files["shop/helpers.py"]["change_type"] == "RENAMED"
    assert files["shop/helpers.py"]["old_path"] == "shop/util.py"
    assert files["pyproject.toml"]["mapping_status"] == "NON_PYTHON"
    assert files["pyproject.toml"]["new_ranges"] == [[3, 3]]
    assert files["assets/logo.png"]["mapping_status"] == "BINARY"

    symbols = {
        s["qualified_name"]: s
        for s in client.get(f"/api/impact/{change['id']}/changed-symbols").json()["items"]
    }
    assert symbols["shop.pricing.discount"]["change_kind"] == "MODIFIED"
    assert symbols["shop.pricing.discount"]["signature_changed"]
    assert symbols["shop.pricing.discount"]["body_changed"]
    assert symbols["shop.pricing.tax"]["change_kind"] == "ADDED"
    assert symbols["shop.pricing.legacy"]["change_kind"] == "REMOVED"
    assert "shop.pricing.total" not in symbols  # only a comment was added
    # The pure file rename keeps every symbol: module renamed, nothing else changed.
    assert not any(name.startswith(("shop.util", "shop.helpers.")) for name in symbols)

    # Both commits were analysed on demand, once each.
    with transaction(session_factory) as session:
        runs = session.execute(
            select(func.count()).select_from(AnalysisRun).where(AnalysisRun.status == "SUCCEEDED")
        ).scalar_one()
    assert runs == 2


def test_existing_analyses_are_reused_and_identical_commits_have_no_changes(
    client: TestClient,
    worker: Worker,
    two_commits: tuple[FixtureRepo, str, str],
    session_factory: sessionmaker[Session],
) -> None:
    repo, base, target = two_commits
    repo_id = register(client, repo.url)["id"]
    first = _impact(client, repo_id, base, target)
    worker.run_once()
    same = _impact(client, repo_id, target, "main")
    worker.run_once()
    result = client.get(f"/api/impact/{same['id']}").json()
    assert result["status"] == "SUCCEEDED"
    assert result["summary"]["changed_files"] == 0
    with transaction(session_factory) as session:
        runs = session.execute(select(func.count()).select_from(AnalysisRun)).scalar_one()
    assert runs == 2  # no re-analysis of already analysed commits
    listing = client.get(f"/api/repositories/{repo_id}/impact").json()
    assert {i["id"] for i in listing["items"]} == {first["id"], same["id"]}


def test_unknown_revision_fails_cleanly(
    client: TestClient, worker: Worker, two_commits: tuple[FixtureRepo, str, str]
) -> None:
    repo, base, _ = two_commits
    repo_id = register(client, repo.url)["id"]
    change = _impact(client, repo_id, base, "does-not-exist")
    worker.run_once()
    failed = client.get(f"/api/impact/{change['id']}").json()
    assert failed["status"] == "FAILED"
    assert failed["error"].startswith("revision_not_resolved")


def test_validation_and_cancellation(
    client: TestClient, worker: Worker, two_commits: tuple[FixtureRepo, str, str]
) -> None:
    repo, base, target = two_commits
    repo_id = register(client, repo.url)["id"]
    bad = client.post(
        f"/api/repositories/{repo_id}/impact",
        json={"base": "--output=x", "target": "main"},
        headers=AUTH,
    )
    assert bad.status_code == 422
    unauthorised = client.post(
        f"/api/repositories/{repo_id}/impact", json={"base": base, "target": target}
    )
    assert unauthorised.status_code == 401
    change = _impact(client, repo_id, base, target)
    cancelled = client.post(f"/api/impact/{change['id']}/cancel", headers=AUTH).json()
    assert cancelled["status"] == "CANCELLED"
    assert not worker.run_once()


def test_mirrored_commits_are_compared_without_fetching(
    client: TestClient, worker: Worker, two_commits: tuple[FixtureRepo, str, str]
) -> None:
    repo, base, target = two_commits
    repo_id = register(client, repo.url)["id"]
    first = _impact(client, repo_id, base, "main")  # a branch name: must fetch
    assert worker.run_once()
    assert "fetch_skipped" not in client.get(f"/api/impact/{first['id']}").json()["timings_ms"]

    newer = repo.commit("newer", {"shop/util2.py": "X = 1\n"})
    again = _impact(client, repo_id, base, target)  # both commits already mirrored
    assert worker.run_once()
    done = client.get(f"/api/impact/{again['id']}").json()
    assert done["status"] == "SUCCEEDED", done
    assert done["timings_ms"]["fetch_skipped"] == 1

    unknown = _impact(client, repo_id, target, newer)  # newer is not mirrored yet
    assert worker.run_once()
    done = client.get(f"/api/impact/{unknown['id']}").json()
    assert done["status"] == "SUCCEEDED", done
    assert "fetch_skipped" not in done["timings_ms"]
    assert done["target_sha"] == newer
