# Self-hosting on a single Linux server

This guide runs RepoLens on one Linux VPS with Docker: PostgreSQL, a one-shot migration job,
the API, a worker and the web app, from `docker-compose.prod.yml`. An optional Caddy service
terminates TLS in front of the web app.

[operations.md](operations.md) covers the operating model, failure handling and scaling.
RepoLens has not been run as a hosted multi-tenant service, so treat this guide as a starting
point, not a hardened runbook.

## What runs where

| Service | Image | Published | Notes |
|---|---|---|---|
| `postgres` | `postgres:16-alpine` (pinned digest) | no | data in the `pgdata` volume |
| `migrate` | `repolens-api` | no | runs `alembic upgrade head` once, then exits |
| `api` | `repolens-api` (`infra/docker/app/Dockerfile`, target `api`) | no | FastAPI on the internal network only |
| `worker` | `repolens-worker` (same Dockerfile, target `worker`) | no | Git, the Docker CLI, and the host Docker socket |
| `web` | `repolens-web` (`infra/docker/web/Dockerfile`) | `127.0.0.1:3100` | Next.js standalone server; adds the API token server-side |
| `caddy` (profile `tls`) | `caddy:2.10.2-alpine` (pinned digest) | `:80`, `:443` | TLS and basic auth in front of `web` |

The repository images run as non-root users with a read-only root filesystem, all Linux
capabilities dropped and `no-new-privileges`. `postgres`, `api` and `worker` share an
internal network with no route out. The worker also joins an egress network so it can fetch
from GitHub. The sandbox containers it launches run with `--network none`.

The API reads source blobs from the worker's Git mirrors (code explorer, question evidence),
so `api` and `worker` mount the same data directory and run as the same user.

## Prerequisites

- A Linux host (x86_64) with Docker Engine 24+ and Compose v2, using cgroup v2. Rootful
  Docker is assumed. The sandbox relies on `--memory`, `--pids-limit`, `--cpus` and
  `--read-only`.
- About 4 GB of RAM to start. Each sandbox may use up to `REPOLENS_SANDBOX_MEMORY_MB` (default
  1024 MB), and `REPOLENS_MAX_CONCURRENT_SANDBOXES` of them may run at once (default 2).
- Disk for PostgreSQL and for repository mirrors. Each repository is capped at
  `REPOLENS_MAX_REPOSITORY_SIZE_MB` (default 500).
