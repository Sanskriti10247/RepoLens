"""source file line counts

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("source_files", sa.Column("line_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("source_files", "line_count")
