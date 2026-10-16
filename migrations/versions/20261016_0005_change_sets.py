"""change sets

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # CHECK constraints are not detected by autogenerate: widen the job kinds by hand.
    op.drop_constraint(op.f("ck_jobs_kind_valid"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_jobs_kind_valid"), "jobs", "kind IN ('ANALYZE_REPOSITORY', 'ANALYZE_CHANGE')"
    )
    op.create_table(
        "change_sets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("repository_id", sa.UUID(), nullable=False),
        sa.Column("base_ref", sa.Text(), nullable=False),
        sa.Column("target_ref", sa.Text(), nullable=False),
        sa.Column("base_sha", sa.Text(), nullable=True),
        sa.Column("target_sha", sa.Text(), nullable=True),
        sa.Column("base_run_id", sa.UUID(), nullable=True),
        sa.Column("target_run_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("diff_algorithm", sa.Text(), nullable=True),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("timings_ms", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "base_sha IS NULL OR base_sha ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'",
            name=op.f("ck_change_sets_base_sha_format"),
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name=op.f("ck_change_sets_status_valid"),
        ),
        sa.CheckConstraint(
            "target_sha IS NULL OR target_sha ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'",
            name=op.f("ck_change_sets_target_sha_format"),
        ),
        sa.ForeignKeyConstraint(
            ["base_run_id"],
            ["analysis_runs.id"],
            name=op.f("fk_change_sets_base_run_id_analysis_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_change_sets_repository_id_repositories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_run_id"],
            ["analysis_runs.id"],
            name=op.f("fk_change_sets_target_run_id_analysis_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_change_sets")),
    )
    op.create_index(
        op.f("ix_change_sets_repository_id_created_at"),
        "change_sets",
        ["repository_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "changed_files",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("change_set_id", sa.UUID(), nullable=False),
        sa.Column("change_type", sa.Text(), nullable=False),
        sa.Column("old_path", sa.Text(), nullable=True),
        sa.Column("new_path", sa.Text(), nullable=True),
        sa.Column("old_file_id", sa.BigInteger(), nullable=True),
        sa.Column("new_file_id", sa.BigInteger(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=True),
        sa.Column("mapping_status", sa.Text(), nullable=False),
        sa.Column("similarity", sa.Integer(), nullable=True),
        sa.Column("added_lines", sa.Integer(), nullable=False),
        sa.Column("deleted_lines", sa.Integer(), nullable=False),
        sa.Column(
            "old_ranges",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "new_ranges",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "change_type IN ('ADDED', 'MODIFIED', 'DELETED', 'RENAMED', 'COPIED', 'TYPE_CHANGED')",
            name=op.f("ck_changed_files_change_type_valid"),
        ),
        sa.CheckConstraint(
            "mapping_status IN ('MAPPED', 'MAPPED_WITH_PARSE_ERRORS', 'NOT_ANALYZED', 'NON_PYTHON', 'BINARY', 'SPECIAL')",
            name=op.f("ck_changed_files_mapping_status_valid"),
        ),
        sa.CheckConstraint(
            "old_path IS NOT NULL OR new_path IS NOT NULL", name=op.f("ck_changed_files_has_path")
        ),
        sa.ForeignKeyConstraint(
            ["change_set_id"],
            ["change_sets.id"],
            name=op.f("fk_changed_files_change_set_id_change_sets"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["new_file_id"],
            ["source_files.id"],
            name=op.f("fk_changed_files_new_file_id_source_files"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["old_file_id"],
            ["source_files.id"],
            name=op.f("fk_changed_files_old_file_id_source_files"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_changed_files")),
    )
    op.create_index(
        op.f("ix_changed_files_change_set_id"), "changed_files", ["change_set_id"], unique=False
    )
    op.create_table(
        "changed_symbols",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("change_set_id", sa.UUID(), nullable=False),
        sa.Column("changed_file_id", sa.BigInteger(), nullable=False),
        sa.Column("change_kind", sa.Text(), nullable=False),
        sa.Column("symbol_kind", sa.Text(), nullable=False),
        sa.Column("qualified_name", sa.Text(), nullable=False),
        sa.Column("base_symbol_id", sa.BigInteger(), nullable=True),
        sa.Column("target_symbol_id", sa.BigInteger(), nullable=True),
        sa.Column("signature_changed", sa.Boolean(), nullable=False),
        sa.Column("body_changed", sa.Boolean(), nullable=False),
        sa.Column("decorators_changed", sa.Boolean(), nullable=False),
        sa.Column("bases_changed", sa.Boolean(), nullable=False),
        sa.Column("changed_lines", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "change_kind IN ('ADDED', 'REMOVED', 'MODIFIED', 'RENAMED')",
            name=op.f("ck_changed_symbols_change_kind_valid"),
        ),
        sa.CheckConstraint(
            "base_symbol_id IS NOT NULL OR target_symbol_id IS NOT NULL",
            name=op.f("ck_changed_symbols_has_symbol"),
        ),
        sa.ForeignKeyConstraint(
            ["base_symbol_id"],
            ["symbols.id"],
            name=op.f("fk_changed_symbols_base_symbol_id_symbols"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["change_set_id"],
            ["change_sets.id"],
            name=op.f("fk_changed_symbols_change_set_id_change_sets"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["changed_file_id"],
            ["changed_files.id"],
            name=op.f("fk_changed_symbols_changed_file_id_changed_files"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_symbol_id"],
            ["symbols.id"],
            name=op.f("fk_changed_symbols_target_symbol_id_symbols"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_changed_symbols")),
    )
    op.create_index(
        op.f("ix_changed_symbols_change_set_id"), "changed_symbols", ["change_set_id"], unique=False
    )


def downgrade() -> None:
    op.execute("DELETE FROM jobs WHERE kind = 'ANALYZE_CHANGE'")
    op.drop_constraint(op.f("ck_jobs_kind_valid"), "jobs", type_="check")
    op.create_check_constraint(op.f("ck_jobs_kind_valid"), "jobs", "kind IN ('ANALYZE_REPOSITORY')")
    op.drop_index(op.f("ix_changed_symbols_change_set_id"), table_name="changed_symbols")
    op.drop_table("changed_symbols")
    op.drop_index(op.f("ix_changed_files_change_set_id"), table_name="changed_files")
    op.drop_table("changed_files")
    op.drop_index(op.f("ix_change_sets_repository_id_created_at"), table_name="change_sets")
    op.drop_table("change_sets")
