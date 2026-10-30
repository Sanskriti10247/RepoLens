# Architecture

RepoLens has three processes: the **API** (FastAPI), one or more **workers**, and the **web
app** (Next.js). They share one **PostgreSQL** database. Workers also use a local data
directory for Git mirrors and test workspaces, and **Docker** for sandboxed test runs.

```
browser ──> web (Next.js, :3100) ──proxy + token──> API (FastAPI, :8100) ──> PostgreSQL
                                                                               ^   jobs
                                                    worker(s) ─────────────────┘
                                                      │  git mirrors, workspaces (data dir)
                                                      └─ docker run (sandbox image, no network)
```

The API never runs Git or Docker. It validates requests, writes rows and enqueues jobs.
Everything slow or untrusted happens in a worker.

## Code layout

`src/repolens` is layered, and `import-linter` enforces the layering in CI:

| Layer | Packages | May import |
|---|---|---|
| entry points | `api`, `worker` | services and below |
| services | `services` | persistence, domain, analysis libraries |
| persistence | `persistence` (models, queue, graph queries, bulk writer) | domain |
| domain | `domain` (enums, spans, job state machine) | nothing internal |

The analysis libraries have no database or web code, so they can be tested in isolation:

| Package | Contents |
|---|---|
| `repository` | URL validation, hardened Git runner, mirrors, inventory, safe materialiser |
| `parsers` | Tree-sitter adapter |
| `codegraph` | module naming, resolver, traversal |
| `impact` | diff mapping, propagation |
| `test_selection` | evidence and policies |
| `execution` | sandbox policy, Docker runner, outcomes |
| `llm` | optional provider |

## Data model

Everything a pipeline derives is immutable and scoped. Results are never patched in place;
a new input gets a new row.

| Entity | Identity | Notes |
|---|---|---|
| `repositories` | canonical URL | provider, default branch |
| `repository_snapshots` | repository × commit SHA | file inventory in `source_files` (path, blob SHA, mode, language, size) |
| `analysis_runs` | snapshot × parser version × engine version × config hash | one partial unique "result" row per key. A repeat request becomes a `REUSED` run pointing at the equivalent run. |
| `symbols`, `symbol_references`, `dependency_edges`, `analysis_files`, `analysis_warnings` | analysis run | the code graph. Every reference has a resolution status and rule. Every edge has a provenance and confidence. Functions and methods carry their cyclomatic complexity, which the health report ([insights.md](insights.md)) reads. |
| `change_sets` → `changed_files`, `changed_symbols` | repository × base ref × target ref | resolved SHAs and both analysis runs recorded |
| `impact_reports`, `impact_items` | change set | each item keeps its path back to a change and its evidence |
| `test_cases` | analysis run | pytest tests, discovered statically when first needed |
| `test_plans`, `test_plan_items` | impact report × policy | every included or excluded test has evidence or an exclusion reason |
| `test_runs`, `test_results`, `coverage_records` | plan or analysis | outcome, counts, bounded logs, sandbox limits used |
| `parse_cache` | blob SHA × path × module × parser version | parser output reused across analyses |
| `analysis_progress` | analysis run | the worker's latest progress report for a run (stage, files done, current file). A separate table, not a column on `analysis_runs`: the analysis transaction holds the run row `FOR UPDATE` while it parses and writes, and progress is written from short transactions of its own |
| `jobs` | — | the work queue (below) |

Status columns are text with CHECK constraints, mirroring the enums in `domain/enums.py`.
Migrations live in `migrations/` and are applied with Alembic.

## Pipelines

**Analysis** (`ANALYZE_REPOSITORY`)
1. Preflight the provider: public, within the size limit.
2. Fetch the bare mirror (hardened Git) and resolve the ref to a full SHA.
3. Create the snapshot from `git ls-tree`, if this commit has none yet.
4. Parse each Python file, or reuse its cached parse.
5. Resolve names across the snapshot.
6. Bulk-load the graph with `COPY`.
7. Discover pytest tests statically.

