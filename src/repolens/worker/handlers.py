"""Registry of job handlers available to the worker process."""

from __future__ import annotations

from repolens.config import Settings
from repolens.domain.enums import JobKind
from repolens.services.analysis import AnalyzeRepositoryHandler, build_ingestion_deps
from repolens.services.jobs import JobHandler


def build_handlers(settings: Settings) -> dict[JobKind, JobHandler]:
    return {JobKind.ANALYZE_REPOSITORY: AnalyzeRepositoryHandler(build_ingestion_deps(settings))}
