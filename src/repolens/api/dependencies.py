"""FastAPI dependencies: settings, database sessions and access control."""

from __future__ import annotations

import hmac
import json
import uuid
from collections.abc import Iterable, Iterator
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from repolens.api.errors import ApiError
from repolens.config import Settings
from repolens.services import ownership

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


def current_user(
    request: Request,
    settings: Annotated[Settings, Depends(get_app_settings)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> uuid.UUID | None:
    """The signed-in user in user mode (401 without a valid token); None in operator mode."""
    if not settings.user_auth:
        return None
    if credentials is None:
        raise ApiError(401, "unauthorized", "Missing or invalid access token")
    user_id: uuid.UUID = request.app.state.token_verifier.user_id(credentials.credentials)
    return user_id


UserDep = Annotated[uuid.UUID | None, Depends(current_user)]


def _ids(pairs: Iterable[tuple[str, Any]]) -> list[tuple[str, uuid.UUID]]:
    found = []
    for name, value in pairs:
        if name not in ownership.OWNED_PARAMETERS:
            continue
        try:
            found.append((name, uuid.UUID(str(value))))
        except ValueError:
            continue  # not an id at all: request validation answers 422
    return found


async def enforce_ownership(request: Request, user: UserDep) -> None:
    """Every repository, analysis, impact, plan, test run or job id a request names, in its
    path, query or JSON body, must belong to the signed-in user (404 otherwise)."""
    if user is None:
        return
    pairs: list[tuple[str, Any]] = [
        *request.path_params.items(),
        *request.query_params.multi_items(),
    ]
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = None  # request validation answers 422
        if isinstance(body, dict):
            pairs.extend(body.items())
    ids = _ids(pairs)
    if not ids:
        return
    factory: sessionmaker[Session] = request.app.state.session_factory

    def check() -> None:
        with factory() as session:
            ownership.require_owned(session, user, ids)

    await run_in_threadpool(check)


def require_token(
    settings: Annotated[Settings, Depends(get_app_settings)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    user: UserDep,
) -> None:
    """Guard mutating endpoints. With no token configured they are disabled entirely.

    In user mode the signed-in user's token (checked by ``current_user``) is what counts.
    """
    if user is not None:
        return
    if settings.api_token is None:
        raise ApiError(403, "mutations_disabled", "REPOLENS_API_TOKEN is not configured")
    expected = settings.api_token.get_secret_value().encode()
    if credentials is None or not hmac.compare_digest(credentials.credentials.encode(), expected):
        raise ApiError(401, "unauthorized", "Missing or invalid bearer token")


SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
Authorized = Depends(require_token)
Owned = Depends(enforce_ownership)
