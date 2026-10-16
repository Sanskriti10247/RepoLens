# Deployment and operations

RepoLens is built and tested as a **single-host** deployment: API, workers, web app,
PostgreSQL and Docker on one machine. It has not been run as a hosted multi-tenant service.
Treat the guidance below as the intended operating model, not a battle-tested runbook.

## Components

| Process | Command | Needs |
|---|---|---|
| API | `uv run repolens-api` | PostgreSQL |
| Worker | `uv run repolens-worker` (run one or more) | PostgreSQL, data directory, `git`, Docker, outbound HTTPS to GitHub |
| Web | `npm run web:build`, then `npm --workspace apps/web run start` | the API, and `REPOLENS_API_TOKEN` in its environment |
| Database | PostgreSQL 16 | migrations applied with `uv run alembic upgrade head` |

Build the sandbox image once on each worker host: `./scripts/build-sandbox.sh`. The base image
and the Python packages inside it are pinned by digest and hash. Workers refuse any image
other than `REPOLENS_SANDBOX_IMAGE`.

## Configuration checklist

Settings come from `REPOLENS_*` environment variables, or `.env` (see
`src/repolens/config.py`).

- Set `REPOLENS_API_TOKEN` to a long random value. Without it, every mutating endpoint is
  disabled.
- Set `REPOLENS_LOG_FORMAT=json` for log shipping. Logs carry `request_id`, `job_id` and
  `worker_id`.
- Point `REPOLENS_DATA_DIR` at a disk with room for mirrors. Repositories are capped by
  `REPOLENS_MAX_REPOSITORY_SIZE_MB` (default 500). Jobs refuse to start below
  `REPOLENS_MIN_FREE_DISK_MB`.
- Keep the API bound to `127.0.0.1` (the default). If remote access is needed, put the web
  app behind a TLS reverse proxy. The API itself has no TLS, no user accounts and no rate
  limiting. It has one shared token.
- Leave `REPOLENS_ALLOW_LOCAL_REPOSITORIES` off. It exists for tests and offline demos.
- The language-model provider stays off unless you set `REPOLENS_LLM_PROVIDER=anthropic`.
  See the README for what is sent.

## Security-relevant operating facts

- **The worker can run containers, which makes it root-equivalent on the host.** Run workers
  on a host, or in a VM, dedicated to RepoLens. The sandbox restrictions limit what
  *repository code* can do. They do not protect the host from a compromised worker process.
  See [security/sandbox.md](security/sandbox.md) for the threat model and residual risks.
- **Repository code runs only inside the sandbox.** Ingestion, parsing and analysis read Git
  objects and never execute them.
- **Untrusted output is bounded.** Sandbox output reaches RepoLens only through a validated,
  size-capped envelope. Logs are truncated.

## Scaling

- Run more workers to process more jobs. Claiming uses `SKIP LOCKED`, so workers never share
  a job.
- Concurrency limits apply across all workers:
  - `REPOLENS_MAX_JOBS_PER_REPOSITORY` (default 2);
  - `REPOLENS_MAX_CONCURRENT_SANDBOXES` (default 2).

  Size the sandbox limit to the host. Each sandbox may use up to `REPOLENS_SANDBOX_MEMORY_MB`
  of memory and `REPOLENS_SANDBOX_CPUS` CPUs.
- Workers on different hosts each keep their own mirrors and need their own sandbox image.

## Failure handling

| Failure | What happens |
|---|---|
| GitHub unreachable, fetch times out | Retryable: requeued with backoff, up to 3 attempts, then `FAILED` (`fetch_failed`) |
| Repository private, too large, ref not found or ambiguous | Permanent failure with a specific category. No retry. |
| Object integrity failure while fetching | Fetch rejected (`transfer.fsckObjects`). Known-harmless metadata quirks are accepted. |
| A file fails to parse, is too large, or is not UTF-8 | That file is marked and a warning stored. The analysis completes as `PARTIAL`. |
| Worker crashes or is killed | Its lease expires. The sweeper requeues the job, or fails it once attempts are used up. At startup, the next worker removes leftover containers and workspaces. |
| Worker loses its lease, for example after a long database stall | It stops writing. All its updates are conditional on holding the lease. |
| Test run exceeds time, memory or process limits | `TIMED_OUT` or `RESOURCE_LIMIT_EXCEEDED`. The container is killed and removed. |
| Docker unavailable or the image is missing | `INFRASTRUCTURE_ERROR`, retryable |
| A test tries to escape the policy | `SECURITY_POLICY_BLOCKED` |
| Cancellation requested | Queued jobs are cancelled immediately. Running jobs stop at their next checkpoint, and running containers are stopped. |
| Database unavailable | `GET /api/ready` returns 503. Workers log the error and keep polling. Jobs that are running fail their heartbeat and are recovered later. |
| Model provider error, refusal or invalid citations | The deterministic answer is returned, with the reason |

Watch the queue at `GET /api/metrics/queue` and on the web app's **Queue** page. The endpoint
reports, per job kind:
- counts by status;
- the median and p95 time spent queued and running;
- how many jobs were retried.

It also reports active and expired leases.

## Upgrades

1. Stop the workers. Running jobs are recovered if they are interrupted.
2. Deploy the new code.
3. Run `uv run alembic upgrade head`.
4. Start the API and workers. `/api/ready` reports `not_ready` until the schema is at head.

Analysis results are tied to parser and engine versions. A new version analyses commits
again on request, rather than mixing results across versions.

## Data and retention

- Nothing is deleted automatically, apart from sandbox workspaces and containers after each
  run.
- Snapshots, analyses, reports, test runs and the parse cache all stay in PostgreSQL. Mirrors
  stay in `REPOLENS_DATA_DIR/mirrors`.
- Back up PostgreSQL to keep results. Mirrors can be fetched again.
- There is no retention policy yet. Deleting data means deleting rows or mirrors by hand.
