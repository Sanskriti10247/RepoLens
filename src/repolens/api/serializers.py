"""ORM → API schema conversion."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from repolens.api.schemas import (
    AnalysisOut,
    AnalysisProgressOut,
    AnalysisSummary,
    JobOut,
    SnapshotOut,
)
from repolens.persistence.models import AnalysisRun
from repolens.services.analysis_views import AnalysisView


def analysis_summary(run: AnalysisRun, commit_sha: str | None) -> AnalysisSummary:
    summary = AnalysisSummary.model_validate(run)
    summary.commit_sha = commit_sha
    return summary


def analysis_out(view: AnalysisView) -> AnalysisOut:
    snapshot = None
    if view.snapshot is not None:
        snapshot = SnapshotOut.model_validate(view.snapshot)
        issues = view.snapshot.inventory_issues or {}
        snapshot.issue_counts = issues.get("counts", {})
        snapshot.issues_truncated = bool(issues.get("truncated", False))
    return AnalysisOut.model_validate(
        {
            **{k: getattr(view.run, k) for k in AnalysisOut.model_fields if hasattr(view.run, k)},
            "results_run_id": view.results_run_id,
            "snapshot": snapshot,
            "job": JobOut.model_validate(view.job) if view.job else None,
            "progress": _progress(view.progress),
        }
    )


def _progress(document: dict[str, Any] | None) -> AnalysisProgressOut | None:
    # A report is informational: one written by another engine version that no longer
    # matches the schema is left out rather than failing the whole response.
    if not document:
        return None
    try:
        return AnalysisProgressOut.model_validate(document)
    except ValidationError:
        return None
