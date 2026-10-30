"""repository owner and mirror re-fetch jobs

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = "'ANALYZE_REPOSITORY', 'ANALYZE_CHANGE', 'RUN_TESTS'"


def upgrade() -> None:
    op.add_column("repositories", sa.Column("owner_id", postgresql.UUID(), nullable=True))
    op.drop_constraint(op.f("uq_repositories_canonical_url"), "repositories", type_="unique")
    op.create_unique_constraint(
        op.f("uq_repositories_owner_id_canonical_url"),
        "repositories",
        ["owner_id", "canonical_url"],
        postgresql_nulls_not_distinct=True,
    )
    op.drop_constraint(op.f("ck_jobs_kind_valid"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_jobs_kind_valid"), "jobs", f"kind IN ({KINDS}, 'FETCH_MIRROR')"
    )


def downgrade() -> None:
    # Signed-in users' repositories cannot be represented without an owner; dropping them
    # (with their results, by cascade) keeps them from becoming visible to the operator.
    op.execute("DELETE FROM jobs WHERE kind = 'FETCH_MIRROR'")
    op.drop_constraint(op.f("ck_jobs_kind_valid"), "jobs", type_="check")
    op.create_check_constraint(op.f("ck_jobs_kind_valid"), "jobs", f"kind IN ({KINDS})")
    op.execute("DELETE FROM repositories WHERE owner_id IS NOT NULL")
    op.drop_constraint(
        op.f("uq_repositories_owner_id_canonical_url"), "repositories", type_="unique"
    )
    op.create_unique_constraint(
        op.f("uq_repositories_canonical_url"), "repositories", ["canonical_url"]
    )
    op.drop_column("repositories", "owner_id")
