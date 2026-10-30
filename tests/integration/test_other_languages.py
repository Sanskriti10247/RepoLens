"""A repository without Python, analysed end to end through the API and the worker."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from repolens.worker.runtime import Worker
from tests.integration.conftest import AUTH, analyze, register, run_change
from tests.support.gitrepo import FixtureRepo

pytestmark = pytest.mark.integration

FILES = {
    "src/util.ts": (
        "/** Adds two numbers. */\n"
        "export function add(a: number, b: number): number {\n"
        "  return a > 0 && b > 0 ? a + b : 0;\n"
        "}\n"
        "export class Box {\n"
        "  get(): number { return add(1, 2); }\n"
        "}\n"
    ),
    "src/index.ts": (
        'import { add, Box } from "./util.js";\n'
        'import lodash from "lodash";\n'
        "export const main = () => add(new Box().get(), lodash.size([]));\n"
    ),
    "test/util.test.ts": 'import { add } from "../src/util";\nadd(1, 1);\n',
    "cmd/main.go": "package main\n\n// Run starts.\nfunc Run() { helper() }\n\nfunc helper() {}\n",
    "README.md": "# demo\n",
    "LICENSE": "MIT\n",
}


def _analysed(client: TestClient, worker: Worker, repo: FixtureRepo) -> tuple[str, str]:
    registered = register(client, repo.url)
    run = analyze(client, registered["id"])
    assert worker.run_once()
    status = client.get(f"/api/analyses/{run['id']}").json()
    assert status["status"] == "SUCCEEDED", status
    return registered["id"], run["id"]


def _symbol(client: TestClient, repo_id: str, name: str) -> dict[str, Any]:
    items = client.get(f"/api/repositories/{repo_id}/symbols", params={"q": name}).json()["items"]
    match = next(i for i in items if i["qualified_name"] == name)
    detail = client.get(f"/api/repositories/{repo_id}/symbols/{match['id']}")
    assert detail.status_code == 200, detail.text
    return detail.json()  # type: ignore[no-any-return]


def test_typescript_and_go_repository(client: TestClient, worker: Worker, tmp_path: Path) -> None:
    repo = FixtureRepo(tmp_path / "repos" / "polyglot")
    repo.commit("initial", FILES)
    repo_id, run_id = _analysed(client, worker, repo)

    counts = client.get(f"/api/analyses/{run_id}").json()["counts"]
    assert counts["code_files_by_language"] == {"go": 1, "typescript": 3}
    assert counts["python_files"] == 0 and counts["incomplete_files"] == 0

    body = client.get(f"/api/analyses/{run_id}/insights").json()
    languages = {lang["language"]: lang for lang in body["overview"]["languages"]}
    assert set(languages) == {"typescript", "go", "markdown", "text"}
    assert languages["typescript"]["files"] == 3 and languages["typescript"]["lines"] == 12
    assert languages["go"]["lines"] == 6
    assert body["overview"]["test_files"] == 1
    assert body["complexity"]["available"] is True

    add = _symbol(client, repo_id, "src/util.ts.add")
    assert add["symbol"]["doc_summary"] == "Adds two numbers."
    callers = {c["symbol"]["qualified_name"] for c in add["callers"]}
    # Through the relative import ("./util.js" names util.ts) and within the file.
    assert {"src/index.ts.main", "src/util.ts.Box.get"} <= callers
    main = _symbol(client, repo_id, "src/index.ts.main")
    statuses = {r["raw_text"]: r["resolution_status"] for r in main["references"]}
    assert statuses["lodash.size([])"] == "UNRESOLVED"  # package imports are never guessed
    run_go = _symbol(client, repo_id, "cmd/main.go.Run")
    assert [c["symbol"]["name"] for c in run_go["callees"]] == ["helper"]


def test_test_selection_reports_unsupported_language(
    client: TestClient, worker: Worker, tmp_path: Path
) -> None:
    repo = FixtureRepo(tmp_path / "repos" / "ts-only")
    repo.commit("base", FILES)
    repo.tag("base")
    impact_id = run_change(
        client, worker, repo, {"src/util.ts": FILES["src/util.ts"].replace("0;", "-1;")}
    )
    report = client.get(f"/api/impact/{impact_id}/report").json()
    assert report["counts"]["changed_symbols"] >= 1

    response = client.post(f"/api/impact/{impact_id}/test-plan", json={}, headers=AUTH)
    assert response.status_code == 422, response.text
    assert "not supported for typescript" in response.text
