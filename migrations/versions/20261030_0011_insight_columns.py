"""insight columns: symbol complexity, type-checking imports

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("symbols", sa.Column("complexity", sa.Integer(), nullable=True))
    op.add_column("symbol_references", sa.Column("type_checking", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("symbol_references", "type_checking")
    op.drop_column("symbols", "complexity")
