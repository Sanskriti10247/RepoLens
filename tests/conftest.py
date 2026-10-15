"""Shared fixtures.

Integration tests run against a real PostgreSQL database (``REPOLENS_TEST_DATABASE_URL``,
default: the ``repolens_test`` database from docker-compose). If the database is unreachable
the tests are skipped, unless ``REPOLENS_REQUIRE_INTEGRATION=1`` (set in CI), in which case
they fail.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from alembic import command
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from repolens.persistence.db import make_engine, make_session_factory, ping
from repolens.persistence.migrations import alembic_config

DEFAULT_TEST_DB = "postgresql+psycopg://repolens:repolens@127.0.0.1:5433/repolens_test"


@pytest.fixture(scope="session")
def database_url() -> str:
    return os.environ.get("REPOLENS_TEST_DATABASE_URL", DEFAULT_TEST_DB)


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    try:
        ping(eng)
    except Exception as exc:  # noqa: BLE001 - any connection failure means "no database"
        eng.dispose()
        if os.environ.get("REPOLENS_REQUIRE_INTEGRATION") == "1":
            pytest.fail(f"PostgreSQL required but unavailable: {exc}")
        pytest.skip(f"PostgreSQL unavailable: {type(exc).__name__}")
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    command.upgrade(alembic_config(database_url), "head")
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    yield make_session_factory(engine)
    with engine.begin() as conn:
        tables = conn.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename <> 'alembic_version'"
            )
        ).scalars()
        names = ", ".join(f'"{t}"' for t in tables)
        if names:
            conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
