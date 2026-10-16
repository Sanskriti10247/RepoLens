"""Change and impact analysis endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from repolens.api.dependencies import Authorized, SessionDep
from repolens.api.pagination import Pagination
from repolens.api.schemas import (
    ChangedFileOut,
    ChangedFilePage,
    ChangedSymbolOut,
    ChangedSymbolPage,
    ChangeSetOut,
    ChangeSetPage,
    ImpactCreate,
    JobOut,
    PageMeta,
)
from repolens.persistence.models import ChangeSet, Job
from repolens.services import change_analysis, change_views

router = APIRouter(tags=["impact"])


def _change_out(change: ChangeSet, job: Job | None) -> ChangeSetOut:
    out = ChangeSetOut.model_validate(change)
    out.job = JobOut.model_validate(job) if job else None
    return out


@router.post(
    "/repositories/{repository_id}/impact",
    response_model=ChangeSetOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Authorized],
)
def request_impact(
    repository_id: uuid.UUID, body: ImpactCreate, session: SessionDep
) -> ChangeSetOut:
    """Compare ``base`` with ``target``. Both are analysed first if needed."""
    with session.begin():
        result = change_analysis.request_change_analysis(
            session, repository_id, body.base, body.target
        )
        change_id = result.change_set.id
    return _change_out(*change_views.get_change_set(session, change_id))


@router.get("/repositories/{repository_id}/impact", response_model=ChangeSetPage)
def list_impact(repository_id: uuid.UUID, session: SessionDep, page: Pagination) -> ChangeSetPage:
    result = change_views.list_change_sets(
        session, repository_id, limit=page.limit, offset=page.offset
    )
    return ChangeSetPage(
        items=[ChangeSetOut.model_validate(c) for c in result.items],
        page=PageMeta(total=result.total, limit=result.limit, offset=result.offset),
    )


@router.get("/impact/{impact_id}", response_model=ChangeSetOut)
def get_impact(impact_id: uuid.UUID, session: SessionDep) -> ChangeSetOut:
    return _change_out(*change_views.get_change_set(session, impact_id))


@router.post("/impact/{impact_id}/cancel", response_model=ChangeSetOut, dependencies=[Authorized])
def cancel_impact(impact_id: uuid.UUID, session: SessionDep) -> ChangeSetOut:
    with session.begin():
        change_analysis.cancel_change_analysis(session, impact_id)
    return _change_out(*change_views.get_change_set(session, impact_id))


@router.get("/impact/{impact_id}/files", response_model=ChangedFilePage)
def impact_files(
    impact_id: uuid.UUID, session: SessionDep, page: Pagination, mapping_status: str | None = None
) -> ChangedFilePage:
    result = change_views.changed_files(
        session, impact_id, mapping_status=mapping_status, limit=page.limit, offset=page.offset
    )
    return ChangedFilePage(
        items=[ChangedFileOut.model_validate(f) for f in result.items],
        page=PageMeta(total=result.total, limit=result.limit, offset=result.offset),
    )


@router.get("/impact/{impact_id}/changed-symbols", response_model=ChangedSymbolPage)
def impact_changed_symbols(
    impact_id: uuid.UUID, session: SessionDep, page: Pagination, change_kind: str | None = None
) -> ChangedSymbolPage:
    result = change_views.changed_symbols(
        session, impact_id, change_kind=change_kind, limit=page.limit, offset=page.offset
    )
    return ChangedSymbolPage(
        items=[ChangedSymbolOut.model_validate(s) for s in result.items],
        page=PageMeta(total=result.total, limit=result.limit, offset=result.offset),
    )
