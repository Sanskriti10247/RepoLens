"""FastAPI dependencies: settings, database sessions and access control."""

from __future__ import annotations

import hmac
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from repolens.api.errors import ApiError
from repolens.config import Settings

_bearer = HTTPBearer(auto_error=False)


def get_app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_engine(request: Request) -> Engine:
    engine: Engine = request.app.state.engine
    return engine


def get_session(request: Request) -> Iterator[Session]:
    factory: sessionmaker[Session] = request.app.state.session_factory
    session = factory()
    try:
        yield session
    finally:
        session.close()


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def reject_writes_when_read_only(
    request: Request, settings: Annotated[Settings, Depends(get_app_settings)]
) -> None:
    """Deny by default: in read-only mode every non-safe request is refused, token or not."""
    if settings.read_only and request.method.upper() not in _SAFE_METHODS:
        raise ApiError(403, "read_only", "This instance is read-only")


def require_token(
    settings: Annotated[Settings, Depends(get_app_settings)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    """Guard mutating endpoints. With no token configured they are disabled entirely."""
    if settings.api_token is None:
        raise ApiError(403, "mutations_disabled", "REPOLENS_API_TOKEN is not configured")
    expected = settings.api_token.get_secret_value().encode()
    if credentials is None or not hmac.compare_digest(credentials.credentials.encode(), expected):
        raise ApiError(401, "unauthorized", "Missing or invalid bearer token")


SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
Authorized = Depends(require_token)
