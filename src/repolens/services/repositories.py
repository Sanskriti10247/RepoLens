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
    session: Session, providers: list[RepositoryProvider], url: str
) -> tuple[Repository, bool]:
    """Register a repository by URL. Idempotent on the canonical URL.

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
        )
        .on_conflict_do_nothing(index_elements=["canonical_url"])
        .returning(Repository.id)
    ).scalar_one_or_none()
    repo = session.execute(
        select(Repository).where(Repository.canonical_url == locator.canonical_url)
    ).scalar_one()
    return repo, inserted is not None


def get_repository(session: Session, repository_id: uuid.UUID) -> Repository:
    repo = session.get(Repository, repository_id)
    if repo is None:
        raise NotFound("Repository not found")
    return repo


def list_repositories(session: Session, *, limit: int, offset: int) -> Page[Repository]:
    total = session.execute(select(func.count()).select_from(Repository)).scalar_one()
    items = (
        session.execute(
            select(Repository)
            .order_by(Repository.created_at.desc(), Repository.id)
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return Page(items=list(items), total=total, limit=limit, offset=offset)
