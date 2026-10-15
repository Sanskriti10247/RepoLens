"""Runtime configuration loaded from environment variables (prefix ``REPOLENS_``)."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

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
    min_free_disk_mb: int = Field(default=1024, ge=0)
    # file:// repositories are for tests and offline demos only; off unless enabled.
    allow_local_repositories: bool = False
    local_repository_roots: list[Path] = []

    # Worker
    job_lease_seconds: int = Field(default=60, ge=5)

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
