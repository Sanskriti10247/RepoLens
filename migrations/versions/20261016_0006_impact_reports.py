"""impact reports

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "impact_reports",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("change_set_id", sa.UUID(), nullable=False),
        sa.Column("engine_version", sa.Text(), nullable=False),
        sa.Column("completeness", sa.Text(), nullable=False),
        sa.Column("completeness_reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("risk_level", sa.Text(), nullable=False),
        sa.Column("risk_factors", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("recommended_scope", sa.Text(), nullable=False),
        sa.Column("scope_reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("counts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("limits", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("uncertainty", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "completeness IN ('COMPLETE', 'PARTIAL', 'UNKNOWN')",
            name=op.f("ck_impact_reports_completeness_valid"),
        ),
        sa.CheckConstraint(
            "recommended_scope IN ('NONE', 'TARGETED', 'BROAD', 'FULL_SUITE')",
            name=op.f("ck_impact_reports_recommended_scope_valid"),
        ),
        sa.CheckConstraint(
            "risk_level IN ('LOW', 'MEDIUM', 'HIGH')",
            name=op.f("ck_impact_reports_risk_level_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["change_set_id"],
            ["change_sets.id"],
            name=op.f("fk_impact_reports_change_set_id_change_sets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_impact_reports")),
        sa.UniqueConstraint("change_set_id", name=op.f("uq_impact_reports_change_set_id")),
    )
    op.create_table(
        "impact_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("report_id", sa.UUID(), nullable=False),
        sa.Column("target_symbol_id", sa.BigInteger(), nullable=True),
        sa.Column("base_symbol_id", sa.BigInteger(), nullable=True),
        sa.Column("qualified_name", sa.Text(), nullable=False),
        sa.Column("symbol_kind", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("is_test", sa.Boolean(), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("strength", sa.Text(), nullable=False),
        sa.Column("seed_qualified_name", sa.Text(), nullable=False),
        sa.Column("seed_reason", sa.Text(), nullable=False),
        sa.Column("graph", sa.Text(), nullable=False),
        sa.Column("explanation", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "strength IN ('CHANGED', 'DIRECT', 'TRANSITIVE', 'IMPORT_ONLY', 'HEURISTIC')",
            name=op.f("ck_impact_items_strength_valid"),
        ),
        sa.CheckConstraint(
            "target_symbol_id IS NOT NULL OR base_symbol_id IS NOT NULL",
            name=op.f("ck_impact_items_has_symbol"),
        ),
        sa.ForeignKeyConstraint(
            ["base_symbol_id"],
            ["symbols.id"],
            name=op.f("fk_impact_items_base_symbol_id_symbols"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["impact_reports.id"],
            name=op.f("fk_impact_items_report_id_impact_reports"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_symbol_id"],
            ["symbols.id"],
            name=op.f("fk_impact_items_target_symbol_id_symbols"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_impact_items")),
    )
    op.create_index(
        op.f("ix_impact_items_report_id_depth"),
        "impact_items",
        ["report_id", "depth"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_impact_items_report_id_depth"), table_name="impact_items")
    op.drop_table("impact_items")
    op.drop_table("impact_reports")
