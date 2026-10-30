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
  similar). The API, worker and web app run there, but sandboxed test runs and
  `pytest --collect-only` need a Docker daemon. Set `REPOLENS_SANDBOX_ENABLED=false`: those
  requests are then refused with 409 and code `sandbox_unavailable`, and `/api/health`
  reports `"sandbox": false`. Test plans still work.
- Kubernetes as is. The compose layout does not translate directly. Workers
  would need a dedicated node pool with Docker, or a different sandbox backend, which RepoLens
  does not implement.
- Docker Desktop on macOS or Windows, and rootless Docker. These are untested; bind-mount
  path translation and resource limits behave differently there.

The web app on its own can be hosted anywhere Node.js runs, as long as it can reach the API
privately. A small instance without test execution fits free tiers; see
[Free hosting](#free-hosting-vercel--render). Test execution still needs the worker on a
Docker host.

## Free hosting (Vercel + Render)

Demo mode runs a small working instance on free tiers: the web app on Vercel and everything
else in one Render web service. The image in `infra/docker/demo/Dockerfile` holds the API,
the worker and a small PostgreSQL server (the Debian package of the base image, version 17).
On every start it creates an empty database, restores `infra/demo/snapshot.tar.gz` (the
database plus the Git mirrors the API reads source from), starts the API and, once the API
listens, the worker. If either process exits, the container exits and Render restarts it.

Visitors can register public GitHub repositories, analyze them, explore the graph, run impact
analysis, build test plans and ask questions. Tests are not run: there is no Docker daemon,
so the image sets `REPOLENS_SANDBOX_ENABLED=false`. Test runs and sandboxed collection are
refused with 409 and code `sandbox_unavailable`, `/api/health` reports `"sandbox": false`,
and test plans fall back to the statically discovered tests. The worker never calls
`docker`.

**Nothing is kept.** The database and mirrors live in the container's filesystem. Every
restart, deploy, or wake-up from sleep goes back to the snapshot, and repositories visitors
added are gone. Render free web services sleep after 15 minutes without traffic. To keep
data and give each visitor their own repositories, use
[Sign-in with Supabase](#sign-in-with-supabase).

The snapshot holds `mahmoud/boltons` at `5150802` with an impact report against `574393a`, a
test plan (108 of 399 tests, confirmed with sandboxed collection) and a sandboxed run of
those tests (203 passed), recorded on a Docker host. It is about 3 MB.

The image sets limits sized for 512 MiB of memory and a tenth of a CPU. Any of them can be
overridden in Render's environment:

| Setting | Demo image | Default |
|---|---|---|
| `REPOLENS_MAX_REPOSITORY_SIZE_MB` | 50 | 500 |
| `REPOLENS_MAX_FILE_COUNT` | 5000 | 50000 |
| `REPOLENS_MIN_FREE_DISK_MB` | 200 | 1024 |
| `REPOLENS_MAX_JOBS_PER_REPOSITORY` | 1 | 2 |
| `REPOLENS_GIT_TIMEOUT_SECONDS` | 120 | 300 |

Larger repositories fail with a message such as `repository_too_large: Repository is larger
than 50 MiB` (GitHub's reported size is checked before anything is fetched). The worker runs
one job at a time, so a second visitor's analysis waits in the queue.

The image includes the `llm` extra. It is imported only when `REPOLENS_LLM_PROVIDER` is set,
so it costs no memory otherwise. Questions are answered from the analysis without a model by
default; to use one, set `REPOLENS_LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY` on Render,
and read [operations.md](operations.md) first about what gets sent.

`REPOLENS_READ_ONLY=true` still turns the image into a showcase: every request other than
GET, HEAD and OPTIONS is refused with 403 and code `read_only`, and the worker is not
started.

Measured with `docker run --memory 512m --memory-swap 512m --cpus 0.1 -e PORT=10000`,
registering and analyzing through the API. The host was busy (load average 7 to 12 on 12
cores), so the times are ranges over two runs:

| | |
|---|---|
| Container start to `/api/ready` returning 200 | 55 to 64 s (read-only mode: the same) |
| Analysis of `pallets/itsdangerous` at `HEAD` | 9 to 13 s |
| Impact `2.1.2..2.2.0` on `itsdangerous`, including analysis of both revisions | 8 to 11 s |
| Analysis of `pallets/flask` at `HEAD` | 36 to 54 s |
| Impact `3.0.0..3.1.0` on `flask` | 45 to 67 s |
| Peak cgroup memory (`memory.peak`, includes page cache), after both repositories | 277 to 317 MiB |
| Anonymous memory after both repositories | 175 to 179 MiB |
| VmHWM, API / worker / largest PostgreSQL process | 103 MB / 92 MB / 48 MB |

Start-up is mostly the snapshot restore and Python imports; the read-only image measured
35 s on a quiet host.
The first request after Render wakes the service waits for all of it. The web app's `/api`
proxy allows up to 60 seconds.

Render:

- New Web Service, language Docker, from the repository.
- Dockerfile path `infra/docker/demo/Dockerfile`, Docker build context directory `.`.
- Instance type Free. Health check path `/api/ready`.
- Environment: `REPOLENS_API_TOKEN`, a long random value
  (`python -c "import secrets; print(secrets.token_urlsafe(32))"`). Without it the instance
  starts, logs `demo_writes_disabled` and refuses every change with `mutations_disabled`.
  Render sets `PORT`, and the image reads it. Do not attach a disk.

Vercel:

- Import the repository with Root Directory `apps/web` and Framework Preset Next.js. Leave
  "Include source files outside of the Root Directory" on, so the npm workspace at the
  repository root installs, and leave the build and install commands at their defaults.
- Environment: `REPOLENS_API_URL=https://<your-service>.onrender.com` and the same
  `REPOLENS_API_TOKEN` as on Render. The web app's server adds it to requests it forwards;
  browsers never see it. Do not set `REPOLENS_WEB_STANDALONE`.
- `REPOLENS_TRUST_FORWARDED_HOST` is not needed: Vercel passes the public host through as
  `Host`, which already matches the browser's `Origin`.

Anyone who can open the web app can register and analyze repositories with that token. The
limits above, and the fact that nothing outlives a restart, are what keep that cheap.

To rebuild the snapshot, run the normal local stack (README, "Getting started") against a
separate database and data directory, for example `repolens_demo` and `var/demo-data`, with
the sandbox image built. Register the repository, analyze the two revisions through an
impact analysis, run collection, create a test plan and run it, then:

```bash
DEMO_DB=repolens_demo DEMO_DATA_DIR=var/demo-data ./scripts/make_demo_snapshot.sh
docker build -f infra/docker/demo/Dockerfile -t repolens-demo .
docker run --rm --memory 512m --memory-swap 512m -e PORT=10000 -p 127.0.0.1:18300:10000 repolens-demo
```

The script dumps a scratch copy of the database with worker and lease fields cleared and the
parse cache emptied, and stops if the dump contains this host's name, home directory or the
API token. Questions are answered on request and not stored, so they are not part of the
snapshot.

## Sign-in with Supabase

By default RepoLens has a single operator: whoever can open the web app acts with the API
token. With Supabase, visitors sign in with Google, and each user sees and changes only the
repositories they added. The database moves to Supabase Postgres, so it survives restarts.
Git mirrors stay on the service's local disk; when one is missing (a free-tier restart wipes
the disk), the worker fetches it again the first time someone needs the source, and the page
shows "Fetching the source again" until it is back.

Without `REPOLENS_SUPABASE_URL` on the API and the two `NEXT_PUBLIC_SUPABASE_*` variables on
the web app, everything behaves as described above.

1. **Create a project** at supabase.com. From Project Settings, note the project URL
   (`https://<ref>.supabase.co`) and the anon (publishable) API key. The anon key is meant
   for browsers; never use the service-role key here.
2. **Google OAuth client.** In Google Cloud Console, APIs & Services, set up the OAuth
   consent screen, then Credentials, Create credentials, OAuth client ID, type Web
   application. Add one authorised redirect URI: `https://<ref>.supabase.co/auth/v1/callback`.
   Copy the client ID and secret.
3. **Enable the provider.** In Supabase, Authentication, Sign In / Providers, Google: turn
   it on and paste the client ID and secret.
4. **URL configuration.** In Authentication, URL Configuration, set the Site URL to the web
   app (`https://<your-app>.vercel.app`) and add these redirect URLs:
   - `http://localhost:3100/auth/callback`
   - `https://<your-app>.vercel.app/auth/callback`

   Supabase only sends visitors back to URLs on this list.
5. **Database URL.** In Connect, copy the **Session pooler** string (port 5432). Change the
   scheme to `postgresql+psycopg://` and add `?sslmode=require`:

   ```
   postgresql+psycopg://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require
   ```

   Percent-encode special characters in the password. Do not use the Transaction pooler
   (port 6543): it hands each transaction to a different server connection, which breaks
   the prepared statements psycopg creates and the session state the job queue relies on.
   The direct connection (`db.<ref>.supabase.co`) is IPv6-only on the free plan, which
   Render cannot reach. The job queue's `FOR UPDATE SKIP LOCKED` claims and
   transaction-scoped advisory locks work through the Session pooler unchanged.

Environment variables:

| Where | Variable | Value |
|---|---|---|
| Render (API and worker) | `REPOLENS_DATABASE_URL` | the Session pooler URL from step 5 |
| Render | `REPOLENS_SUPABASE_URL` | `https://<ref>.supabase.co` |
| Render, only for legacy projects | `REPOLENS_SUPABASE_JWT_SECRET` | the project's legacy JWT secret, if it still signs with HS256 |
| Vercel (web) | `REPOLENS_API_URL` | `https://<your-service>.onrender.com`, as before |
| Vercel | `NEXT_PUBLIC_SUPABASE_URL` | `https://<ref>.supabase.co` |
| Vercel | `NEXT_PUBLIC_SUPABASE_ANON_KEY` | the anon (publishable) key |

`REPOLENS_API_TOKEN` is not used in this mode; remove it from both services. The
`NEXT_PUBLIC_*` values are built into the web app, so redeploy on Vercel after setting them.

With `REPOLENS_DATABASE_URL` set, the demo image skips its embedded PostgreSQL and the
snapshot restore, and only runs `alembic upgrade head` against Supabase on each start. The
database starts empty; the boltons demo data is not loaded into it.

The API checks each request's token against the project's published keys
(`/auth/v1/.well-known/jwks.json`, cached and fetched again when a new signing key appears):
signature, issuer `https://<ref>.supabase.co/auth/v1`, audience `authenticated` and expiry.
The web app keeps the Supabase session in a cookie and forwards the user's access token to
the API from its server; the static token is never sent. Repositories registered before
sign-in was turned on have no owner and stay hidden.

To try it locally, put `REPOLENS_SUPABASE_URL` (and the Supabase `REPOLENS_DATABASE_URL`, or
keep the local one) in `.env`, and the two `NEXT_PUBLIC_SUPABASE_*` values in
`apps/web/.env.local`. The localhost redirect URL from step 4 covers `npm run dev`.

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

Without [Supabase sign-in](#sign-in-with-supabase) RepoLens has no user accounts. The web
proxy adds the API token to every same-origin mutation, so anyone who can open the web app
acts as the operator: they can register repositories, queue analyses and sandboxed runs,
and spend model credit if a provider is enabled. Always put authentication in front of it.

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
