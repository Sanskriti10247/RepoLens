"""Repository registration and lookup."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from repolens.persistence.models import Repository
from repolens.repository.providers import RepositoryProvider, locate_with
from repolens.repository.validation import ValidationError
from repolens.services.errors import InvalidInput, NotFound


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: list[T]
    total: int
    limit: int
    offset: int


def register_repository(
    session: Session,
    providers: list[RepositoryProvider],
    url: str,
    owner_id: uuid.UUID | None = None,
) -> tuple[Repository, bool]:
    """Register a repository by URL. Idempotent on the owner and canonical URL.

    Only validates the URL; nothing is fetched here (fetching happens in the worker).
    """
    try:
        provider, locator = locate_with(providers, url)
    except ValidationError as exc:
        raise InvalidInput(str(exc), code="invalid_repository_url") from exc
    inserted = session.execute(
        insert(Repository)
        .values(
            id=uuid.uuid4(),
            provider=provider.name,
            host=locator.host,
            owner=locator.owner,
            name=locator.name,
            canonical_url=locator.canonical_url,
            owner_id=owner_id,
        )
        .on_conflict_do_nothing(index_elements=["owner_id", "canonical_url"])
        .returning(Repository.id)
    ).scalar_one_or_none()
    repo = session.execute(
        select(Repository).where(
            Repository.canonical_url == locator.canonical_url,
            Repository.owner_id.is_not_distinct_from(owner_id),
        )
    ).scalar_one()
    return repo, inserted is not None


def get_repository(session: Session, repository_id: uuid.UUID) -> Repository:
    repo = session.get(Repository, repository_id)
    if repo is None:
        raise NotFound("Repository not found")
    return repo


def list_repositories(
    session: Session, *, limit: int, offset: int, owner_id: uuid.UUID | None = None
) -> Page[Repository]:
    """All repositories, or only ``owner_id``'s in user mode."""
    stmt = select(Repository)
    if owner_id is not None:
        stmt = stmt.where(Repository.owner_id == owner_id)
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    items = (
        session.execute(
            stmt.order_by(Repository.created_at.desc(), Repository.id).limit(limit).offset(offset)
        )
        .scalars()
        .all()
    )
    return Page(items=list(items), total=total, limit=limit, offset=offset)
