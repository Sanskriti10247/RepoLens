"""test catalog and plans

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "test_cases",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("analysis_run_id", sa.UUID(), nullable=False),
        sa.Column("symbol_id", sa.BigInteger(), nullable=True),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("qualified_name", sa.Text(), nullable=False),
        sa.Column("collection_status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "collection_status IN ('STATIC', 'COLLECTED', 'NOT_COLLECTED', 'COLLECTION_ERROR')",
            name=op.f("ck_test_cases_collection_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"],
            ["analysis_runs.id"],
            name=op.f("fk_test_cases_analysis_run_id_analysis_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["symbol_id"],
            ["symbols.id"],
            name=op.f("fk_test_cases_symbol_id_symbols"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_test_cases")),
        sa.UniqueConstraint(
            "analysis_run_id", "node_id", name=op.f("uq_test_cases_analysis_run_id_node_id")
        ),
    )
    op.create_table(
        "test_plans",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("change_set_id", sa.UUID(), nullable=False),
        sa.Column("analysis_run_id", sa.UUID(), nullable=False),
        sa.Column("policy", sa.Text(), nullable=False),
        sa.Column("budget", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("fallback_reason", sa.Text(), nullable=True),
        sa.Column("total_tests", sa.Integer(), nullable=False),
        sa.Column("selected_tests", sa.Integer(), nullable=False),
        sa.Column("estimated_seconds", sa.Float(), nullable=True),
        sa.Column("budget_limited", sa.Boolean(), nullable=False),
        sa.Column("evidence_sources", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "policy IN ('CONSERVATIVE', 'BUDGETED')", name=op.f("ck_test_plans_policy_valid")
        ),
        sa.CheckConstraint(
            "scope IN ('NONE', 'TARGETED', 'BROAD', 'FULL_SUITE')",
            name=op.f("ck_test_plans_scope_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"],
            ["analysis_runs.id"],
            name=op.f("fk_test_plans_analysis_run_id_analysis_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["change_set_id"],
            ["change_sets.id"],
            name=op.f("fk_test_plans_change_set_id_change_sets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_test_plans")),
    )
    op.create_index(
        op.f("ix_test_plans_change_set_id_created_at"),
        "test_plans",
        ["change_set_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "test_plan_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("plan_id", sa.UUID(), nullable=False),
        sa.Column("test_case_id", sa.BigInteger(), nullable=True),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("exclusion_reason", sa.Text(), nullable=True),
        sa.Column("estimated_seconds", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["test_plans.id"],
            name=op.f("fk_test_plan_items_plan_id_test_plans"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["test_case_id"],
            ["test_cases.id"],
            name=op.f("fk_test_plan_items_test_case_id_test_cases"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_test_plan_items")),
        sa.UniqueConstraint("plan_id", "node_id", name=op.f("uq_test_plan_items_plan_id_node_id")),
    )
    op.create_index(
        op.f("ix_test_plan_items_plan_id_selected_rank"),
        "test_plan_items",
        ["plan_id", "selected", "rank"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_test_plan_items_plan_id_selected_rank"), table_name="test_plan_items")
    op.drop_table("test_plan_items")
    op.drop_index(op.f("ix_test_plans_change_set_id_created_at"), table_name="test_plans")
    op.drop_table("test_plans")
    op.drop_table("test_cases")
