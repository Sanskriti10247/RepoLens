"""Snapshot file records and the analyzer contract used by the analysis pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.orm import Session

from repolens.persistence.models import AnalysisRun
from repolens.services.jobs import JobContext
from repolens.services.progress import NULL_PROGRESS, ProgressSink


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    id: int
    path: str
    blob_sha: str
    size_bytes: int
    language: str | None
    role: str
    mode: str
    is_binary: bool


class SnapshotAnalyzer(Protocol):
    """Turns a snapshot into analysis results inside the caller's transaction."""

    version: str

    def analyze(
        self,
        session: Session,
        run: AnalysisRun,
        files: list[SnapshotFile],
        read_blob: Any,
        ctx: JobContext,
        progress: ProgressSink = NULL_PROGRESS,
    ) -> dict[str, Any]:
        """Persist results for ``run`` and return counts. Must be deterministic."""
        ...


class NoAnalyzer:
    version = "none"

    def analyze(
        self,
        session: Session,
        run: AnalysisRun,
        files: list[SnapshotFile],
        read_blob: Any,
        ctx: JobContext,
        progress: ProgressSink = NULL_PROGRESS,
    ) -> dict[str, Any]:
        return {}
