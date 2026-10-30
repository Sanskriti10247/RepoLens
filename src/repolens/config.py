"""Runtime configuration loaded from environment variables (prefix ``REPOLENS_``)."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class LogFormat(StrEnum):
    JSON = "json"
    CONSOLE = "console"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REPOLENS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Environment = Environment.DEVELOPMENT
    database_url: str = "postgresql+psycopg://repolens:repolens@127.0.0.1:5433/repolens"
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8100, ge=1, le=65535)
    # Bearer token required for mutating endpoints. None disables them entirely.
    api_token: SecretStr | None = None
    # User sign-in (Supabase Auth). When set, every route except health/ready needs a user's
    # access token and each user sees only the repositories they added. Unset: single
    # operator with api_token, as before.
    supabase_url: str | None = None
    # Only for projects still signing tokens with the legacy shared HS256 secret.
    supabase_jwt_secret: SecretStr | None = None
    # Showcase mode: every mutating endpoint answers 403 and nothing needs a worker or Docker.
    read_only: bool = False
    cors_origins: list[str] = ["http://127.0.0.1:3100", "http://localhost:3100"]

    data_dir: Path = Path("var")
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON

    # Ingestion limits
    max_repository_size_mb: int = Field(default=500, ge=1)
    max_file_count: int = Field(default=50_000, ge=1)
    max_indexed_file_bytes: int = Field(default=1_000_000, ge=1)
    max_hashed_file_bytes: int = Field(default=20_000_000, ge=1)
    git_timeout_seconds: float = Field(default=300.0, gt=0)
    # Reuse parser output for unchanged files (keyed by blob, path, module, parser version).
    parse_cache_enabled: bool = True
    min_free_disk_mb: int = Field(default=1024, ge=0)
    # file:// repositories are for tests and offline demos only; off unless enabled.
    allow_local_repositories: bool = False
    local_repository_roots: list[Path] = []

    # Worker
    job_lease_seconds: int = Field(default=60, ge=5)
    max_jobs_per_repository: int = Field(default=2, ge=1)
    max_concurrent_sandboxes: int = Field(default=2, ge=1)

    # Sandboxed test execution (Docker). Only the configured image may be used. Off on hosts
    # without a Docker daemon: test runs and collection are refused, test plans still work.
    sandbox_enabled: bool = True
    sandbox_image: str = "repolens-sandbox:py3.12-1"
    docker_binary: str = "docker"
    sandbox_memory_mb: int = Field(default=1024, ge=64)
    sandbox_cpus: float = Field(default=2.0, gt=0)
    sandbox_pids: int = Field(default=256, ge=16)
    sandbox_timeout_seconds: float = Field(default=600.0, gt=0)
    sandbox_workspace_mb: int = Field(default=1024, ge=16)

    # Optional model provider for repository questions. Off by default; core analysis never
    # depends on it. Source snippets are sent only if llm_allow_source_upload is true.
    llm_provider: Literal["none", "anthropic"] = "none"
    llm_model: str = "claude-opus-5-5"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_allow_source_upload: bool = False
    llm_max_evidence_chars: int = Field(default=40_000, ge=1000)
    llm_min_interval_seconds: float = Field(default=1.0, ge=0)

    @property
    def user_auth(self) -> bool:
        return bool(self.supabase_url)

    @property
    def mirrors_dir(self) -> Path:
        return self.data_dir / "mirrors"

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def git_home_dir(self) -> Path:
        return self.data_dir / "git-home"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
