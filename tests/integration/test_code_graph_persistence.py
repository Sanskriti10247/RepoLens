"""The analyzer's output as stored in PostgreSQL."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, aliased, sessionmaker

from repolens.persistence.db import transaction
from repolens.persistence.models import (
    AnalysisFile,
    AnalysisWarning,
    DependencyEdge,
    SourceFile,
    Symbol,
    SymbolReference,
)
from repolens.worker.runtime import Worker
from tests.integration.conftest import analyze, register
from tests.support.gitrepo import FixtureRepo

pytestmark = pytest.mark.integration


@pytest.fixture
def project(tmp_path: Path) -> FixtureRepo:
    repo = FixtureRepo(tmp_path / "repos" / "calc")
    repo.commit(
        "initial",
        {
            "calc/__init__.py": "from .ops import add\n",
            "calc/ops.py": (
                "def add(a, b):\n    return a + b\n\n\ndef twice(x):\n    return add(x, x)\n"
            ),
            "calc/shapes.py": (
                "class Base:\n    def area(self):\n        return 0\n\n\n"
                "class Square(Base):\n    def area(self):\n        return super().area() + 1\n"
            ),
            "calc/broken.py": "def ok():\n    pass\n\ndef bad(:\n    pass\n",
            "tests/test_ops.py": (
                "from calc import add\nfrom calc.ops import twice\n\n\n"
                "def test_add():\n    assert add(1, 2) == 3\n\n\n"
                "def test_twice():\n    assert twice(2) == 4\n"
            ),
            "scripts/run-demo.py": "from calc.ops import twice\nprint(twice(3))\n",
            "README.md": "# calc\n",
        },
    )
    return repo


def test_graph_is_persisted(
    client: TestClient,
    worker: Worker,
    project: FixtureRepo,
    session_factory: sessionmaker[Session],
) -> None:
    repo = register(client, project.url)
    run_id = analyze(client, repo["id"])["id"]
    assert worker.run_once()
    run = client.get(f"/api/analyses/{run_id}").json()
    assert run["status"] == "PARTIAL", run  # calc/broken.py has a syntax error
    counts = run["counts"]
    assert counts["python_files"] == 6
    assert counts["incomplete_files"] == 1
    assert counts["files_by_status"] == {
        "PARSED": 5,
        "PARSED_WITH_ERRORS": 1,
        "SKIPPED_UNSUPPORTED": 1,
    }
    assert run["parser_version"].startswith("python-ts")

    with transaction(session_factory) as session:
        qualified = set(
            session.execute(
                select(Symbol.qualified_name).where(Symbol.analysis_run_id == run_id)
            ).scalars()
        )
        assert {
            "calc",
            "calc.ops.add",
            "calc.ops.twice",
            "calc.shapes.Square.area",
            "test_ops.test_add",
            "__file__.scripts_run_demo",
        } <= qualified

        src, dst = aliased(Symbol), aliased(Symbol)
        edge_rows = session.execute(
            select(src.qualified_name, dst.qualified_name, DependencyEdge.edge_type)
            .join(src, src.id == DependencyEdge.src_symbol_id)
            .join(dst, dst.id == DependencyEdge.dst_symbol_id)
            .where(DependencyEdge.analysis_run_id == run_id)
        ).all()
        edges = {(a, b, t) for a, b, t in edge_rows}
        assert ("calc.ops.twice", "calc.ops.add", "CALLS") in edges
        assert ("test_ops.test_add", "calc.ops.add", "CALLS") in edges  # via re-export
        assert ("test_ops.test_twice", "calc.ops.twice", "CALLS") in edges
        assert ("__file__.scripts_run_demo", "calc.ops.twice", "CALLS") in edges
        assert ("calc.shapes.Square", "calc.shapes.Base", "INHERITS") in edges
        assert ("calc.shapes.Square.area", "calc.shapes.Base.area", "CALLS") in edges
        assert ("calc.ops", "calc.ops.add", "CONTAINS") in edges

        tests = set(
            session.execute(
                select(Symbol.qualified_name).where(
                    Symbol.analysis_run_id == run_id, Symbol.is_test.is_(True)
                )
            ).scalars()
        )
        assert tests == {"test_ops.test_add", "test_ops.test_twice"}

        # Parent links are set and spans are stored exactly.
        twice = session.execute(
            select(Symbol).where(
                Symbol.analysis_run_id == run_id, Symbol.qualified_name == "calc.ops.twice"
            )
        ).scalar_one()
        parent = session.get(Symbol, twice.parent_id)
        assert parent is not None and parent.qualified_name == "calc.ops"
        assert (twice.start_line, twice.start_col, twice.end_line, twice.end_col) == (5, 0, 6, 20)

        # External and builtin references are stored, not dropped.
        print_ref = session.execute(
            select(SymbolReference).where(
                SymbolReference.analysis_run_id == run_id,
                SymbolReference.raw_text.like("print(%"),
            )
        ).scalar_one()
        assert (print_ref.resolution_status, print_ref.external_target) == (
            "EXTERNAL",
            "builtins.print",
        )

        broken = session.execute(
            select(AnalysisFile.status, AnalysisFile.error_count)
            .join(SourceFile, SourceFile.id == AnalysisFile.file_id)
            .where(AnalysisFile.analysis_run_id == run_id, SourceFile.path == "calc/broken.py")
        ).one()
        assert broken.status == "PARSED_WITH_ERRORS" and broken.error_count >= 1
        codes = set(
            session.execute(
                select(AnalysisWarning.code).where(AnalysisWarning.analysis_run_id == run_id)
            ).scalars()
        )
        assert "SYNTAX_ERROR" in codes
