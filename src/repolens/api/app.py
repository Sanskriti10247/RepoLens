"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import Engine

import repolens
from repolens.api.errors import install_error_handlers
from repolens.api.middleware import request_context
from repolens.api.routes import (
    analyses,
    graph,
    health,
    impact,
    repositories,
    test_plans,
    test_runs,
)
from repolens.config import Settings, get_settings
from repolens.observability.logging import configure_logging
from repolens.persistence.db import make_engine, make_session_factory
from repolens.repository.git import GitPolicy, GitRunner
from repolens.repository.mirror import MirrorStore
from repolens.services.analysis import build_providers, default_analyzer
from repolens.services.graph import SourceReader


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    settings = settings or get_settings()
    owns_engine = engine is None
    db_engine = engine or make_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if owns_engine:
            db_engine.dispose()

    app = FastAPI(
        title="RepoLens API",
        version=repolens.__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.engine = db_engine
    app.state.session_factory = make_session_factory(db_engine)
    app.state.providers = list(build_providers(settings).values())
    app.state.parser_version = default_analyzer(settings).version
    # Reads blobs from local mirrors only, so no transport protocol is allowed.
    reader_runner = GitRunner(settings.git_home_dir, GitPolicy(allowed_protocols=()))
    app.state.source_reader = SourceReader(MirrorStore(settings.mirrors_dir, reader_runner))

    app.middleware("http")(request_context)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )
    install_error_handlers(app)
    app.include_router(health.router, prefix="/api")
    app.include_router(repositories.router, prefix="/api")
    app.include_router(analyses.router, prefix="/api")
    app.include_router(graph.router, prefix="/api")
    app.include_router(impact.router, prefix="/api")
    app.include_router(test_plans.router, prefix="/api")
    app.include_router(test_runs.router, prefix="/api")
    return app


def build() -> FastAPI:
    """Entry point for ``uvicorn --factory repolens.api.app:build``."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    return create_app(settings)
