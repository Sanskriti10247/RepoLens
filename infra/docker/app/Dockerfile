# syntax=docker/dockerfile:1
# RepoLens API and worker images, two targets of one Dockerfile:
#   docker build -f infra/docker/app/Dockerfile --target api    -t repolens-api .
#   docker build -f infra/docker/app/Dockerfile --target worker -t repolens-worker .
# Base images are pinned by digest; python:3.12-slim matches the sandbox image.

ARG PYTHON_IMAGE=python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.9.26@sha256:9a23023be68b2ed09750ae636228e903a54a05ea56ed03a934d00fe9fbeded4b
ARG DOCKER_CLI_IMAGE=docker:28.5.2-cli@sha256:625d9431a9f54c5a2bc90f24f0e1c3d55b1349fd857dd85035f98c2c9acbdd4d

FROM ${UV_IMAGE} AS uv
FROM ${DOCKER_CLI_IMAGE} AS docker-cli

# Build: install the locked dependencies into /app/.venv.
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Dependencies first, so source edits do not invalidate this layer.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
# Installed editable on purpose: persistence/migrations.py finds alembic.ini and migrations/
# relative to the source tree, so /app keeps the repository layout.
RUN uv sync --locked --no-dev

# Runtime shared by both targets. Both need git: the worker fetches mirrors and the API reads
# source blobs from them. Debian package versions follow the pinned base image.
FROM ${PYTHON_IMAGE} AS runtime
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --system --gid 10010 repolens \
    && useradd --system --uid 10010 --gid 10010 --home-dir /app --shell /usr/sbin/nologin repolens
WORKDIR /app
COPY --from=build --chown=root:root /app/.venv /app/.venv
COPY --chown=root:root src ./src
COPY --chown=root:root alembic.ini ./alembic.ini
COPY --chown=root:root migrations ./migrations
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    REPOLENS_ENVIRONMENT=production \
    REPOLENS_LOG_FORMAT=json \
    REPOLENS_DATA_DIR=/var/lib/repolens

# API, also used for the one-shot migration job.
FROM runtime AS api
ENV REPOLENS_API_HOST=0.0.0.0 \
    REPOLENS_API_PORT=8100
USER 10010:10010
EXPOSE 8100
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
    CMD ["python", "-c", "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8100/api/health', timeout=2); sys.exit(0)"]
CMD ["repolens-api"]

# Worker: adds the docker CLI to launch sandboxes on the host daemon.
FROM runtime AS worker
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
USER 10010:10010
# No HTTP port, so healthy means the Docker daemon is reachable. The API's /api/ready covers
# the database.
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD ["docker", "version", "--format", "{{.Server.Version}}"]
CMD ["repolens-worker"]
