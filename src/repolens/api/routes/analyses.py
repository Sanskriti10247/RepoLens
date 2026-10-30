from __future__ import annotations

import uuid

from fastapi import APIRouter

from repolens.api.dependencies import Authorized, SessionDep
from repolens.api.schemas import AnalysisOut, InsightsOut, SnapshotIssue
from repolens.api.serializers import analysis_out
from repolens.services import analysis as analysis_service
from repolens.services import analysis_views, insights

router = APIRouter(prefix="/analyses", tags=["analyses"])


@router.get("/{analysis_id}", response_model=AnalysisOut)
def get_analysis(analysis_id: uuid.UUID, session: SessionDep) -> AnalysisOut:
    return analysis_out(analysis_views.get_analysis(session, analysis_id))


@router.post("/{analysis_id}/cancel", response_model=AnalysisOut, dependencies=[Authorized])
def cancel_analysis(analysis_id: uuid.UUID, session: SessionDep) -> AnalysisOut:
    """Cancel a queued analysis immediately, or request cancellation of a running one."""
    with session.begin():
        analysis_service.cancel_analysis(session, analysis_id)
    return analysis_out(analysis_views.get_analysis(session, analysis_id))


@router.get("/{analysis_id}/snapshot-issues", response_model=list[SnapshotIssue])
def snapshot_issues(analysis_id: uuid.UUID, session: SessionDep) -> list[SnapshotIssue]:
    """Inventory findings for the analysed snapshot (unsafe paths, submodules, ...)."""
    view = analysis_views.get_analysis(session, analysis_id)
    issues = (view.snapshot.inventory_issues or {}) if view.snapshot else {}
    return [SnapshotIssue.model_validate(item) for item in issues.get("items", [])]


@router.get("/{analysis_id}/insights", response_model=InsightsOut)
def get_insights(analysis_id: uuid.UUID, session: SessionDep) -> InsightsOut:
    """Health score, metrics and findings computed from the analysis results.

    Returns 409 ``analysis_not_ready`` until the analysis has finished with results.
    """
    return InsightsOut.model_validate(insights.analysis_insights(session, analysis_id))
