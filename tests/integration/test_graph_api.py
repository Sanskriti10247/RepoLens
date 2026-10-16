"""Code-graph exploration API over a real analysed fixture repository."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from repolens.worker.runtime import Worker
from tests.integration.conftest import analyze, register
from tests.support.gitrepo import FixtureRepo

pytestmark = pytest.mark.integration

FILES = {
    "calc/__init__.py": "from .ops import add\n",
    "calc/ops.py": "def add(a, b):\n    return a + b\n\n\ndef twice(x):\n    return add(x, x)\n",
    "calc/cycle_a.py": "from calc import cycle_b\n\n\ndef a():\n    return cycle_b.b()\n",
    "calc/cycle_b.py": "from calc import cycle_a\n\n\ndef b():\n    return 1\n",
    "calc/broken.py": "def bad(:\n    pass\n",
    "tests/test_ops.py": (
        "from calc import add\nfrom calc.ops import twice\n\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n\n"
        "def test_twice():\n    assert twice(2) == 4\n"
    ),
    "logo.png": b"\x89PNG\r\n\x1a\n\x00\x00binary",
}


@pytest.fixture
def analysed(client: TestClient, worker: Worker, tmp_path: Path) -> tuple[str, str, FixtureRepo]:
    repo = FixtureRepo(tmp_path / "repos" / "calc")
    repo.commit("initial", FILES)
    registered = register(client, repo.url)
    run = analyze(client, registered["id"])
    assert worker.run_once()
    status = client.get(f"/api/analyses/{run['id']}").json()["status"]
    assert status == "PARTIAL"
    return registered["id"], run["id"], repo


def _symbol(client: TestClient, repo_id: str, name: str) -> dict[str, Any]:
    items = client.get(f"/api/repositories/{repo_id}/symbols", params={"q": name}).json()["items"]
    return next(i for i in items if i["qualified_name"] == name)


def test_symbol_search(client: TestClient, analysed: tuple[str, str, FixtureRepo]) -> None:
    repo_id, run_id, repo = analysed
    page = client.get(f"/api/repositories/{repo_id}/symbols", params={"q": "add"}).json()
    assert page["commit_sha"] == repo.git("rev-parse", "HEAD")
    assert page["analysis_id"] == run_id
    assert page["items"][0]["qualified_name"] == "calc.ops.add"  # exact name match first
    functions = client.get(
        f"/api/repositories/{repo_id}/symbols", params={"kind": "FUNCTION", "limit": 100}
    ).json()
    assert {i["kind"] for i in functions["items"]} == {"FUNCTION"}
    tests = client.get(f"/api/repositories/{repo_id}/symbols", params={"is_test": True}).json()
    assert {i["qualified_name"] for i in tests["items"]} == {
        "test_ops.test_add",
        "test_ops.test_twice",
    }
    like = client.get(f"/api/repositories/{repo_id}/symbols", params={"q": "%"}).json()
    assert like["page"]["total"] == 0  # wildcard characters are matched literally
    bad = client.get(f"/api/repositories/{repo_id}/symbols", params={"kind": "NOPE"})
    assert bad.status_code == 422


def test_symbol_detail_shows_callers_and_resolution(
    client: TestClient, analysed: tuple[str, str, FixtureRepo]
) -> None:
    repo_id, _, _ = analysed
    add = _symbol(client, repo_id, "calc.ops.add")
    detail = client.get(f"/api/repositories/{repo_id}/symbols/{add['id']}").json()
    callers = {c["symbol"]["qualified_name"] for c in detail["callers"]}
    assert callers == {"calc.ops.twice", "test_ops.test_add"}
    assert detail["callers_total"] == 2
    assert detail["parent"]["qualified_name"] == "calc.ops"
    assert detail["symbol"]["signature"] == "def add(a, b)"
    importers = {d["symbol"]["qualified_name"] for d in detail["dependents"]}
    assert {"calc", "test_ops"} <= importers  # re-export and test import

    twice = _symbol(client, repo_id, "calc.ops.twice")
    twice_detail = client.get(f"/api/repositories/{repo_id}/symbols/{twice['id']}").json()
    assert [r["target_qualified_name"] for r in twice_detail["references"]] == ["calc.ops.add"]
    assert twice_detail["reference_counts"] == {"RESOLVED": 1}


def test_bounded_neighbourhood_and_paths(
    client: TestClient, analysed: tuple[str, str, FixtureRepo]
) -> None:
    repo_id, _, _ = analysed
    add = _symbol(client, repo_id, "calc.ops.add")
    graph = client.get(
        f"/api/repositories/{repo_id}/graph",
        params={"symbol_id": add["id"], "direction": "IN", "depth": 2, "edge_types": "CALLS"},
    ).json()
    depths = {n["symbol"]["qualified_name"]: n["depth"] for n in graph["nodes"]}
    assert depths == {
        "calc.ops.add": 0,
        "calc.ops.twice": 1,
        "test_ops.test_add": 1,
        "test_ops.test_twice": 2,
    }
    assert not graph["truncated"]
    limited = client.get(
        f"/api/repositories/{repo_id}/graph",
        params={"symbol_id": add["id"], "direction": "IN", "max_nodes": 2, "edge_types": "CALLS"},
    ).json()
    assert limited["truncated"] and len(limited["nodes"]) == 2

    test_twice = _symbol(client, repo_id, "test_ops.test_twice")
    path = client.get(
        f"/api/repositories/{repo_id}/paths",
        params={"from": test_twice["id"], "to": add["id"], "edge_types": "CALLS"},
    ).json()
    assert path["found"] and len(path["edges"]) == 2
    reverse = client.get(
        f"/api/repositories/{repo_id}/paths",
        params={"from": add["id"], "to": test_twice["id"], "edge_types": "CALLS"},
    ).json()
    assert reverse == {"found": False, "edges": [], "symbols": reverse["symbols"]}


def test_modules_cycles_and_stats(
    client: TestClient, analysed: tuple[str, str, FixtureRepo]
) -> None:
    _, run_id, _ = analysed
    modules = client.get(f"/api/analyses/{run_id}/modules").json()
    names = {m["id"]: m["name"] for m in modules["modules"]}
    assert [sorted(names[i] for i in cycle) for cycle in modules["import_cycles"]] == [
        ["calc.cycle_a", "calc.cycle_b"]
    ]
    pairs = {
        (names[e["src_module_id"]], names[e["dst_module_id"]], e["edge_type"])
        for e in modules["edges"]
    }
    assert ("test_ops", "calc.ops", "CALLS") in pairs

    stats = client.get(f"/api/analyses/{run_id}/stats").json()
    assert stats["top_fan_in"][0]["symbol"]["qualified_name"] == "calc.ops.add"
    assert stats["symbols_by_kind"]["MODULE"] == 6


def test_files_warnings_and_source(
    client: TestClient, analysed: tuple[str, str, FixtureRepo]
) -> None:
    repo_id, run_id, _ = analysed
    files = client.get(f"/api/analyses/{run_id}/files", params={"limit": 100}).json()
    assert files["status_counts"] == {
        "PARSED": 5,
        "PARSED_WITH_ERRORS": 1,
        "SKIPPED_BINARY": 1,
    }
    by_path = {f["path"]: f for f in files["items"]}
    assert by_path["calc/ops.py"]["module_name"] == "calc.ops"
    broken = client.get(
        f"/api/analyses/{run_id}/files", params={"status": "PARSED_WITH_ERRORS"}
    ).json()
    assert [f["path"] for f in broken["items"]] == ["calc/broken.py"]

    warnings = client.get(f"/api/analyses/{run_id}/warnings").json()
    assert {w["code"] for w in warnings["items"]} == {"SYNTAX_ERROR"}
    assert warnings["items"][0]["path"] == "calc/broken.py"

    source = client.get(
        f"/api/repositories/{repo_id}/files/{by_path['calc/ops.py']['id']}/source"
    ).json()
    assert source["content"] == FILES["calc/ops.py"]
    binary = client.get(f"/api/repositories/{repo_id}/files/{by_path['logo.png']['id']}/source")
    assert binary.status_code == 422
    missing = client.get(f"/api/repositories/{repo_id}/files/999999999/source")
    assert missing.status_code == 404


def test_results_require_completed_analysis(
    client: TestClient, analysed: tuple[str, str, FixtureRepo], tmp_path: Path
) -> None:
    other = FixtureRepo(tmp_path / "repos" / "other")
    other.commit("x", {"m.py": "def f(): ...\n"})
    other_id = register(client, other.url)["id"]
    none_yet = client.get(f"/api/repositories/{other_id}/symbols")
    assert none_yet.status_code == 404 and none_yet.json()["error"]["code"] == "no_analysis"
    queued = analyze(client, other_id)
    not_ready = client.get(
        f"/api/repositories/{other_id}/symbols", params={"analysis_id": queued["id"]}
    )
    assert not_ready.status_code == 422
    repo_id, run_id, _ = analysed
    # An analysis of a different repository cannot be used to read this one.
    wrong = client.get(f"/api/repositories/{other_id}/symbols", params={"analysis_id": run_id})
    assert wrong.status_code == 404
    add = _symbol(client, repo_id, "calc.ops.add")
    assert client.get(f"/api/repositories/{other_id}/symbols/{add['id']}").status_code == 404
