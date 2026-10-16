"""Reproducible benchmarks for RepoLens (analysis, impact, selection, sandbox execution).

Runs the real worker code paths in-process against a dedicated database and data directory,
so numbers include fetching, parsing, resolution and PostgreSQL persistence.

Usage:
    uv run python scripts/benchmark.py --out docs/benchmarks/results.json \\
        --repo https://github.com/pallets/itsdangerous@main --repo https://github.com/pallets/click@main

Network access is needed for GitHub repositories; Docker and the sandbox image for execution.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alembic import command
from pydantic import SecretStr
from sqlalchemy import text

from repolens.config import Settings
from repolens.execution.docker import DockerSandbox, SandboxRequest
from repolens.execution.policy import SandboxLimits
from repolens.persistence.db import make_engine, make_session_factory, transaction
from repolens.persistence.migrations import alembic_config
from repolens.persistence.models import AnalysisRun, ChangeSet, ImpactReport, TestPlan, TestRun
from repolens.services import analysis as analysis_service
from repolens.services import change_analysis, execution, repositories, test_planning
from repolens.test_selection.selection import Policy
from repolens.worker.handlers import build_handlers
from repolens.worker.runtime import Worker
from tests.support import projects
from tests.support.gitrepo import FixtureRepo

BENCH_DB = "postgresql+psycopg://repolens:repolens@127.0.0.1:5433/repolens_bench"


def environment() -> dict[str, Any]:
    def run(*cmd: str) -> str:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout.strip()
        except OSError:
            return "unavailable"

    cpu = next(
        (
            line.split(":", 1)[1].strip()
            for line in Path("/proc/cpuinfo").read_text().splitlines()
            if line.startswith("model name")
        ),
        platform.processor(),
    )
    memory_kb = next(
        int(line.split()[1])
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith("MemTotal")
    )
    return {
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "cpu": cpu,
        "cores": os.cpu_count(),
        "memory_gb": round(memory_kb / 1024 / 1024, 1),
        "os": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "git": run("git", "--version"),
        "docker": run("docker", "version", "--format", "{{.Server.Version}}"),
        "postgres": "16 (docker compose, same machine)",
        "note": "shared development laptop; other workloads were running",
    }


class Bench:
    def __init__(self, database_url: str, data_dir: Path, local_root: Path) -> None:
        self.database_url = database_url
        engine = make_engine(database_url)
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        command.upgrade(alembic_config(database_url), "head")
        self.factory = make_session_factory(engine)
        self.settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            database_url=database_url,
            api_token=SecretStr("bench"),
            data_dir=data_dir,
            allow_local_repositories=True,
            local_repository_roots=[local_root],
            min_free_disk_mb=0,
        )
        self.worker = Worker(
            self.factory,
            build_handlers(self.settings),
            worker_id="bench",
            retry_backoff=timedelta(0),
            lease=timedelta(minutes=10),
        )
        self.parser_version = analysis_service.default_analyzer(self.settings).version

    def drain(self) -> None:
        while self.worker.run_once():
            pass

    def register(self, url: str) -> uuid.UUID:
        with transaction(self.factory) as session:
            repo, _ = repositories.register_repository(
                session, list(analysis_service.build_providers(self.settings).values()), url
            )
            return repo.id

    def analyze(self, repo_id: uuid.UUID, ref: str) -> dict[str, Any]:
        with transaction(self.factory) as session:
            run_id = analysis_service.request_analysis(
                session, repo_id, ref, settings=self.settings, parser_version=self.parser_version
            ).run.id
        started = time.perf_counter()
        self.drain()
        wall = time.perf_counter() - started
        with transaction(self.factory) as session:
            run = session.get(AnalysisRun, run_id)
            assert run is not None
            return {
                "status": run.status,
                "wall_ms": round(wall * 1000, 1),
                "counts": run.counts,
                "timings_ms": run.timings_ms,
                "snapshot_id": str(run.snapshot_id),
            }

    def impact(self, repo_id: uuid.UUID, base: str, target: str) -> dict[str, Any]:
        with transaction(self.factory) as session:
            change_id = change_analysis.request_change_analysis(
                session, repo_id, base, target
            ).change_set.id
        started = time.perf_counter()
        self.drain()
        wall = time.perf_counter() - started
        with transaction(self.factory) as session:
            change = session.get(ChangeSet, change_id)
            assert change is not None
            plan_started = time.perf_counter()
            plan = test_planning.create_test_plan(session, change_id, policy=Policy.CONSERVATIVE)
            plan_ms = (time.perf_counter() - plan_started) * 1000
            report = session.query(ImpactReport).filter_by(change_set_id=change_id).one_or_none()
            return {
                "change_set_id": str(change_id),
                "status": change.status,
                "wall_ms": round(wall * 1000, 1),
                "timings_ms": change.timings_ms,
                "summary": {
                    k: v for k, v in (change.summary or {}).items() if k != "impact"
                },
                "impact": (change.summary or {}).get("impact"),
                "report_scope": report.recommended_scope if report else None,
                "plan": {
                    "id": str(plan.id),
                    "selection_ms": round(plan_ms, 1),
                    "selected": plan.selected_tests,
                    "total": plan.total_tests,
                },
            }

    def run_tests(
        self, *, plan_id: uuid.UUID | None = None, analysis_id: uuid.UUID | None = None
    ) -> dict[str, Any]:
        with transaction(self.factory) as session:
            run, _ = execution.request_test_run(
                session,
                self.settings,
                plan_id=plan_id,
                analysis_id=analysis_id,
                full_suite=plan_id is None,
                coverage=False,
            )
            run_id = run.id
        started = time.perf_counter()
        self.drain()
        wall = time.perf_counter() - started
        with transaction(self.factory) as session:
            run = session.get(TestRun, run_id)
            assert run is not None
            return {
                "outcome": run.outcome,
                "wall_ms": round(wall * 1000, 1),
                "sandbox_ms": run.duration_ms,
                "counts": run.counts,
            }


def sandbox_startup(samples: int, workdir: Path) -> dict[str, Any]:
    project = workdir / "empty-project"
    (project / "tests").mkdir(parents=True)
    (project / "tests" / "test_noop.py").write_text("def test_noop():\n    pass\n")
    subprocess.run(["chmod", "-R", "a+rX", str(project)], check=True)
    sandbox = DockerSandbox()
    durations = []
    for _ in range(samples):
        result = sandbox.run(
            SandboxRequest(
                name=f"repolens-bench-{uuid.uuid4().hex[:8]}",
                image=Settings(_env_file=None).sandbox_image,  # type: ignore[call-arg]
                source_dir=project,
                arguments=["run"],
                limits=SandboxLimits(timeout_seconds=60),
                labels={"repolens.bench": "1"},
            ),
            lambda: None,
        )
        durations.append(result.duration_seconds * 1000)
    return {
        "samples": samples,
        "median_ms": round(statistics.median(durations), 1),
        "min_ms": round(min(durations), 1),
        "max_ms": round(max(durations), 1),
        "what": "docker run of the sandbox image with one trivial pytest test (create, run, report, remove)",
    }


def fixture_selection(bench: Bench, workdir: Path) -> dict[str, Any]:
    """Selected vs full suite on the inventory fixture for each controlled change."""
    results = {}
    cases = {
        "with_tax body change": projects.CHANGE_WITH_TAX_BODY,
        "with_tax regression": projects.REGRESSION_WITH_TAX,
        "Item.total change": projects.CHANGE_ITEM_TOTAL,
        "util-only change": projects.CHANGE_UTIL_ONLY,
        "config-only change": projects.CHANGE_CONFIG_ONLY,
    }
    for name, change in cases.items():
        repo = FixtureRepo(workdir / "repos" / uuid.uuid4().hex[:8])
        repo.commit("base", projects.INVENTORY)
        repo.tag("base")
        repo.commit("change", change)
        repo.tag("change")
        repo_id = bench.register(repo.url)
        impact = bench.impact(repo_id, "base", "change")
        with transaction(bench.factory) as session:
            change_set = session.get(ChangeSet, uuid.UUID(impact["change_set_id"]))
            assert change_set is not None and change_set.target_run_id is not None
            target_run = change_set.target_run_id
            plan = session.get(TestPlan, uuid.UUID(impact["plan"]["id"]))
            assert plan is not None
            plan_scope, selected = plan.scope, plan.selected_tests
        selected_run = (
            bench.run_tests(plan_id=uuid.UUID(impact["plan"]["id"])) if selected or plan_scope == "FULL_SUITE" else None
        )
        full_run = bench.run_tests(analysis_id=target_run)
        results[name] = {
            "scope": plan_scope,
            "selected_tests": selected,
            "total_tests": impact["plan"]["total"],
            "selection_overhead_ms": impact["plan"]["selection_ms"],
            "impact_wall_ms": impact["wall_ms"],
            "selected_run": selected_run,
            "full_run": full_run,
        }
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", action="append", default=[], help="URL@ref (GitHub)")
    parser.add_argument("--database-url", default=BENCH_DB)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--sandbox-samples", type=int, default=5)
    parser.add_argument("--skip-sandbox", action="store_true")
    parser.add_argument("--impact-depth", type=int, default=5, help="commits between base and target")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="repolens-bench-") as tmp:
        workdir = Path(tmp)
        bench = Bench(args.database_url, workdir / "data", workdir / "repos")
        report: dict[str, Any] = {"environment": environment(), "repositories": {}}
        for spec in args.repo:
            url, _, ref = spec.partition("@")
            repo_id = bench.register(url)
            cold = bench.analyze(repo_id, ref or "HEAD")
            entry: dict[str, Any] = {"ref": ref or "HEAD", "analysis": cold}
            mirror = bench.settings.mirrors_dir / f"{repo_id}.git"
            history = subprocess.run(
                ["git", "--git-dir", str(mirror), "rev-list", f"--max-count={args.impact_depth + 1}", ref or "HEAD"],
                capture_output=True, text=True, check=True,
            ).stdout.split()
            if len(history) > args.impact_depth:
                entry["impact"] = bench.impact(repo_id, history[-1], history[0])
                entry["impact"]["base"], entry["impact"]["target"] = history[-1], history[0]
            report["repositories"][url] = entry
            print(f"{url}: analysis {cold['wall_ms']} ms", file=sys.stderr)
        if not args.skip_sandbox:
            report["sandbox_startup"] = sandbox_startup(args.sandbox_samples, workdir)
            report["fixture_selection"] = fixture_selection(bench, workdir)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, default=str) + "\n")
        print(f"wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
