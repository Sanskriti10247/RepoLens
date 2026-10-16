"""test runs

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_jobs_kind_valid"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_jobs_kind_valid"),
        "jobs",
        "kind IN ('ANALYZE_REPOSITORY', 'ANALYZE_CHANGE', 'RUN_TESTS')",
    )
    op.create_table(
        "test_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("repository_id", sa.UUID(), nullable=False),
        sa.Column("analysis_run_id", sa.UUID(), nullable=False),
        sa.Column("snapshot_id", sa.UUID(), nullable=False),
        sa.Column("commit_sha", sa.Text(), nullable=False),
        sa.Column("plan_id", sa.UUID(), nullable=True),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("requested_node_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("coverage", sa.Boolean(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("outcome_reason", sa.Text(), nullable=True),
        sa.Column("pytest_exit_code", sa.Integer(), nullable=True),
        sa.Column("container_exit_code", sa.Integer(), nullable=True),
        sa.Column("counts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("image", sa.Text(), nullable=False),
        sa.Column("limits", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("stdout_tail", sa.Text(), nullable=True),
        sa.Column("stderr_tail", sa.Text(), nullable=True),
        sa.Column("output_truncated", sa.Boolean(), nullable=False),
        sa.Column("cleanup_ok", sa.Boolean(), nullable=True),
        sa.Column("collected_node_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'COMPLETED') = (outcome IS NOT NULL)",
            name=op.f("ck_test_runs_completed_has_outcome"),
        ),
        sa.CheckConstraint(
            "commit_sha ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'",
            name=op.f("ck_test_runs_commit_sha_format"),
        ),
        sa.CheckConstraint(
            "mode IN ('SELECTED', 'FULL_SUITE', 'COLLECT')", name=op.f("ck_test_runs_mode_valid")
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('PASSED', 'FAILED', 'TIMED_OUT', 'CANCELLED', 'INFRASTRUCTURE_ERROR', 'COLLECTION_ERROR', 'SECURITY_POLICY_BLOCKED', 'RESOURCE_LIMIT_EXCEEDED')",
            name=op.f("ck_test_runs_outcome_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED')", name=op.f("ck_test_runs_status_valid")
        ),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"],
            ["analysis_runs.id"],
            name=op.f("fk_test_runs_analysis_run_id_analysis_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["test_plans.id"],
            name=op.f("fk_test_runs_plan_id_test_plans"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_test_runs_repository_id_repositories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["repository_snapshots.id"],
            name=op.f("fk_test_runs_snapshot_id_repository_snapshots"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_test_runs")),
    )
    op.create_index(op.f("ix_test_runs_plan_id"), "test_runs", ["plan_id"], unique=False)
    op.create_index(
        op.f("ix_test_runs_repository_id_created_at"),
        "test_runs",
        ["repository_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "coverage_records",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("test_run_id", sa.UUID(), nullable=False),
        sa.Column("test_node_id", sa.Text(), nullable=True),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("lines", postgresql.ARRAY(sa.Integer()), nullable=False),
        sa.ForeignKeyConstraint(
            ["test_run_id"],
            ["test_runs.id"],
            name=op.f("fk_coverage_records_test_run_id_test_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_coverage_records")),
    )
    op.create_index(
        op.f("ix_coverage_records_test_run_id_path"),
        "coverage_records",
        ["test_run_id", "path"],
        unique=False,
    )
    op.create_table(
        "test_results",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("test_run_id", sa.UUID(), nullable=False),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "outcome IN ('PASSED', 'FAILED', 'ERROR', 'SKIPPED')",
            name=op.f("ck_test_results_outcome_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["test_run_id"],
            ["test_runs.id"],
            name=op.f("fk_test_results_test_run_id_test_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_test_results")),
    )
    op.create_index(op.f("ix_test_results_node_id"), "test_results", ["node_id"], unique=False)
    op.create_index(
        op.f("ix_test_results_test_run_id"), "test_results", ["test_run_id"], unique=False
    )


def downgrade() -> None:
    op.execute("DELETE FROM jobs WHERE kind = 'RUN_TESTS'")
    op.drop_constraint(op.f("ck_jobs_kind_valid"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_jobs_kind_valid"), "jobs", "kind IN ('ANALYZE_REPOSITORY', 'ANALYZE_CHANGE')"
    )
    op.drop_index(op.f("ix_test_results_test_run_id"), table_name="test_results")
    op.drop_index(op.f("ix_test_results_node_id"), table_name="test_results")
    op.drop_table("test_results")
    op.drop_index(op.f("ix_coverage_records_test_run_id_path"), table_name="coverage_records")
    op.drop_table("coverage_records")
    op.drop_index(op.f("ix_test_runs_repository_id_created_at"), table_name="test_runs")
    op.drop_index(op.f("ix_test_runs_plan_id"), table_name="test_runs")
    op.drop_table("test_runs")
