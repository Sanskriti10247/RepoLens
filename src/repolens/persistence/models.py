"""ORM models. Every table is created through an Alembic migration, never ``create_all``.

Identity rules:
- A snapshot is ``(repository, commit SHA)`` and is immutable once READY.
- Analysis results (symbols, references, edges, warnings) belong to an *analysis run*, i.e. a
  snapshot analysed by a given parser version, engine version and configuration. A parser
  upgrade therefore produces a new run instead of mutating earlier results.
- Symbol identity is ``(analysis_run_id, symbol_key)``. Line numbers are never identity.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from repolens.domain.enums import (
    AnalysisStatus,
    ChangeMappingStatus,
    ChangeSetStatus,
    Confidence,
    EdgeType,
    FileAnalysisStatus,
    FileMode,
    FileRole,
    JobKind,
    JobStatus,
    Provenance,
    ReferenceKind,
    ResolutionStatus,
    Severity,
    SnapshotStatus,
    SymbolKind,
    Visibility,
)
from repolens.persistence.base import Base


def enum_check(column: str, enum: type[StrEnum], *, nullable: bool = False) -> CheckConstraint:
    values = ", ".join(f"'{member.value}'" for member in enum)
    condition = f"{column} IN ({values})"
    if nullable:
        condition = f"{column} IS NULL OR {condition}"
    return CheckConstraint(condition, name=f"{column}_valid")


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _bigint_pk() -> Mapped[int]:
    return mapped_column(BigInteger, Identity(always=True), primary_key=True)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


SHA_CHECK = "~ '^([0-9a-f]{40}|[0-9a-f]{64})$'"  # SHA-1 and SHA-256 object formats
# Literal predicate shared by the partial index and ON CONFLICT inference (which cannot use
# bound parameters).
JOB_NOT_TERMINAL_SQL = "status IN ('QUEUED', 'CLAIMED', 'RUNNING')"


class Repository(Base):
    __tablename__ = "repositories"
    __table_args__ = (
        # One registration per URL and owner; NULL (operator mode) counts as one owner.
        UniqueConstraint("owner_id", "canonical_url", postgresql_nulls_not_distinct=True),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    # Signed-in user (Supabase ``sub``) who added it; NULL in single-operator mode.
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    host: Mapped[str] = mapped_column(Text, nullable=False)
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    default_branch: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class RepositorySnapshot(Base):
    __tablename__ = "repository_snapshots"
    __table_args__ = (
        UniqueConstraint("repository_id", "commit_sha"),
        CheckConstraint(f"commit_sha {SHA_CHECK}", name="commit_sha_format"),
        enum_check("status", SnapshotStatus),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    commit_sha: Mapped[str] = mapped_column(Text, nullable=False)
    tree_sha: Mapped[str | None] = mapped_column(Text)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False, default=SnapshotStatus.PENDING)
    file_count: Mapped[int | None] = mapped_column(Integer)
    total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    language_inventory: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    inventory_sha256: Mapped[str | None] = mapped_column(Text)
    # Snapshot-level findings (unsafe paths, unhashed files, submodules), capped in size.
    inventory_issues: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourceFile(Base):
    """A file in a snapshot. Facts only; per-analysis outcomes live in ``analysis_files``."""

    __tablename__ = "source_files"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "path"),
        enum_check("role", FileRole),
        enum_check("mode", FileMode),
        Index(None, "snapshot_id", "language"),
    )

    id: Mapped[int] = _bigint_pk()
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repository_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)
    blob_sha: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    is_binary: Mapped[bool] = mapped_column(nullable=False, default=False)
    line_count: Mapped[int | None] = mapped_column(Integer)  # None: binary or not read


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"
    __table_args__ = (
        enum_check("status", AnalysisStatus),
        # At most one usable result per (snapshot, parser, engine, config): enables reuse.
        Index(
            "uq_analysis_runs_result_key",
            "snapshot_id",
            "parser_version",
            "engine_version",
            "config_fingerprint",
            unique=True,
            postgresql_where=text("status IN ('SUCCEEDED', 'PARTIAL')"),
        ),
        Index(None, "repository_id", "created_at"),
        CheckConstraint(
            "(status = 'REUSED') = (reused_run_id IS NOT NULL)", name="reused_has_source"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    requested_ref: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_ref: Mapped[str | None] = mapped_column(Text)  # e.g. refs/heads/main
    resolved_ref_kind: Mapped[str | None] = mapped_column(Text)  # BRANCH | TAG | COMMIT
    # Unknown until the worker resolves the ref to a commit.
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("repository_snapshots.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default=AnalysisStatus.QUEUED)
    reused_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE")
    )
    parser_version: Mapped[str] = mapped_column(Text, nullable=False)
    engine_version: Mapped[str] = mapped_column(Text, nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    counts: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    timings_ms: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AnalysisProgress(Base):
    """The latest progress snapshot of a running analysis, one row per run.

    Kept out of ``analysis_runs`` on purpose: the analysis transaction holds the run row
    ``FOR UPDATE`` for its whole duration, so progress written from a separate short
    transaction would wait on that lock. The row is created before the lock is taken and only
    ``progress`` is updated afterwards, which does not touch the referenced run row.
    """

    __tablename__ = "analysis_progress"

    analysis_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), primary_key=True
    )
    progress: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AnalysisFile(Base):
    __tablename__ = "analysis_files"
    __table_args__ = (enum_check("status", FileAnalysisStatus),)

    analysis_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), primary_key=True
    )
    file_id: Mapped[int] = mapped_column(
        ForeignKey("source_files.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    module_name: Mapped[str | None] = mapped_column(Text)
    symbol_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parse_ms: Mapped[float | None] = mapped_column(Float)


class Symbol(Base):
    __tablename__ = "symbols"
    __table_args__ = (
        UniqueConstraint("analysis_run_id", "symbol_key"),
        enum_check("kind", SymbolKind),
        enum_check("visibility", Visibility),
        Index(None, "analysis_run_id", "qualified_name"),
        Index(None, "analysis_run_id", "name"),
        Index(None, "analysis_run_id", "file_id"),
        CheckConstraint(
            "start_line >= 1 AND (end_line, end_col) >= (start_line, start_col)",
            name="span_valid",
        ),
    )

    id: Mapped[int] = _bigint_pk()
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False
    )
    file_id: Mapped[int] = mapped_column(
        ForeignKey("source_files.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"))
    symbol_key: Mapped[str] = mapped_column(Text, nullable=False)
    qualified_name: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    visibility: Mapped[str] = mapped_column(Text, nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    start_col: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_col: Mapped[int] = mapped_column(Integer, nullable=False)
    signature: Mapped[str | None] = mapped_column(Text)
    body_fingerprint: Mapped[str | None] = mapped_column(Text)
    decorators: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    doc_summary: Mapped[str | None] = mapped_column(Text)
    is_test: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    has_parse_errors: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    # Cyclomatic complexity of functions and methods; NULL for other kinds and for runs
    # parsed before it was measured.
    complexity: Mapped[int | None] = mapped_column(Integer)


class SymbolReference(Base):
    """A syntactic reference and the outcome of trying to resolve it."""

    __tablename__ = "symbol_references"
    __table_args__ = (
        enum_check("ref_kind", ReferenceKind),
        enum_check("resolution_status", ResolutionStatus),
        Index(None, "analysis_run_id", "source_symbol_id"),
        Index(None, "analysis_run_id", "target_symbol_id"),
        Index(None, "analysis_run_id", "resolution_status"),
        CheckConstraint(
            "(resolution_status = 'RESOLVED') = (target_symbol_id IS NOT NULL)",
            name="resolved_has_target",
        ),
    )

    id: Mapped[int] = _bigint_pk()
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False
    )
    file_id: Mapped[int] = mapped_column(
        ForeignKey("source_files.id", ondelete="CASCADE"), nullable=False
    )
    source_symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False
    )
    ref_kind: Mapped[str] = mapped_column(Text, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    start_col: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_col: Mapped[int] = mapped_column(Integer, nullable=False)
    resolution_status: Mapped[str] = mapped_column(Text, nullable=False)
    target_symbol_id: Mapped[int | None] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE")
    )
    external_target: Mapped[str | None] = mapped_column(Text)
    candidates: Mapped[list[str] | None] = mapped_column(JSONB)
    resolver_rule: Mapped[str | None] = mapped_column(Text)
    # Import inside ``if TYPE_CHECKING:``; NULL for runs parsed before it was recorded.
    type_checking: Mapped[bool | None] = mapped_column()


class DependencyEdge(Base):
    __tablename__ = "dependency_edges"
    __table_args__ = (
        UniqueConstraint(
            "analysis_run_id", "src_symbol_id", "dst_symbol_id", "edge_type", "provenance"
        ),
        enum_check("edge_type", EdgeType),
        enum_check("provenance", Provenance),
        enum_check("confidence", Confidence),
        Index(None, "analysis_run_id", "dst_symbol_id", "edge_type"),
        Index(None, "analysis_run_id", "edge_type"),
    )

    id: Mapped[int] = _bigint_pk()
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False
    )
    src_symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False
    )
    dst_symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False
    )
    edge_type: Mapped[str] = mapped_column(Text, nullable=False)
    provenance: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[str] = mapped_column(Text, nullable=False)
    reference_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_reference_id: Mapped[int | None] = mapped_column(
        ForeignKey("symbol_references.id", ondelete="SET NULL")
    )
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class AnalysisWarning(Base):
    __tablename__ = "analysis_warnings"
    __table_args__ = (
        enum_check("severity", Severity),
        Index(None, "analysis_run_id", "code"),
    )

    id: Mapped[int] = _bigint_pk()
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False
    )
    file_id: Mapped[int | None] = mapped_column(ForeignKey("source_files.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    start_line: Mapped[int | None] = mapped_column(Integer)
    end_line: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Job(Base):
    """A unit of background work, coordinated through PostgreSQL row locks and leases."""

    __tablename__ = "jobs"
    __table_args__ = (
        enum_check("kind", JobKind),
        enum_check("status", JobStatus),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="attempts_valid"),
        CheckConstraint(
            "status NOT IN ('CLAIMED', 'RUNNING') OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="active_has_lease",
        ),
        Index(
            "ix_jobs_claimable",
            "priority",
            "run_after",
            postgresql_where=text("status = 'QUEUED'"),
        ),
        Index(
            "ix_jobs_active_lease",
            "lease_expires_at",
            postgresql_where=text("status IN ('CLAIMED', 'RUNNING')"),
        ),
        Index(
            "uq_jobs_active_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text(JOB_NOT_TERMINAL_SQL),
        ),
        Index(None, "repository_id", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default=JobStatus.QUEUED)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    repository_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE")
    )
    subject_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_category: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ChangeSet(Base):
    """A comparison between two revisions of a repository (the subject of impact analysis)."""

    __tablename__ = "change_sets"
    __table_args__ = (
        enum_check("status", ChangeSetStatus),
        CheckConstraint(f"base_sha IS NULL OR base_sha {SHA_CHECK}", name="base_sha_format"),
        CheckConstraint(f"target_sha IS NULL OR target_sha {SHA_CHECK}", name="target_sha_format"),
        Index(None, "repository_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    base_ref: Mapped[str] = mapped_column(Text, nullable=False)
    target_ref: Mapped[str] = mapped_column(Text, nullable=False)
    base_sha: Mapped[str | None] = mapped_column(Text)
    target_sha: Mapped[str | None] = mapped_column(Text)
    base_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="SET NULL")
    )
    target_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default=ChangeSetStatus.QUEUED)
    diff_algorithm: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    timings_ms: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChangedFile(Base):
    __tablename__ = "changed_files"
    __table_args__ = (
        enum_check("mapping_status", ChangeMappingStatus),
        CheckConstraint(
            "change_type IN ('ADDED', 'MODIFIED', 'DELETED', 'RENAMED', 'COPIED', 'TYPE_CHANGED')",
            name="change_type_valid",
        ),
        CheckConstraint("old_path IS NOT NULL OR new_path IS NOT NULL", name="has_path"),
        Index(None, "change_set_id"),
    )

    id: Mapped[int] = _bigint_pk()
    change_set_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("change_sets.id", ondelete="CASCADE"), nullable=False
    )
    change_type: Mapped[str] = mapped_column(Text, nullable=False)
    old_path: Mapped[str | None] = mapped_column(Text)
    new_path: Mapped[str | None] = mapped_column(Text)
    old_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_files.id", ondelete="SET NULL")
    )
    new_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_files.id", ondelete="SET NULL")
    )
    language: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str | None] = mapped_column(Text)
    mapping_status: Mapped[str] = mapped_column(Text, nullable=False)
    similarity: Mapped[int | None] = mapped_column(Integer)
    added_lines: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deleted_lines: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    old_ranges: Mapped[list[list[int]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    new_ranges: Mapped[list[list[int]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )


class ChangedSymbol(Base):
    __tablename__ = "changed_symbols"
    __table_args__ = (
        CheckConstraint(
            "change_kind IN ('ADDED', 'REMOVED', 'MODIFIED', 'RENAMED')", name="change_kind_valid"
        ),
        CheckConstraint(
            "base_symbol_id IS NOT NULL OR target_symbol_id IS NOT NULL", name="has_symbol"
        ),
        Index(None, "change_set_id"),
    )

    id: Mapped[int] = _bigint_pk()
    change_set_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("change_sets.id", ondelete="CASCADE"), nullable=False
    )
    changed_file_id: Mapped[int] = mapped_column(
        ForeignKey("changed_files.id", ondelete="CASCADE"), nullable=False
    )
    change_kind: Mapped[str] = mapped_column(Text, nullable=False)
    symbol_kind: Mapped[str] = mapped_column(Text, nullable=False)
    qualified_name: Mapped[str] = mapped_column(Text, nullable=False)
    base_symbol_id: Mapped[int | None] = mapped_column(
        ForeignKey("symbols.id", ondelete="SET NULL")
    )
    target_symbol_id: Mapped[int | None] = mapped_column(
        ForeignKey("symbols.id", ondelete="SET NULL")
    )
    signature_changed: Mapped[bool] = mapped_column(nullable=False, default=False)
    body_changed: Mapped[bool] = mapped_column(nullable=False, default=False)
    decorators_changed: Mapped[bool] = mapped_column(nullable=False, default=False)
    bases_changed: Mapped[bool] = mapped_column(nullable=False, default=False)
    changed_lines: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ImpactReport(Base):
    """Evidence-backed impact of a change set (one per change set)."""

    __tablename__ = "impact_reports"
    __table_args__ = (
        CheckConstraint(
            "completeness IN ('COMPLETE', 'PARTIAL', 'UNKNOWN')", name="completeness_valid"
        ),
        CheckConstraint("risk_level IN ('LOW', 'MEDIUM', 'HIGH')", name="risk_level_valid"),
        CheckConstraint(
            "recommended_scope IN ('NONE', 'TARGETED', 'BROAD', 'FULL_SUITE')",
            name="recommended_scope_valid",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    change_set_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("change_sets.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    engine_version: Mapped[str] = mapped_column(Text, nullable=False)
    completeness: Mapped[str] = mapped_column(Text, nullable=False)
    completeness_reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    risk_level: Mapped[str] = mapped_column(Text, nullable=False)
    risk_factors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    recommended_scope: Mapped[str] = mapped_column(Text, nullable=False)
    scope_reasons: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    uncertainty: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class ImpactItem(Base):
    """One changed or impacted symbol, with the path that explains its inclusion."""

    __tablename__ = "impact_items"
    __table_args__ = (
        CheckConstraint(
            "strength IN ('CHANGED', 'DIRECT', 'TRANSITIVE', 'IMPORT_ONLY', 'HEURISTIC')",
            name="strength_valid",
        ),
        CheckConstraint(
            "target_symbol_id IS NOT NULL OR base_symbol_id IS NOT NULL", name="has_symbol"
        ),
        Index(None, "report_id", "depth"),
    )

    id: Mapped[int] = _bigint_pk()
    report_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("impact_reports.id", ondelete="CASCADE"), nullable=False
    )
    target_symbol_id: Mapped[int | None] = mapped_column(
        ForeignKey("symbols.id", ondelete="SET NULL")
    )
    base_symbol_id: Mapped[int | None] = mapped_column(
        ForeignKey("symbols.id", ondelete="SET NULL")
    )
    qualified_name: Mapped[str] = mapped_column(Text, nullable=False)
    symbol_kind: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str | None] = mapped_column(Text)
    is_test: Mapped[bool] = mapped_column(nullable=False, default=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False)
    strength: Mapped[str] = mapped_column(Text, nullable=False)
    seed_qualified_name: Mapped[str] = mapped_column(Text, nullable=False)
    seed_reason: Mapped[str] = mapped_column(Text, nullable=False)
    graph: Mapped[str] = mapped_column(Text, nullable=False)  # TARGET or BASE
    explanation: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)


class TestCaseRecord(Base):
    """A test in an analysed snapshot. Static discovery first; sandboxed collection updates it."""

    __test__ = False
    __tablename__ = "test_cases"
    __table_args__ = (
        UniqueConstraint("analysis_run_id", "node_id"),
        CheckConstraint(
            "collection_status IN ('STATIC', 'COLLECTED', 'NOT_COLLECTED', 'COLLECTION_ERROR')",
            name="collection_status_valid",
        ),
    )

    id: Mapped[int] = _bigint_pk()
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False
    )
    symbol_id: Mapped[int | None] = mapped_column(ForeignKey("symbols.id", ondelete="SET NULL"))
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    qualified_name: Mapped[str] = mapped_column(Text, nullable=False)
    collection_status: Mapped[str] = mapped_column(Text, nullable=False, default="STATIC")
    created_at: Mapped[datetime] = _created_at()


class TestPlan(Base):
    __test__ = False
    __tablename__ = "test_plans"
    __table_args__ = (
        CheckConstraint("policy IN ('CONSERVATIVE', 'BUDGETED')", name="policy_valid"),
        CheckConstraint("scope IN ('NONE', 'TARGETED', 'BROAD', 'FULL_SUITE')", name="scope_valid"),
        Index(None, "change_set_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    change_set_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("change_sets.id", ondelete="CASCADE"), nullable=False
    )
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False
    )
    policy: Mapped[str] = mapped_column(Text, nullable=False)
    budget: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    fallback_reason: Mapped[str | None] = mapped_column(Text)
    total_tests: Mapped[int] = mapped_column(Integer, nullable=False)
    selected_tests: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_seconds: Mapped[float | None] = mapped_column(Float)
    budget_limited: Mapped[bool] = mapped_column(nullable=False, default=False)
    evidence_sources: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class TestPlanItem(Base):
    __test__ = False
    __tablename__ = "test_plan_items"
    __table_args__ = (
        UniqueConstraint("plan_id", "node_id"),
        Index(None, "plan_id", "selected", "rank"),
    )

    id: Mapped[int] = _bigint_pk()
    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("test_plans.id", ondelete="CASCADE"), nullable=False
    )
    test_case_id: Mapped[int | None] = mapped_column(
        ForeignKey("test_cases.id", ondelete="SET NULL")
    )
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    selected: Mapped[bool] = mapped_column(nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    exclusion_reason: Mapped[str | None] = mapped_column(Text)
    estimated_seconds: Mapped[float | None] = mapped_column(Float)


RUN_OUTCOMES_SQL = (
    "'PASSED', 'FAILED', 'TIMED_OUT', 'CANCELLED', 'INFRASTRUCTURE_ERROR', "
    "'COLLECTION_ERROR', 'SECURITY_POLICY_BLOCKED', 'RESOURCE_LIMIT_EXCEEDED'"
)


class TestRun(Base):
    """One sandboxed pytest execution against an exact snapshot."""

    __test__ = False
    __tablename__ = "test_runs"
    __table_args__ = (
        CheckConstraint("mode IN ('SELECTED', 'FULL_SUITE', 'COLLECT')", name="mode_valid"),
        CheckConstraint("status IN ('QUEUED', 'RUNNING', 'COMPLETED')", name="status_valid"),
        CheckConstraint(
            f"outcome IS NULL OR outcome IN ({RUN_OUTCOMES_SQL})", name="outcome_valid"
        ),
        CheckConstraint(
            "(status = 'COMPLETED') = (outcome IS NOT NULL)", name="completed_has_outcome"
        ),
        CheckConstraint(f"commit_sha {SHA_CHECK}", name="commit_sha_format"),
        Index(None, "repository_id", "created_at"),
        Index(None, "plan_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repository_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    commit_sha: Mapped[str] = mapped_column(Text, nullable=False)
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_plans.id", ondelete="SET NULL")
    )
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    requested_node_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    coverage: Mapped[bool] = mapped_column(nullable=False, default=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="QUEUED")
    outcome: Mapped[str | None] = mapped_column(Text)
    outcome_reason: Mapped[str | None] = mapped_column(Text)
    pytest_exit_code: Mapped[int | None] = mapped_column(Integer)
    container_exit_code: Mapped[int | None] = mapped_column(Integer)
    counts: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    image: Mapped[str] = mapped_column(Text, nullable=False)
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    stdout_tail: Mapped[str | None] = mapped_column(Text)
    stderr_tail: Mapped[str | None] = mapped_column(Text)
    output_truncated: Mapped[bool] = mapped_column(nullable=False, default=False)
    cleanup_ok: Mapped[bool | None] = mapped_column()
    collected_node_ids: Mapped[list[str] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TestResultRecord(Base):
    __test__ = False
    __tablename__ = "test_results"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('PASSED', 'FAILED', 'ERROR', 'SKIPPED')", name="outcome_valid"
        ),
        Index(None, "test_run_id"),
        Index(None, "node_id"),
    )

    id: Mapped[int] = _bigint_pk()
    test_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("test_runs.id", ondelete="CASCADE"), nullable=False
    )
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False)
    message: Mapped[str | None] = mapped_column(Text)


class CoverageRecord(Base):
    """Lines executed by one test (``test_node_id``) or outside any test (NULL)."""

    __tablename__ = "coverage_records"
    __table_args__ = (Index(None, "test_run_id", "path"),)

    id: Mapped[int] = _bigint_pk()
    test_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("test_runs.id", ondelete="CASCADE"), nullable=False
    )
    test_node_id: Mapped[str | None] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    lines: Mapped[list[int]] = mapped_column(ARRAY(Integer), nullable=False)


class ParseCacheEntry(Base):
    """Parser output for exact inputs (blob, path, module name, parser version).

    The key covers everything the parser reads, so a hit is equivalent to re-parsing.
    Resolution is never cached: it depends on the whole snapshot.
    """

    __tablename__ = "parse_cache"

    blob_sha: Mapped[str] = mapped_column(Text, primary_key=True)
    path: Mapped[str] = mapped_column(Text, primary_key=True)
    module_name: Mapped[str] = mapped_column(Text, primary_key=True)
    parser_version: Mapped[str] = mapped_column(Text, primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created_at()
