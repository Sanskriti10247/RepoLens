"""Registry of job handlers available to the worker process."""

from __future__ import annotations

from repolens.config import Settings
from repolens.domain.enums import JobKind
from repolens.worker.runtime import JobHandler


def build_handlers(settings: Settings) -> dict[JobKind, JobHandler]:
    del settings  # handlers are registered as their services are implemented
    return {}
