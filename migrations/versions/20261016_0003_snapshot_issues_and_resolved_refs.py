"""snapshot issues and resolved refs

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("analysis_runs", sa.Column("resolved_ref", sa.Text(), nullable=True))
    op.add_column("analysis_runs", sa.Column("resolved_ref_kind", sa.Text(), nullable=True))
    op.add_column(
        "repository_snapshots",
        sa.Column("inventory_issues", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("repository_snapshots", "inventory_issues")
    op.drop_column("analysis_runs", "resolved_ref_kind")
    op.drop_column("analysis_runs", "resolved_ref")
