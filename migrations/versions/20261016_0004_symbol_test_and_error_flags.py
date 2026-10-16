"""symbol test and error flags

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "symbols",
        sa.Column("is_test", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "symbols",
        sa.Column(
            "has_parse_errors", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("symbols", "has_parse_errors")
    op.drop_column("symbols", "is_test")
