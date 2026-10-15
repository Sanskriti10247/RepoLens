import pytest
from alembic import command
from sqlalchemy import Engine, inspect

from tests.conftest import alembic_config

pytestmark = pytest.mark.integration


def test_migrations_round_trip(engine: Engine, database_url: str) -> None:
    cfg = alembic_config(database_url)
    command.downgrade(cfg, "base")
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    command.upgrade(cfg, "head")


def test_models_match_migrations(engine: Engine, database_url: str) -> None:
    # Fails if an ORM model changed without a corresponding migration.
    command.check(alembic_config(database_url))