- Outbound HTTPS to `github.com`.
- For TLS: a DNS name pointing at the host, and ports 80 and 443 open.
- A host or VM dedicated to RepoLens (see [The Docker socket](#the-docker-socket)).

## Where this will not work

The worker has to start Docker containers next to itself. That rules out:

- Serverless platforms (AWS Lambda, Cloud Run, Vercel or Netlify functions, and similar).
  They have no Docker daemon, no long-running worker process and no persistent local disk for
  mirrors.
- PaaS without Docker access (Heroku, Render, Railway, Fly.io Machines, App Platform and
  similar). The API and web app would run there, but sandboxed test runs and
  `pytest --collect-only` need a Docker daemon. Without one, every run ends in
  `INFRASTRUCTURE_ERROR`.
- Kubernetes as is. The compose layout does not translate directly. Workers
  would need a dedicated node pool with Docker, or a different sandbox backend, which RepoLens
  does not implement.
- Docker Desktop on macOS or Windows, and rootless Docker. These are untested; bind-mount
  path translation and resource limits behave differently there.

The web app on its own can be hosted anywhere Node.js runs, as long as it can reach the API
privately. Analysis, impact and test execution still need the worker on a Docker host.

## The Docker socket

The worker mounts `/var/run/docker.sock` so it can run `docker run` for each sandbox. Access
to the Docker socket is equivalent to root on the host: anyone who gains code execution in the
worker container can start a privileged container and take over the machine. The sandbox
restrictions protect the host from repository code, not from a compromised worker.

The setup limits this as far as it can:
- The worker runs as a non-root user (`REPOLENS_APP_UID`, default 10010). It has the docker
  group (`REPOLENS_DOCKER_GID`) as a supplementary group, a read-only root filesystem, no
  capabilities and `no-new-privileges`.
- Repository code never runs in the worker. Ingestion and parsing read Git objects without
  executing them. Tests run only in the sandbox image, with no network, as a non-root user,
  with no capabilities, a read-only root and a read-only source mount.
- The worker only runs the configured image (`REPOLENS_SANDBOX_IMAGE`), and it removes the
  containers it labelled `repolens.managed=1`.

The worker is still root-equivalent, so dedicate the host or VM to RepoLens and do not put
other workloads or secrets on it. Alternatives, roughly from least to most work:

1. A Docker socket proxy (for example `tecnativa/docker-socket-proxy`), exposing only the
   container endpoints the worker uses. Point the worker at it with `DOCKER_HOST`. This
   narrows the API surface, but `docker run` can still bind-mount host paths, so it
   reduces risk without removing it. RepoLens has not been tested this way.
2. Run the worker on the host instead of in a container (`uv run repolens-worker` under
   systemd, as a user in the `docker` group), and keep the other services in compose. The
   trust boundary is the same, but there is one fewer layer to reason about.
3. Rootless Docker for the daemon the worker uses. A container escape then lands in an
   unprivileged user account. This is untested with RepoLens; cgroup v2 delegation must be
   enabled for the resource limits to work.
4. A separate VM for workers. The workers reach PostgreSQL over a private network, and
   the API and web app run elsewhere. This isolates the root-equivalent part completely.
   The API reads source from the mirrors, so it needs the same data directory (for example
   over a shared filesystem) or has to run on the worker VM.

## First deploy

```bash
git clone https://github.com/Sanskriti10247/RepoLens.git && cd RepoLens

# 1. Configuration (.env.prod is gitignored).
cp .env.example .env.prod
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # -> REPOLENS_API_TOKEN
python3 -c "import secrets; print(secrets.token_hex(24))"       # -> POSTGRES_PASSWORD
stat -c %g /var/run/docker.sock                                 # -> REPOLENS_DOCKER_GID
$EDITOR .env.prod
chmod 600 .env.prod

# 2. Data directory: the same absolute path on the host and in the containers,
#    owned by the app user.
sudo install -d -m 0750 -o 10010 -g 10010 /var/lib/repolens

# 3. The sandbox image, built on the host daemon the worker uses.
./scripts/build-sandbox.sh                     # repolens-sandbox:py3.12-1

# 4. Build and start. migrate runs first, then api, worker and web.
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build --wait

# 5. Check.
docker compose -f docker-compose.prod.yml --env-file .env.prod ps
curl -s http://127.0.0.1:3100/api/ready        # {"status":"ready",...}
```

Required variables in `.env.prod`:

| Variable | Purpose |
|---|---|
| `REPOLENS_API_TOKEN` | Shared bearer token for mutations. The web proxy attaches it server-side. |
| `POSTGRES_PASSWORD` | Database password. Use hex or URL-safe characters, because it is embedded in the connection URL. |
| `REPOLENS_HOST_DATA_DIR` | Absolute host path for mirrors and test workspaces (default `/var/lib/repolens`). |
| `REPOLENS_DOCKER_GID` | Group id of `/var/run/docker.sock` on the host. |
| `REPOLENS_APP_UID` / `REPOLENS_APP_GID` | Non-root user for `api` and `worker`. It must own the data directory (default 10010). |

Optional: `REPOLENS_WEB_PORT`, `REPOLENS_MAX_CONCURRENT_SANDBOXES`,
`REPOLENS_SANDBOX_MEMORY_MB`, `REPOLENS_SANDBOX_CPUS`, `REPOLENS_LLM_PROVIDER`, and the Caddy
variables below. The remaining settings are documented in `src/repolens/config.py`.

The data directory is mounted at the same path inside the worker because the worker hands
workspace paths to the host Docker daemon as bind-mount sources. A different path inside the
container would point the sandbox at the wrong host directory.

## TLS and authentication with Caddy

RepoLens has no user accounts. The web proxy adds the API token to every same-origin
mutation, so anyone who can open the web app acts as the operator: they can register
repositories, queue analyses and sandboxed runs, and spend model credit if a provider is
enabled. Always put authentication in front of it.

The bundled Caddy service provides automatic HTTPS (Let's Encrypt) and basic auth:

```bash
# in .env.prod
REPOLENS_PUBLIC_HOST=repolens.example.com
REPOLENS_BASIC_AUTH_USER=operator
REPOLENS_BASIC_AUTH_HASH='<output of the command below>'

docker run --rm caddy:2.10.2-alpine caddy hash-password --plaintext 'a long passphrase'

docker compose -f docker-compose.prod.yml --env-file .env.prod --profile tls up -d --wait
```

The configuration is in [`infra/caddy/Caddyfile`](../infra/caddy/Caddyfile). Bcrypt hashes
contain `$`, so single-quote the value in `.env.prod` to stop Compose interpolating it.

If you already run a reverse proxy on the host (Caddy, nginx, Traefik), skip the profile and
proxy to `127.0.0.1:3100`. Keep the original `Host` header. If your proxy rewrites `Host`
and sets `X-Forwarded-Host` itself, set `REPOLENS_TRUST_FORWARDED_HOST=true`; otherwise
leave it `false`. The web app uses these headers for its same-origin (CSRF) check on
mutations.

Never publish the API port. Read endpoints are unauthenticated and include job details such
as error messages and worker host names.

## Upgrades and migrations

```bash
git pull
docker compose -f docker-compose.prod.yml --env-file .env.prod build
docker compose -f docker-compose.prod.yml --env-file .env.prod stop worker   # optional, see below
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --wait
```

`up` re-runs `migrate`, which applies any new Alembic migrations before `api` and `worker`
start again. `GET /api/ready` reports `not_ready` while the schema is behind. Stopping the
worker first is optional: a job interrupted mid-run has its lease expire, and it is retried
or failed by the next worker.

If the sandbox Dockerfile or its requirements changed, rebuild the image with
`./scripts/build-sandbox.sh` before restarting the worker. Workers refuse any image other than
`REPOLENS_SANDBOX_IMAGE`.

Analysis results are tied to parser and engine versions. After an upgrade, older commits are
analysed again on request rather than mixed across versions.

To pin a release, set `REPOLENS_IMAGE_TAG` (for example to a Git tag) before building, so
you can roll back to the previous images. Alembic migrations are forward-only in practice, so
take a database backup before upgrading.

## Backups

PostgreSQL holds everything that matters: repositories, snapshots, analyses, impact
reports, test plans and runs, and the job queue. Mirrors under the data directory can be
fetched again.

```bash
# Backup (consistent logical dump, compressed)
docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T postgres \
  pg_dump -U repolens -d repolens -Fc > repolens-$(date +%F).dump

# Restore, with the app services stopped
docker compose -f docker-compose.prod.yml --env-file .env.prod stop api worker web
docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T postgres \
  pg_restore -U repolens -d repolens --clean --if-exists < repolens-2026-01-01.dump
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --wait
```

Run the backup from cron or a systemd timer, copy dumps off the host, and test a restore now
and then. Keep `.env.prod` in your secret store: without `POSTGRES_PASSWORD` you cannot
connect, and a new `REPOLENS_API_TOKEN` means updating every API client.

There is no automatic retention. Data grows until you delete rows or mirrors by hand (see
[operations.md](operations.md#data-and-retention)).

## Logs and monitoring

- All services log to stdout. RepoLens logs are JSON with `request_id`, `job_id` and
  `worker_id`. Use `docker compose ... logs -f api worker`, or ship them with your log driver.
- Health checks: `api` checks `/api/ready` (database plus migrations at head), `web` checks
  its home page, `worker` checks that it can reach the Docker daemon, and `postgres` runs
  `pg_isready`.
- Queue health: the **Queue** page, or `GET /api/metrics/queue`. It reports per-kind counts,
  p50/p95 queue and run times, retries and expired leases.

## Tearing down

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod --profile tls down      # keep data
docker compose -f docker-compose.prod.yml --env-file .env.prod --profile tls down -v   # also delete the database volume
```

Mirrors and workspaces in `REPOLENS_HOST_DATA_DIR` are not removed by `down`.
