"""Public API schemas (response and request bodies)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

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
