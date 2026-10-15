"""Engine and session management."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker


def make_engine(database_url: str, *, pool_size: int = 5) -> Engine:
    return create_engine(
        database_url,
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5},
    )


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@contextmanager
def transaction(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Yield a session inside a transaction: commit on success, roll back on any error."""
    session = factory()
    try:
        with session.begin():
            yield session
    finally:
        session.close()


def ping(engine: Engine) -> None:
    """Raise if the database is unreachable."""
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
