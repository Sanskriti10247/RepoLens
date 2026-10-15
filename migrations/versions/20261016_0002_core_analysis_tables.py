"""core analysis tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "repositories",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("default_branch", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repositories")),
        sa.UniqueConstraint("canonical_url", name=op.f("uq_repositories_canonical_url")),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("repository_id", sa.UUID(), nullable=True),
        sa.Column("subject_id", sa.UUID(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_category", sa.Text(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('ANALYZE_REPOSITORY')", name=op.f("ck_jobs_kind_valid")),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'CLAIMED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'TIMED_OUT', 'BLOCKED')",
            name=op.f("ck_jobs_status_valid"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('CLAIMED', 'RUNNING') OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name=op.f("ck_jobs_active_has_lease"),
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND max_attempts >= 1", name=op.f("ck_jobs_attempts_valid")
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_jobs_repository_id_repositories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index(
        "ix_jobs_active_lease",
        "jobs",
        ["lease_expires_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('CLAIMED', 'RUNNING')"),
    )
    op.create_index(
        "ix_jobs_claimable",
        "jobs",
        ["priority", "run_after"],
        unique=False,
        postgresql_where=sa.text("status = 'QUEUED'"),
    )
    op.create_index(
        op.f("ix_jobs_repository_id_status"), "jobs", ["repository_id", "status"], unique=False
    )
    op.create_index(
        "uq_jobs_active_idempotency_key",
        "jobs",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED', 'CLAIMED', 'RUNNING')"),
    )
    op.create_table(
        "repository_snapshots",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("repository_id", sa.UUID(), nullable=False),
        sa.Column("commit_sha", sa.Text(), nullable=False),
        sa.Column("tree_sha", sa.Text(), nullable=True),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=True),
        sa.Column("total_bytes", sa.BigInteger(), nullable=True),
        sa.Column("language_inventory", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("inventory_sha256", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "commit_sha ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'",
            name=op.f("ck_repository_snapshots_commit_sha_format"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'READY', 'FAILED')",
            name=op.f("ck_repository_snapshots_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_repository_snapshots_repository_id_repositories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repository_snapshots")),
        sa.UniqueConstraint(
            "repository_id",
            "commit_sha",
            name=op.f("uq_repository_snapshots_repository_id_commit_sha"),
        ),
    )
    op.create_table(
        "analysis_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("repository_id", sa.UUID(), nullable=False),
        sa.Column("requested_ref", sa.Text(), nullable=False),
        sa.Column("snapshot_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reused_run_id", sa.UUID(), nullable=True),
        sa.Column("parser_version", sa.Text(), nullable=False),
        sa.Column("engine_version", sa.Text(), nullable=False),
        sa.Column("config_fingerprint", sa.Text(), nullable=False),
        sa.Column("counts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
            "(status = 'REUSED') = (reused_run_id IS NOT NULL)",
            name=op.f("ck_analysis_runs_reused_has_source"),
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'PARTIAL', 'REUSED', 'FAILED', 'CANCELLED')",
            name=op.f("ck_analysis_runs_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_analysis_runs_repository_id_repositories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reused_run_id"],
            ["analysis_runs.id"],
            name=op.f("fk_analysis_runs_reused_run_id_analysis_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["repository_snapshots.id"],
            name=op.f("fk_analysis_runs_snapshot_id_repository_snapshots"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analysis_runs")),
    )
    op.create_index(
        op.f("ix_analysis_runs_repository_id_created_at"),
        "analysis_runs",
        ["repository_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "uq_analysis_runs_result_key",
        "analysis_runs",
        ["snapshot_id", "parser_version", "engine_version", "config_fingerprint"],
        unique=True,
        postgresql_where=sa.text("status IN ('SUCCEEDED', 'PARTIAL')"),
    )
    op.create_table(
        "source_files",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("snapshot_id", sa.UUID(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("blob_sha", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("is_binary", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "mode IN ('REGULAR', 'EXECUTABLE', 'SYMLINK', 'SUBMODULE')",
            name=op.f("ck_source_files_mode_valid"),
        ),
        sa.CheckConstraint(
            "role IN ('SOURCE', 'TEST', 'CONFIG', 'DOC', 'OTHER')",
            name=op.f("ck_source_files_role_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["repository_snapshots.id"],
            name=op.f("fk_source_files_snapshot_id_repository_snapshots"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_files")),
        sa.UniqueConstraint("snapshot_id", "path", name=op.f("uq_source_files_snapshot_id_path")),
    )
    op.create_index(
        op.f("ix_source_files_snapshot_id_language"),
        "source_files",
        ["snapshot_id", "language"],
        unique=False,
    )
    op.create_table(
        "analysis_files",
        sa.Column("analysis_run_id", sa.UUID(), nullable=False),
        sa.Column("file_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("module_name", sa.Text(), nullable=True),
        sa.Column("symbol_count", sa.Integer(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("parse_ms", sa.Float(), nullable=True),
        sa.CheckConstraint(
            "status IN ('PARSED', 'PARSED_WITH_ERRORS', 'SKIPPED_TOO_LARGE', 'SKIPPED_BINARY', 'SKIPPED_SYMLINK', 'SKIPPED_UNSUPPORTED', 'SKIPPED_ENCODING', 'FAILED')",
            name=op.f("ck_analysis_files_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"],
            ["analysis_runs.id"],
            name=op.f("fk_analysis_files_analysis_run_id_analysis_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["source_files.id"],
            name=op.f("fk_analysis_files_file_id_source_files"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("analysis_run_id", "file_id", name=op.f("pk_analysis_files")),
    )
    op.create_table(
        "analysis_warnings",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("analysis_run_id", sa.UUID(), nullable=False),
        sa.Column("file_id", sa.BigInteger(), nullable=True),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=True),
        sa.Column("end_line", sa.Integer(), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(
            "severity IN ('INFO', 'WARNING', 'ERROR')",
            name=op.f("ck_analysis_warnings_severity_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"],
            ["analysis_runs.id"],
            name=op.f("fk_analysis_warnings_analysis_run_id_analysis_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["source_files.id"],
            name=op.f("fk_analysis_warnings_file_id_source_files"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analysis_warnings")),
    )
    op.create_index(
        op.f("ix_analysis_warnings_analysis_run_id_code"),
        "analysis_warnings",
        ["analysis_run_id", "code"],
        unique=False,
    )
    op.create_table(
        "symbols",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("analysis_run_id", sa.UUID(), nullable=False),
        sa.Column("file_id", sa.BigInteger(), nullable=False),
        sa.Column("parent_id", sa.BigInteger(), nullable=True),
        sa.Column("symbol_key", sa.Text(), nullable=False),
        sa.Column("qualified_name", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("visibility", sa.Text(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("start_col", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("end_col", sa.Integer(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=True),
        sa.Column("body_fingerprint", sa.Text(), nullable=True),
        sa.Column(
            "decorators",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("doc_summary", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('MODULE', 'CLASS', 'FUNCTION', 'ASYNC_FUNCTION', 'METHOD', 'ASYNC_METHOD')",
            name=op.f("ck_symbols_kind_valid"),
        ),
        sa.CheckConstraint(
            "visibility IN ('PUBLIC', 'PRIVATE', 'DUNDER')",
            name=op.f("ck_symbols_visibility_valid"),
        ),
        sa.CheckConstraint(
            "start_line >= 1 AND (end_line, end_col) >= (start_line, start_col)",
            name=op.f("ck_symbols_span_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"],
            ["analysis_runs.id"],
            name=op.f("fk_symbols_analysis_run_id_analysis_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["source_files.id"],
            name=op.f("fk_symbols_file_id_source_files"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["symbols.id"],
            name=op.f("fk_symbols_parent_id_symbols"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_symbols")),
        sa.UniqueConstraint(
            "analysis_run_id", "symbol_key", name=op.f("uq_symbols_analysis_run_id_symbol_key")
        ),
    )
    op.create_index(
        op.f("ix_symbols_analysis_run_id_file_id"),
        "symbols",
        ["analysis_run_id", "file_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_symbols_analysis_run_id_name"),
        "symbols",
        ["analysis_run_id", "name"],
        unique=False,
    )
    op.create_index(
        op.f("ix_symbols_analysis_run_id_qualified_name"),
        "symbols",
        ["analysis_run_id", "qualified_name"],
        unique=False,
    )
    op.create_table(
        "symbol_references",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("analysis_run_id", sa.UUID(), nullable=False),
        sa.Column("file_id", sa.BigInteger(), nullable=False),
        sa.Column("source_symbol_id", sa.BigInteger(), nullable=False),
        sa.Column("ref_kind", sa.Text(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("start_col", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("end_col", sa.Integer(), nullable=False),
        sa.Column("resolution_status", sa.Text(), nullable=False),
        sa.Column("target_symbol_id", sa.BigInteger(), nullable=True),
        sa.Column("external_target", sa.Text(), nullable=True),
        sa.Column("candidates", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("resolver_rule", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(resolution_status = 'RESOLVED') = (target_symbol_id IS NOT NULL)",
            name=op.f("ck_symbol_references_resolved_has_target"),
        ),
        sa.CheckConstraint(
            "ref_kind IN ('IMPORT', 'CALL', 'INHERIT', 'DECORATOR', 'ATTRIBUTE', 'NAME')",
            name=op.f("ck_symbol_references_ref_kind_valid"),
        ),
        sa.CheckConstraint(
            "resolution_status IN ('RESOLVED', 'AMBIGUOUS', 'EXTERNAL', 'UNRESOLVED', 'DYNAMIC')",
            name=op.f("ck_symbol_references_resolution_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"],
            ["analysis_runs.id"],
            name=op.f("fk_symbol_references_analysis_run_id_analysis_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["source_files.id"],
            name=op.f("fk_symbol_references_file_id_source_files"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_symbol_id"],
            ["symbols.id"],
            name=op.f("fk_symbol_references_source_symbol_id_symbols"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_symbol_id"],
            ["symbols.id"],
            name=op.f("fk_symbol_references_target_symbol_id_symbols"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_symbol_references")),
    )
    op.create_index(
        op.f("ix_symbol_references_analysis_run_id_resolution_status"),
        "symbol_references",
        ["analysis_run_id", "resolution_status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_symbol_references_analysis_run_id_source_symbol_id"),
        "symbol_references",
        ["analysis_run_id", "source_symbol_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_symbol_references_analysis_run_id_target_symbol_id"),
        "symbol_references",
        ["analysis_run_id", "target_symbol_id"],
        unique=False,
    )
    op.create_table(
        "dependency_edges",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("analysis_run_id", sa.UUID(), nullable=False),
        sa.Column("src_symbol_id", sa.BigInteger(), nullable=False),
        sa.Column("dst_symbol_id", sa.BigInteger(), nullable=False),
        sa.Column("edge_type", sa.Text(), nullable=False),
        sa.Column("provenance", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Text(), nullable=False),
        sa.Column("reference_count", sa.Integer(), nullable=False),
        sa.Column("first_reference_id", sa.BigInteger(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(
            "confidence IN ('EXACT', 'HEURISTIC')",
            name=op.f("ck_dependency_edges_confidence_valid"),
        ),
        sa.CheckConstraint(
            "edge_type IN ('CONTAINS', 'IMPORTS', 'CALLS', 'INHERITS', 'REFERENCES', 'DEFINES_TEST', 'TESTS_SYMBOL', 'COVERED_BY_TEST')",
            name=op.f("ck_dependency_edges_edge_type_valid"),
        ),
        sa.CheckConstraint(
            "provenance IN ('SYNTAX', 'STATIC_RESOLVED', 'COVERAGE', 'EXPLICIT', 'HISTORICAL')",
            name=op.f("ck_dependency_edges_provenance_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"],
            ["analysis_runs.id"],
            name=op.f("fk_dependency_edges_analysis_run_id_analysis_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["dst_symbol_id"],
            ["symbols.id"],
            name=op.f("fk_dependency_edges_dst_symbol_id_symbols"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["first_reference_id"],
            ["symbol_references.id"],
            name=op.f("fk_dependency_edges_first_reference_id_symbol_references"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["src_symbol_id"],
            ["symbols.id"],
            name=op.f("fk_dependency_edges_src_symbol_id_symbols"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dependency_edges")),
        sa.UniqueConstraint(
            "analysis_run_id",
            "src_symbol_id",
            "dst_symbol_id",
            "edge_type",
            "provenance",
            name=op.f(
                "uq_dependency_edges_analysis_run_id_src_symbol_id_dst_symbol_id_edge_type_provenance"
            ),
        ),
    )
    op.create_index(
        op.f("ix_dependency_edges_analysis_run_id_dst_symbol_id_edge_type"),
        "dependency_edges",
        ["analysis_run_id", "dst_symbol_id", "edge_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_dependency_edges_analysis_run_id_edge_type"),
        "dependency_edges",
        ["analysis_run_id", "edge_type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_dependency_edges_analysis_run_id_edge_type"), table_name="dependency_edges"
    )
    op.drop_index(
        op.f("ix_dependency_edges_analysis_run_id_dst_symbol_id_edge_type"),
        table_name="dependency_edges",
    )
    op.drop_table("dependency_edges")
    op.drop_index(
        op.f("ix_symbol_references_analysis_run_id_target_symbol_id"),
        table_name="symbol_references",
    )
    op.drop_index(
        op.f("ix_symbol_references_analysis_run_id_source_symbol_id"),
        table_name="symbol_references",
    )
    op.drop_index(
        op.f("ix_symbol_references_analysis_run_id_resolution_status"),
        table_name="symbol_references",
    )
    op.drop_table("symbol_references")
    op.drop_index(op.f("ix_symbols_analysis_run_id_qualified_name"), table_name="symbols")
    op.drop_index(op.f("ix_symbols_analysis_run_id_name"), table_name="symbols")
    op.drop_index(op.f("ix_symbols_analysis_run_id_file_id"), table_name="symbols")
    op.drop_table("symbols")
    op.drop_index(op.f("ix_analysis_warnings_analysis_run_id_code"), table_name="analysis_warnings")
    op.drop_table("analysis_warnings")
    op.drop_table("analysis_files")
    op.drop_index(op.f("ix_source_files_snapshot_id_language"), table_name="source_files")
    op.drop_table("source_files")
    op.drop_index(
        "uq_analysis_runs_result_key",
        table_name="analysis_runs",
        postgresql_where=sa.text("status IN ('SUCCEEDED', 'PARTIAL')"),
    )
    op.drop_index(op.f("ix_analysis_runs_repository_id_created_at"), table_name="analysis_runs")
    op.drop_table("analysis_runs")
    op.drop_table("repository_snapshots")
    op.drop_index(
        "uq_jobs_active_idempotency_key",
        table_name="jobs",
        postgresql_where=sa.text("status IN ('QUEUED', 'CLAIMED', 'RUNNING')"),
    )
    op.drop_index(op.f("ix_jobs_repository_id_status"), table_name="jobs")
    op.drop_index(
        "ix_jobs_claimable", table_name="jobs", postgresql_where=sa.text("status = 'QUEUED'")
    )
    op.drop_index(
        "ix_jobs_active_lease",
        table_name="jobs",
        postgresql_where=sa.text("status IN ('CLAIMED', 'RUNNING')"),
    )
    op.drop_table("jobs")
    op.drop_table("repositories")
