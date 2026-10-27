# syntax=docker/dockerfile:1
# Demo: the API, the worker and a small PostgreSQL server in one container, for hosts with a
# single free web service (512 MiB, no disk, no Docker socket). See "Free hosting" in
# docs/deployment.md.
#   docker build -f infra/docker/demo/Dockerfile -t repolens-demo .
#   docker run --rm -e PORT=10000 -e REPOLENS_API_TOKEN=... -p 127.0.0.1:18300:10000 repolens-demo
# PostgreSQL starts from an empty directory on every boot and restores the snapshot in
# infra/demo/, so changes never outlive the container. There is no Docker daemon, so test
# runs and sandboxed collection are switched off; set REPOLENS_READ_ONLY=true for a
# showcase that refuses every change.

ARG PYTHON_IMAGE=python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.9.26@sha256:9a23023be68b2ed09750ae636228e903a54a05ea56ed03a934d00fe9fbeded4b

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
# The llm extra is imported only when REPOLENS_LLM_PROVIDER is set, so it costs image size
# but no memory otherwise.
RUN uv sync --locked --no-dev --extra llm --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --extra llm

FROM ${PYTHON_IMAGE} AS demo
# PostgreSQL comes from the Debian release the base image is built on. The server binaries
# are not on PATH, so the entrypoint calls them by full path.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git ca-certificates postgresql \
    && rm -rf /var/lib/apt/lists/* /usr/share/doc /usr/share/man /usr/share/info \
    && rm -rf /var/lib/postgresql /etc/postgresql
RUN groupadd --system --gid 10010 repolens \
    && useradd --system --uid 10010 --gid 10010 --home-dir /app --shell /usr/sbin/nologin repolens
WORKDIR /app
COPY --from=build --chown=root:root /app/.venv /app/.venv
COPY --chown=root:root src ./src
COPY --chown=root:root alembic.ini ./alembic.ini
COPY --chown=root:root migrations ./migrations
COPY --chown=root:root infra/demo/snapshot.tar.gz /opt/demo/snapshot.tar.gz
COPY --chown=root:root infra/docker/demo/entrypoint.sh /opt/demo/entrypoint.sh
RUN chmod 0755 /opt/demo/entrypoint.sh

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    REPOLENS_ENVIRONMENT=production \
    REPOLENS_LOG_FORMAT=json \
    REPOLENS_READ_ONLY=false \
    REPOLENS_SANDBOX_ENABLED=false \
    REPOLENS_API_HOST=0.0.0.0 \
    REPOLENS_DEMO_HOME=/tmp/repolens-demo \
    REPOLENS_MAX_REPOSITORY_SIZE_MB=50 \
    REPOLENS_MAX_FILE_COUNT=5000 \
    REPOLENS_MIN_FREE_DISK_MB=200 \
    REPOLENS_MAX_JOBS_PER_REPOSITORY=1 \
    REPOLENS_GIT_TIMEOUT_SECONDS=120
USER 10010:10010
EXPOSE 10000
HEALTHCHECK --interval=15s --timeout=3s --start-period=120s --retries=5 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/ready' % os.environ.get('PORT', '8000'), timeout=2)"]
ENTRYPOINT ["/opt/demo/entrypoint.sh"]
