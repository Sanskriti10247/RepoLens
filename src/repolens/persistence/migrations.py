"""Programmatic access to the Alembic configuration shipped with the repository."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config

_ROOT = Path(__file__).resolve().parents[3]


def alembic_config(database_url: str | None = None) -> Config:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.attributes["configure_logger"] = False
    if database_url is not None:
        cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg
