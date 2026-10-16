"""Public API schemas (response and request bodies)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from repolens.domain.enums import AnalysisStatus, JobStatus


class Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class PageMeta(Schema):
    total: int
    limit: int
    offset: int


class RepositoryCreate(BaseModel):
    url: str = Field(max_length=300, examples=["https://github.com/pallets/itsdangerous"])


class RepositoryOut(Schema):
    id: uuid.UUID
    provider: str
    host: str
    owner: str
    name: str
    canonical_url: str
    default_branch: str | None
    created_at: datetime
    latest_analysis: AnalysisSummary | None = None


class RepositoryPage(Schema):
    items: list[RepositoryOut]
    page: PageMeta


class AnalysisCreate(BaseModel):
    ref: str = Field(default="HEAD", max_length=255, examples=["HEAD", "main", "v1.2.0"])


class SnapshotOut(Schema):
    id: uuid.UUID
    commit_sha: str
    tree_sha: str | None
    committed_at: datetime | None
    file_count: int | None
    total_bytes: int | None
    language_inventory: dict[str, int] | None
    inventory_sha256: str | None
    issue_counts: dict[str, int] = {}
    issues_truncated: bool = False


class AnalysisSummary(Schema):
    id: uuid.UUID
    status: AnalysisStatus
    requested_ref: str
    commit_sha: str | None = None
    created_at: datetime
    finished_at: datetime | None


class AnalysisOut(Schema):
    id: uuid.UUID
    repository_id: uuid.UUID
    status: AnalysisStatus
    requested_ref: str
    resolved_ref: str | None
    resolved_ref_kind: str | None
    reused_run_id: uuid.UUID | None
    results_run_id: uuid.UUID = Field(
        description="Run that owns the results (differs from id when status is REUSED)"
    )
    parser_version: str
    engine_version: str
    config_fingerprint: str
    counts: dict[str, Any] | None
    timings_ms: dict[str, Any] | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    snapshot: SnapshotOut | None
    job: JobOut | None


class AnalysisPage(Schema):
    items: list[AnalysisSummary]
    page: PageMeta


class JobOut(Schema):
    id: uuid.UUID
    status: JobStatus
    attempts: int
    max_attempts: int
    error_category: str | None
    error_detail: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    cancel_requested_at: datetime | None


class SnapshotIssue(Schema):
    code: str
    severity: str
    message: str
    path: str | None


RepositoryOut.model_rebuild()
AnalysisOut.model_rebuild()


# -- code graph --------------------------------------------------------------------------


class SymbolSummary(Schema):
    id: int
    qualified_name: str
    name: str
    kind: str
    visibility: str
    path: str
    file_id: int
    start_line: int
    start_col: int
    end_line: int
    end_col: int
    signature: str | None
    doc_summary: str | None
    is_test: bool
    has_parse_errors: bool


class SymbolPage(Schema):
    analysis_id: uuid.UUID
    results_run_id: uuid.UUID
    commit_sha: str
    items: list[SymbolSummary]
    page: PageMeta


class NeighbourOut(Schema):
    edge_type: str
    confidence: str
    provenance: str
    reference_count: int
    rules: list[str]
    symbol: SymbolSummary


class ReferenceOut(Schema):
    id: int
    ref_kind: str
    raw_text: str
    start_line: int
    end_line: int
    resolution_status: str
    resolver_rule: str | None
    external_target: str | None
    candidates: list[str] | None
    target_symbol_id: int | None
    target_qualified_name: str | None


class SymbolDetailOut(Schema):
    analysis_id: uuid.UUID
    commit_sha: str
    symbol: SymbolSummary
    body_fingerprint: str | None
    decorators: list[str]
    parent: SymbolSummary | None
    children: list[SymbolSummary]
    callers: list[NeighbourOut]
    callers_total: int
    callees: list[NeighbourOut]
    callees_total: int
    dependents: list[NeighbourOut]
    dependencies: list[NeighbourOut]
    references: list[ReferenceOut]
    reference_counts: dict[str, int]


class GraphNodeOut(Schema):
    depth: int
    symbol: SymbolSummary


class GraphEdgeOut(Schema):
    src: int
    dst: int
    edge_type: str
    confidence: str


class GraphOut(Schema):
    analysis_id: uuid.UUID
    commit_sha: str
    root: int
    nodes: list[GraphNodeOut]
    edges: list[GraphEdgeOut]
    truncated: bool
    truncation_reason: str | None


class PathOut(Schema):
    found: bool
    edges: list[GraphEdgeOut]
    symbols: list[SymbolSummary]


class ModuleNodeOut(Schema):
    id: int
    name: str
    path: str


class ModuleEdgeOut(Schema):
    src_module_id: int
    dst_module_id: int
    edge_type: str
    edges: int
    references: int


class ModuleGraphOut(Schema):
    analysis_id: uuid.UUID
    modules: list[ModuleNodeOut]
    edges: list[ModuleEdgeOut]
    import_cycles: list[list[int]]


class DegreeOut(Schema):
    symbol: SymbolSummary
    count: int


class StatsOut(Schema):
    analysis_id: uuid.UUID
    symbols_by_kind: dict[str, int]
    edges_by_type: dict[str, int]
    references_by_status: dict[str, int]
    top_fan_in: list[DegreeOut]
    top_fan_out: list[DegreeOut]


class FileOut(Schema):
    id: int
    path: str
    language: str | None
    role: str
    size_bytes: int
    status: str | None
    module_name: str | None
    symbol_count: int | None
    error_count: int | None


class FilePage(Schema):
    items: list[FileOut]
    page: PageMeta
    status_counts: dict[str, int]


class WarningOut(Schema):
    id: int
    code: str
    severity: str
    message: str
    path: str | None
    start_line: int | None
    end_line: int | None


class WarningPage(Schema):
    items: list[WarningOut]
    page: PageMeta


class SourceOut(Schema):
    file_id: int
    path: str
    language: str | None
    commit_sha: str
    blob_sha: str
    content: str


# -- change and impact analysis -------------------------------------------------------------


class ImpactCreate(BaseModel):
    base: str = Field(max_length=255, examples=["main", "v1.0.0", "3f2a9c1"])
    target: str = Field(max_length=255, examples=["feature/login", "HEAD"])


class ChangeSetOut(Schema):
    id: uuid.UUID
    repository_id: uuid.UUID
    status: str
    base_ref: str
    target_ref: str
    base_sha: str | None
    target_sha: str | None
    base_run_id: uuid.UUID | None
    target_run_id: uuid.UUID | None
    diff_algorithm: str | None
    summary: dict[str, Any] | None
    timings_ms: dict[str, Any] | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    job: JobOut | None = None


class ChangeSetPage(Schema):
    items: list[ChangeSetOut]
    page: PageMeta


class ChangedFileOut(Schema):
    id: int
    change_type: str
    old_path: str | None
    new_path: str | None
    old_file_id: int | None
    new_file_id: int | None
    language: str | None
    role: str | None
    mapping_status: str
    similarity: int | None
    added_lines: int
    deleted_lines: int
    old_ranges: list[list[int]]
    new_ranges: list[list[int]]


class ChangedFilePage(Schema):
    items: list[ChangedFileOut]
    page: PageMeta


class ChangedSymbolOut(Schema):
    id: int
    changed_file_id: int
    change_kind: str
    symbol_kind: str
    qualified_name: str
    base_symbol_id: int | None
    target_symbol_id: int | None
    signature_changed: bool
    body_changed: bool
    decorators_changed: bool
    bases_changed: bool
    changed_lines: int


class ChangedSymbolPage(Schema):
    items: list[ChangedSymbolOut]
    page: PageMeta


class ImpactReportOut(Schema):
    id: uuid.UUID
    change_set_id: uuid.UUID
    engine_version: str
    completeness: str
    completeness_reasons: list[dict[str, Any]]
    risk_level: str
    risk_factors: list[dict[str, Any]]
    recommended_scope: str
    scope_reasons: list[str]
    counts: dict[str, Any]
    limits: dict[str, Any]
    uncertainty: dict[str, Any]
    created_at: datetime
    disclaimer: str = (
        "Risk is an explainable heuristic score over the listed factors, not a probability "
        "of failure. Heuristic items may be false positives; unresolved relationships may hide "
        "real dependents."
    )


class ImpactItemOut(Schema):
    id: int
    target_symbol_id: int | None
    base_symbol_id: int | None
    qualified_name: str
    symbol_kind: str
    path: str | None
    is_test: bool
    depth: int
    strength: str
    seed_qualified_name: str
    seed_reason: str
    graph: str
    explanation: list[dict[str, Any]]


class ImpactItemPage(Schema):
    items: list[ImpactItemOut]
    page: PageMeta


# -- test plans ------------------------------------------------------------------------------


class TestPlanCreate(BaseModel):
    __test__ = False

    policy: Literal["CONSERVATIVE", "BUDGETED"] = "CONSERVATIVE"
    max_tests: int | None = Field(default=None, ge=1, le=100_000)
    max_seconds: float | None = Field(default=None, gt=0, le=86_400)


class TestPlanOut(Schema):
    __test__ = False

    id: uuid.UUID
    change_set_id: uuid.UUID
    analysis_run_id: uuid.UUID
    policy: str
    budget: dict[str, Any] | None
    scope: str
    fallback_reason: str | None
    total_tests: int
    selected_tests: int
    estimated_seconds: float | None
    budget_limited: bool
    evidence_sources: dict[str, Any]
    created_at: datetime


class TestPlanItemOut(Schema):
    __test__ = False

    id: int
    node_id: str
    selected: bool
    rank: int
    score: int
    evidence: list[dict[str, Any]]
    exclusion_reason: str | None
    estimated_seconds: float | None


class TestPlanItemPage(Schema):
    __test__ = False

    items: list[TestPlanItemOut]
    page: PageMeta


class TestCaseOut(Schema):
    __test__ = False

    id: int
    node_id: str
    path: str
    qualified_name: str
    symbol_id: int | None
    collection_status: str


class TestCasePage(Schema):
    __test__ = False

    items: list[TestCaseOut]
    page: PageMeta


# -- test runs -------------------------------------------------------------------------------


class TestRunCreate(BaseModel):
    __test__ = False

    plan_id: uuid.UUID | None = None
    analysis_id: uuid.UUID | None = None
    full_suite: bool = False
    coverage: bool = True


class TestRunOut(Schema):
    __test__ = False

    id: uuid.UUID
    repository_id: uuid.UUID
    analysis_run_id: uuid.UUID
    snapshot_id: uuid.UUID
    commit_sha: str
    plan_id: uuid.UUID | None
    mode: str
    requested_node_ids: list[str]
    coverage: bool
    status: str
    outcome: str | None
    outcome_reason: str | None
    pytest_exit_code: int | None
    container_exit_code: int | None
    counts: dict[str, Any] | None
    duration_ms: float | None
    image: str
    limits: dict[str, Any]
    output_truncated: bool
    cleanup_ok: bool | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    job: JobOut | None = None


class TestRunPage(Schema):
    __test__ = False

    items: list[TestRunOut]
    page: PageMeta


class TestResultOut(Schema):
    __test__ = False

    id: int
    node_id: str
    outcome: str
    duration_ms: float
    message: str | None


class TestResultPage(Schema):
    __test__ = False

    items: list[TestResultOut]
    page: PageMeta


class TestRunLogs(Schema):
    __test__ = False

    stdout_tail: str | None
    stderr_tail: str | None
    output_truncated: bool
    note: str = "Logs are bounded tails of pytest output captured inside the sandbox."


class CoverageFileOut(Schema):
    path: str
    lines_covered: int
    tests: int


class CoverageSummary(Schema):
    test_run_id: uuid.UUID
    files: list[CoverageFileOut]
    note: str = (
        "Coverage records lines executed by the tests in this run. It is evidence of execution, "
        "not proof that behaviour was verified."
    )


# -- jobs and queue metrics ----------------------------------------------------------------


class JobDetailOut(JobOut):
    kind: str
    repository_id: uuid.UUID | None
    subject_id: uuid.UUID | None
    priority: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    claimed_at: datetime | None
    run_after: datetime


class JobPage(Schema):
    items: list[JobDetailOut]
    page: PageMeta


class KindMetrics(Schema):
    kind: str
    by_status: dict[str, int]
    completed_last_24h: int
    median_queue_wait_ms: float | None
    p95_queue_wait_ms: float | None
    median_run_ms: float | None
    p95_run_ms: float | None
    retried_jobs: int
    failure_categories: dict[str, int]


class QueueMetrics(Schema):
    kinds: list[KindMetrics]
    active_leases: int
    expired_leases: int
