"""Registry of job handlers available to the worker process."""

from __future__ import annotations

from repolens.config import Settings
from repolens.domain.enums import JobKind
from repolens.services.analysis import AnalyzeRepositoryHandler, build_ingestion_deps
from repolens.services.change_analysis import AnalyzeChangeHandler
from repolens.services.impact import impact_stage
from repolens.services.jobs import JobHandler


def build_handlers(settings: Settings) -> dict[JobKind, JobHandler]:
    deps = build_ingestion_deps(settings)
    return {
        JobKind.ANALYZE_REPOSITORY: AnalyzeRepositoryHandler(deps),
        JobKind.ANALYZE_CHANGE: AnalyzeChangeHandler(deps, stages=[impact_stage]),
    }