The worker reports progress through these stages as `fetch`, `snapshot`, `parse`, `resolve`,
`persist` and `tests` (`services/progress.py`). Stage changes are written at once; updates
within a stage (files done, the file being read) at most every 0.75 s. `GET /analyses/{id}`
returns the latest report as `progress`, and keeps it after the run ends: a `done` report
with each stage's wall time, or, for a failed run, the stage it stopped in.

**Change analysis** (`ANALYZE_CHANGE`)
1. Resolve both refs. The fetch is skipped when both are full SHAs already mirrored.
2. Ensure both commits have an analysis; reuse when possible.
3. Diff the trees and map hunks to symbols.
4. Propagate impact backwards over the graph. `IMPORTS` is followed one hop only. Heuristic
   seeds are added for module-level code and unresolved names.
5. Compute risk, completeness and recommended scope.

**Test planning**. This step is synchronous, in the API, because it is fast and touches only
the database.
- The first plan for an analysis builds its test catalog: pytest tests discovered statically
  from the analysed symbols. `POST /analyses/{id}/collect` runs real `pytest --collect-only`
  in the sandbox.
- Every test gets evidence: test changed, static direct, coverage, static transitive, import,
  heuristic, module dependency, previously failed (never enough on its own).
- The conservative policy falls back to the full suite when a change cannot be traced. The
  budgeted policy caps the count and records `BUDGET` exclusions.

**Test run** (`RUN_TESTS`)
1. Materialise the snapshot into a fresh workspace. Writes use `O_NOFOLLOW` and `O_EXCL`
   relative to a directory fd, and symlinks are created last and only if contained.
2. `docker run` the pinned sandbox image with the restrictions in
   [security/sandbox.md](security/sandbox.md).
3. Parse the bounded result envelope.
4. Store the outcome, results and optional coverage. Remove the workspace.

**Questions**. Synchronous.
1. Retrieve evidence deterministically from the graph, warnings and runs.
2. Optionally ask the configured model.
3. Validate the model's citations against the evidence.
4. Fall back to the deterministic answer on any problem.

## Job queue

Jobs live in PostgreSQL (`persistence/jobs.py`).

**Claiming**
- `SELECT … FOR UPDATE SKIP LOCKED`, ordered by priority and `run_after`.
- Claim limits: per repository (a row lock on the repository) and per kind (an advisory
  lock). Sandbox runs are capped at `max_concurrent_sandboxes`.

**Running**
- Leases use the database clock. A heartbeat thread extends the lease while the handler runs.
- All state changes are conditional on the worker still holding the lease. A worker that has
  lost its lease writes nothing.

**Outcomes**
- Handler returns: `SUCCEEDED`.
- `RetryableJobError`: requeued with backoff until `max_attempts` (default 3).
- `PermanentJobError`: `FAILED`, with a category.
- `PolicyBlockedError`: `BLOCKED`.
- Cancellation: `CANCELLED`.
- Any other exception is a defect: `FAILED` (`internal_error`), with no retry.

**Recovery**
- A sweeper requeues jobs whose lease expired, or fails them once their attempts are used up.
- At startup, a worker removes sandbox containers and workspaces older than the sandbox
  timeout plus 15 minutes.

## Web app

`apps/web` is a Next.js App Router application.
- Pages fetch from `/api/*`, which a server-side route proxies to the API.
- The proxy adds the API token, so the token never reaches the browser.
- The proxy only forwards mutating requests that are same-origin JSON.
- No business logic lives in the UI. It renders what the API returns, including unknowns
  and warnings.
- The visual views draw only recorded data: the analysis pipeline from `progress` and
  `timings_ms`, the module graph from `GET /analyses/{id}/modules` (laid out in the browser,
  no layout library), the charts from `…/stats`, and the question trace from the fields of
  the answer. The question endpoint reports no intermediate steps, so while it runs the
  trace shows only the request itself. All of them respect `prefers-reduced-motion`.
