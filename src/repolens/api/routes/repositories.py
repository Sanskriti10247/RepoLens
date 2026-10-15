from __future__ import annotations

import uuid

from fastapi import APIRouter, Request, Response, status

from repolens.api.dependencies import Authorized, SessionDep, SettingsDep
from repolens.api.pagination import Pagination
from repolens.api.schemas import (
    AnalysisCreate,
    AnalysisOut,
    AnalysisPage,
    AnalysisSummary,
    PageMeta,
    RepositoryCreate,
    RepositoryOut,
    RepositoryPage,
)
from repolens.api.serializers import analysis_out, analysis_summary
from repolens.services import analysis as analysis_service
from repolens.services import analysis_views
from repolens.services import repositories as repo_service

router = APIRouter(prefix="/repositories", tags=["repositories"])


def _repository_out(repo: object, latest: AnalysisSummary | None) -> RepositoryOut:
    out = RepositoryOut.model_validate(repo)
    out.latest_analysis = latest
    return out


@router.post(
    "",
    response_model=RepositoryOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Authorized],
    responses={200: {"model": RepositoryOut, "description": "Already registered"}},
)
def register_repository(
    body: RepositoryCreate, request: Request, response: Response, session: SessionDep
) -> RepositoryOut:
    """Register a repository by URL. Validates the URL only; nothing is fetched yet."""
    with session.begin():
        repo, created = repo_service.register_repository(
            session, request.app.state.providers, body.url
        )
    if not created:
        response.status_code = status.HTTP_200_OK
    return _repository_out(repo, None)


@router.get("", response_model=RepositoryPage)
def list_repositories(session: SessionDep, page: Pagination) -> RepositoryPage:
    result = repo_service.list_repositories(session, limit=page.limit, offset=page.offset)
    latest = analysis_views.latest_analyses(session, [r.id for r in result.items])
    items = [
        _repository_out(r, analysis_summary(*latest[r.id]) if r.id in latest else None)
        for r in result.items
    ]
    return RepositoryPage(
        items=items, page=PageMeta(total=result.total, limit=result.limit, offset=result.offset)
    )


@router.get("/{repository_id}", response_model=RepositoryOut)
def get_repository(repository_id: uuid.UUID, session: SessionDep) -> RepositoryOut:
    repo = repo_service.get_repository(session, repository_id)
    latest = analysis_views.latest_analyses(session, [repo.id]).get(repo.id)
    return _repository_out(repo, analysis_summary(*latest) if latest else None)


@router.post(
    "/{repository_id}/analyses",
    response_model=AnalysisOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Authorized],
)
def request_analysis(
    repository_id: uuid.UUID,
    body: AnalysisCreate,
    session: SessionDep,
    settings: SettingsDep,
    request: Request,
) -> AnalysisOut:
    """Queue an analysis of ``ref``. Repeating an in-flight request returns the same run."""
    with session.begin():
        result = analysis_service.request_analysis(
            session,
            repository_id,
            body.ref,
            settings=settings,
            parser_version=request.app.state.parser_version,
        )
        run_id = result.run.id
    return analysis_out(analysis_views.get_analysis(session, run_id))


@router.get("/{repository_id}/analyses", response_model=AnalysisPage)
def list_analyses(repository_id: uuid.UUID, session: SessionDep, page: Pagination) -> AnalysisPage:
    repo_service.get_repository(session, repository_id)
    result = analysis_views.list_analyses(
        session, repository_id, limit=page.limit, offset=page.offset
    )
    return AnalysisPage(
        items=[analysis_summary(run, sha) for run, sha in result.items],
        page=PageMeta(total=result.total, limit=result.limit, offset=result.offset),
    )
