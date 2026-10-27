from __future__ import annotations

from typing import Annotated, Literal

from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import Engine

import repolens
from repolens.api.dependencies import SettingsDep, get_engine
from repolens.persistence.migrations import alembic_config

router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: Literal["ok"]
    version: str
    # True when the instance refuses every mutation (REPOLENS_READ_ONLY).
    read_only: bool


class ReadinessCheck(BaseModel):
    name: str
    ok: bool
    detail: str | None = None


class Readiness(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: list[ReadinessCheck]


@router.get("/health", response_model=Health)
def health(settings: SettingsDep) -> Health:
    """Liveness: the process is up. Does not touch dependencies."""
    return Health(status="ok", version=repolens.__version__, read_only=settings.read_only)


@router.get("/ready", response_model=Readiness, responses={503: {"model": Readiness}})
def ready(engine: Annotated[Engine, Depends(get_engine)]) -> JSONResponse:
    """Readiness: database reachable and schema at the latest migration."""
    checks: list[ReadinessCheck] = []
    try:
        with engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
        checks.append(ReadinessCheck(name="database", ok=True))
        head = ScriptDirectory.from_config(alembic_config()).get_current_head()
        checks.append(
            ReadinessCheck(
                name="migrations",
                ok=current == head,
                detail=None if current == head else f"at {current}, expected {head}",
            )
        )
    except Exception as exc:  # noqa: BLE001 - any failure means not ready
        checks.append(ReadinessCheck(name="database", ok=False, detail=type(exc).__name__))
    ok = all(c.ok for c in checks)
    body = Readiness(status="ready" if ok else "not_ready", checks=checks)
    return JSONResponse(body.model_dump(), status_code=200 if ok else 503)
