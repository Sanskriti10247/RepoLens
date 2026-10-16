"""Incremental analysis (parse cache) must produce exactly what a clean reindex produces."""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, aliased, sessionmaker

from repolens.config import Settings
from repolens.persistence.db import transaction
from repolens.persistence.models import (
    AnalysisFile,
    AnalysisRun,
    AnalysisWarning,
    DependencyEdge,
    SourceFile,
    Symbol,
    SymbolReference,
)
from repolens.worker.handlers import build_handlers
from repolens.worker.runtime import Worker
from tests.integration.conftest import analyze, register
from tests.support import projects
from tests.support.gitrepo import FixtureRepo

pytestmark = pytest.mark.integration


def make_repo(path: Path) -> FixtureRepo:
    repo = FixtureRepo(path)
    files = dict(projects.INVENTORY)
    files["inventory/broken.py"] = "def ok():\n    pass\n\ndef bad(:\n    pass\n"
    repo.commit("a", files)
    repo.tag("a")
    repo.commit(
        "b", {**projects.CHANGE_ITEM_TOTAL, "inventory/extra.py": "def extra():\n    return 1\n"}
    )
    repo.tag("b")
    return repo


def snapshot_of(session: Session, run_id: uuid.UUID) -> dict[str, Any]:
    """Every analysis output row, keyed by stable identity (never by database ids)."""
    sym = {
        s.id: s
        for s in session.execute(select(Symbol).where(Symbol.analysis_run_id == run_id)).scalars()
    }
    key = {i: s.symbol_key for i, s in sym.items()}
    paths = dict(session.execute(select(SourceFile.id, SourceFile.path)).all())
    symbols = {
        (
            s.symbol_key,
            s.qualified_name,
            s.kind,
            s.visibility,
            paths[s.file_id],
            key.get(s.parent_id) if s.parent_id else None,
            s.start_line,
            s.start_col,
            s.end_line,
            s.end_col,
            s.signature,
            s.body_fingerprint,
            tuple(s.decorators),
            s.doc_summary,
            s.is_test,
            s.has_parse_errors,
        )
        for s in sym.values()
    }
    references = {
        (
            key[r.source_symbol_id],
            r.ref_kind,
            r.raw_text,
            r.start_line,
            r.start_col,
            r.end_line,
            r.end_col,
            r.resolution_status,
            key.get(r.target_symbol_id) if r.target_symbol_id else None,
            r.external_target,
            tuple(r.candidates or ()),
            r.resolver_rule,
        )
        for r in session.execute(
            select(SymbolReference).where(SymbolReference.analysis_run_id == run_id)
        ).scalars()
    }
    src, dst = aliased(Symbol), aliased(Symbol)
    edges = set(
        session.execute(
            select(
                src.symbol_key,
                dst.symbol_key,
                DependencyEdge.edge_type,
                DependencyEdge.provenance,
                DependencyEdge.confidence,
                DependencyEdge.reference_count,
            )
            .join(src, src.id == DependencyEdge.src_symbol_id)
            .join(dst, dst.id == DependencyEdge.dst_symbol_id)
            .where(DependencyEdge.analysis_run_id == run_id)
        ).all()
    )
    files = set(
        session.execute(
            select(
                SourceFile.path,
                AnalysisFile.status,
                AnalysisFile.module_name,
                AnalysisFile.symbol_count,
                AnalysisFile.error_count,
            )
            .join(SourceFile, SourceFile.id == AnalysisFile.file_id)
            .where(AnalysisFile.analysis_run_id == run_id)
        ).all()
    )
    warnings = {
        (paths.get(w.file_id), w.code, w.severity, w.message, w.start_line, w.end_line)
        for w in session.execute(
            select(AnalysisWarning).where(AnalysisWarning.analysis_run_id == run_id)
        ).scalars()
    }
    return {
        "symbols": symbols,
        "references": references,
        "edges": edges,
        "files": files,
        "warnings": warnings,
    }


def test_incremental_matches_clean_reindex(
    client: TestClient,
    worker: Worker,
    settings: Settings,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    incremental = make_repo(tmp_path / "repos" / "incremental")
    clean = make_repo(tmp_path / "repos" / "clean")
    assert incremental.git("rev-parse", "b") == clean.git("rev-parse", "b")  # same commit

    inc_id = register(client, incremental.url)["id"]
    analyze(client, inc_id, "a")
    assert worker.run_once()
    inc_b = analyze(client, inc_id, "b")["id"]
    assert worker.run_once()

    # A clean full reindex of the same commit: separate repository, cache disabled.
    cold_worker = Worker(
        session_factory,
        build_handlers(settings.model_copy(update={"parse_cache_enabled": False})),
        worker_id="cold",
        retry_backoff=timedelta(0),
    )
    clean_b = analyze(client, register(client, clean.url)["id"], "b")["id"]
    assert cold_worker.run_once()

    with transaction(session_factory) as session:
        inc_run = session.get(AnalysisRun, uuid.UUID(inc_b))
        clean_run = session.get(AnalysisRun, uuid.UUID(clean_b))
        assert inc_run is not None and clean_run is not None
        assert inc_run.status == clean_run.status == "PARTIAL"  # broken.py has a syntax error
        hits = inc_run.counts["parse_cache_hits"]  # type: ignore[index]
        misses = inc_run.counts["parse_cache_misses"]  # type: ignore[index]
        assert misses == 2 and hits == 10  # models.py and extra.py changed; 10 of 12 reused
        assert clean_run.counts["parse_cache_hits"] == 0  # type: ignore[index]
        incremental_rows = snapshot_of(session, inc_run.id)
        clean_rows = snapshot_of(session, clean_run.id)
    for table in incremental_rows:
        assert incremental_rows[table] == clean_rows[table], table
    assert len(incremental_rows["symbols"]) > 20 and len(incremental_rows["edges"]) > 20
