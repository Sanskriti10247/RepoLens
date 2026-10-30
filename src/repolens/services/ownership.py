"""Per-user ownership of repositories and everything recorded under them.

In user mode every resource reachable through the API belongs to one repository, and each
repository to the user who added it. ``require_owned`` is the one check the API applies to
every identifier a request carries; ``owned_repository_ids`` filters list endpoints.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from repolens.persistence.models import AnalysisRun, ChangeSet, Job, Repository, TestPlan, TestRun
from repolens.services.errors import NotFound

# Request parameter name -> the joins from ``repositories`` to that resource, and its id column.
_JOINS: dict[str, tuple[list[tuple[Any, Any]], Any]] = {
    "repository_id": ([], Repository.id),
    "analysis_id": ([(AnalysisRun, AnalysisRun.repository_id == Repository.id)], AnalysisRun.id),
    "impact_id": ([(ChangeSet, ChangeSet.repository_id == Repository.id)], ChangeSet.id),
    "plan_id": (
        [
            (ChangeSet, ChangeSet.repository_id == Repository.id),
            (TestPlan, TestPlan.change_set_id == ChangeSet.id),
        ],
        TestPlan.id,
    ),
    "run_id": ([(TestRun, TestRun.repository_id == Repository.id)], TestRun.id),
    "job_id": ([(Job, Job.repository_id == Repository.id)], Job.id),
}
OWNED_PARAMETERS = frozenset(_JOINS)


def _owner_query(name: str, value: uuid.UUID) -> Select[uuid.UUID | None]:
    joins, id_column = _JOINS[name]
    stmt = select(Repository.owner_id)
    for model, onclause in joins:
        stmt = stmt.join(model, onclause)
    return stmt.where(id_column == value)


def require_owned(
    session: Session, user_id: uuid.UUID, ids: Iterable[tuple[str, uuid.UUID]]
) -> None:
    """Raise 404 unless every identifier belongs to a repository ``user_id`` added.

    Someone else's resource is indistinguishable from a missing one.
    """
    for name, value in ids:
        owner = session.execute(_owner_query(name, value)).scalar_one_or_none()
        if owner != user_id:
            raise NotFound("Not found")


def owned_repository_ids(user_id: uuid.UUID) -> Select[uuid.UUID]:
    return select(Repository.id).where(Repository.owner_id == user_id)
