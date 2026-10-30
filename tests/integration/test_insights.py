"""Repository health report (GET /analyses/{id}/insights) over a real analysed fixture."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from repolens.worker.runtime import Worker
from tests.integration.conftest import analyze, register
from tests.support.gitrepo import FixtureRepo

pytestmark = pytest.mark.integration

CORE = '''"""Core."""


def decorate(fn):
    return fn


def run(x):
    """Run."""
    if x and x > 1:
        return _helper(x)
    return 0


def _helper(x):
    return x


def _unused(x):
    for i in range(x):
        if i:
            return i
    return x


def _called_on_unknown_object():
    return 1


def use(obj):
    return obj._called_on_unknown_object()


class _Private:
    def _method(self):
        return 1

    def __repr__(self):
        return "p"

    def visible(self):  # public method names may implement a protocol: never flagged
        return 1


@decorate
def _decorated():
    return 1


def public_unused():
    return 1


class Handler(Exception):
    def _hook(self):
        return 1


def outer():
    def _never():
        return 1

    def used():
        return 2

    return used()


def _in_table():
    return 1


TABLE = {"a": _in_table}


class _OnlyAnnotated:
    pass


def annotated(x: _OnlyAnnotated) -> None:
    return None


class _Meta(type):
    pass


class WithMeta(metaclass=_Meta):
    pass


class Middle(Handler):
    pass


class Leaf(Middle):
    def _deep_hook(self):  # an external base two levels up may call it
        return 1


if TABLE:
    def _twice():
        return 1
else:
    def _twice():
        return 2


_twice()
'''

FILES = {
    "app/__init__.py": "from .core import run\n",
    "app/core.py": CORE,
    "app/untested.py": "def lonely():\n    return 1\n",
    "tests/test_core.py": (
        "from app.core import run\n\n\ndef test_run():\n    assert run(1) == 0\n"
    ),
}


@pytest.fixture
def analysed(client: TestClient, worker: Worker, tmp_path: Path) -> tuple[str, str]:
    repo = FixtureRepo(tmp_path / "repos" / "health")
    repo.commit("initial", FILES)
    registered = register(client, repo.url)
    run = analyze(client, registered["id"])
    assert worker.run_once()
    assert client.get(f"/api/analyses/{run['id']}").json()["status"] == "SUCCEEDED"
    return registered["id"], run["id"]


def _insights(client: TestClient, analysis_id: str) -> dict[str, Any]:
    response = client.get(f"/api/analyses/{analysis_id}/insights")
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def test_report_shape_and_numbers(client: TestClient, analysed: tuple[str, str]) -> None:
    _, run_id = analysed
    body = _insights(client, run_id)
    assert body["analysis_id"] == run_id == body["results_run_id"]
    assert len(body["commit_sha"]) == 40

    overview = body["overview"]
    assert overview["files"] == 4
    assert overview["test_files"] == 1
    assert overview["source_files"] == 3
    assert overview["roles"] == {"source": 3, "test": 1}
    assert overview["languages"][0]["language"] == "python"
    assert overview["modules"] == 4

    complexity = body["complexity"]
    assert complexity["available"] is True
    top = {t["qualified_name"]: t for t in complexity["top"]}
    assert top["app.core.run"]["complexity"] == 3  # if + and
    assert top["app.core._unused"]["complexity"] == 3  # for + if
    assert top["app.core.run"]["lines"] == 5
    assert sum(b["count"] for b in complexity["distribution"]) == complexity["functions"]

    testing = body["testing"]
    assert testing["test_functions"] == 1
    assert (testing["modules_with_tests"], testing["modules_without_tests"]) == (1, 1)
    assert [m["module"] for m in testing["untested_modules"]] == ["app.untested"]

    dead = {c["qualified_name"] for c in body["dead_code"]["candidates"]}
    assert dead == {
        "app.core._unused",
        "app.core._Private",
        "app.core._Private._method",
        "app.core.outer.<locals>._never",
    }
    assert body["dead_code"]["count"] == 4

    refs = body["references"]
    assert set(refs) == {
        "resolved",
        "ambiguous",
        "external",
        "unresolved",
        "dynamic",
        "resolution_rate",
    }

    score = body["score"]
    assert [c["key"] for c in score["components"]] == [
        "complexity",
        "documentation",
        "testing",
        "structure",
        "resolution",
    ]
    assert sum(c["weight"] for c in score["components"]) == pytest.approx(1)
    expected = round(sum(c["weight"] * c["score"] for c in score["components"]))
    assert score["overall"] == expected
    assert score["grade"] in {"A", "B", "C", "D", "F"}

    ids = [f["id"] for f in body["findings"]]
    assert "testing.untested_modules" in ids
    assert "dead_code.candidates" in ids
    order = ["high", "medium", "low", "info"]
    severities = [order.index(f["severity"]) for f in body["findings"]]
    assert severities == sorted(severities)
    for finding in body["findings"]:
        assert finding["evidence"], finding


def test_not_ready_and_unknown(client: TestClient, tmp_path: Path) -> None:
    repo = FixtureRepo(tmp_path / "repos" / "pending")
    repo.commit("initial", FILES)
    run = analyze(client, register(client, repo.url)["id"])
    response = client.get(f"/api/analyses/{run['id']}/insights")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "analysis_not_ready"
    missing = client.get(f"/api/analyses/{uuid.uuid4()}/insights")
    assert missing.status_code == 404


def test_reused_analysis_reads_the_original_results(
    client: TestClient, worker: Worker, analysed: tuple[str, str]
) -> None:
    repo_id, first_id = analysed
    second = analyze(client, repo_id)
    assert worker.run_once()
    assert client.get(f"/api/analyses/{second['id']}").json()["status"] == "REUSED"
    body = _insights(client, second["id"])
    assert body["analysis_id"] == second["id"]
    assert body["results_run_id"] == first_id
    original = _insights(client, first_id)
    assert body["score"] == original["score"]
    assert body["dead_code"] == original["dead_code"]


def test_analysis_without_complexity_is_scored_without_it(
    client: TestClient,
    session_factory: sessionmaker[Session],
    analysed: tuple[str, str],
) -> None:
    _, run_id = analysed
    with session_factory.begin() as session:  # as stored by a parser from before complexity
        session.execute(
            text("UPDATE symbols SET complexity = NULL WHERE analysis_run_id = :run"),
            {"run": run_id},
        )
    body = _insights(client, run_id)
    assert body["complexity"]["available"] is False
    assert body["complexity"]["top"] == []
    component = body["score"]["components"][0]
    assert component["key"] == "complexity"
    assert component["score"] is None
    assert component["weight"] == 0
    assert "Excluded from the overall score" in component["detail"]
    others = body["score"]["components"][1:]
    assert sum(c["weight"] for c in others) == pytest.approx(1, abs=1e-3)
    assert not any(f["category"] == "complexity" for f in body["findings"])
