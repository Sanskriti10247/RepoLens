"""Read-side queries for change sets."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from repolens.persistence.models import ChangedFile, ChangedSymbol, ChangeSet, Job
from repolens.services.errors import NotFound
from repolens.services.repositories import Page


def get_change_set(session: Session, change_id: uuid.UUID) -> tuple[ChangeSet, Job | None]:
    change = session.get(ChangeSet, change_id)
    if change is None:
        raise NotFound("Impact analysis not found")
    job = session.execute(
        select(Job).where(Job.subject_id == change_id).order_by(Job.created_at.desc()).limit(1)
    ).scalar_one_or_none()
    return change, job


def list_change_sets(
    session: Session, repository_id: uuid.UUID, *, limit: int, offset: int
) -> Page[ChangeSet]:
    where = ChangeSet.repository_id == repository_id
    total = session.execute(select(func.count()).select_from(ChangeSet).where(where)).scalar_one()
    items = session.execute(
        select(ChangeSet)
        .where(where)
        .order_by(ChangeSet.created_at.desc(), ChangeSet.id)
        .limit(limit)
        .offset(offset)
    ).scalars()
    return Page(items=list(items), total=total, limit=limit, offset=offset)


def changed_files(
    session: Session,
    change_id: uuid.UUID,
    *,
    mapping_status: str | None,
    limit: int,
    offset: int,
) -> Page[ChangedFile]:
    get_change_set(session, change_id)
    stmt = select(ChangedFile).where(ChangedFile.change_set_id == change_id)
    if mapping_status:
        stmt = stmt.where(ChangedFile.mapping_status == mapping_status)
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    items = session.execute(
        stmt.order_by(func.coalesce(ChangedFile.new_path, ChangedFile.old_path), ChangedFile.id)
        .limit(limit)
        .offset(offset)
    ).scalars()
    return Page(items=list(items), total=total, limit=limit, offset=offset)


def changed_symbols(
    session: Session,
    change_id: uuid.UUID,
    *,
    change_kind: str | None,
    limit: int,
    offset: int,
) -> Page[ChangedSymbol]:
    get_change_set(session, change_id)
    stmt = select(ChangedSymbol).where(ChangedSymbol.change_set_id == change_id)
    if change_kind:
        stmt = stmt.where(ChangedSymbol.change_kind == change_kind)
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    items = session.execute(
        stmt.order_by(ChangedSymbol.qualified_name, ChangedSymbol.id).limit(limit).offset(offset)
    ).scalars()
    return Page(items=list(items), total=total, limit=limit, offset=offset)
