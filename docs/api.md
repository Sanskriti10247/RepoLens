# HTTP API

The full, generated reference is served by the running API:
- interactive docs at `/api/docs`;
- the schema at `/api/openapi.json`.

This page covers conventions and the main workflow.

## Conventions

- **Authentication.** Reads are open. Every mutating endpoint requires
  `Authorization: Bearer $REPOLENS_API_TOKEN`. If no token is configured, mutations are
  disabled.
- **Errors** share one shape:
  `{"error": {"code": "...", "message": "...", "request_id": "...", "details": ...}}`.
  The code is stable and machine-readable, for example `impact_not_ready`,
  `invalid_repository_url` or `validation_error`.
- **Request ids.** Each response carries `X-Request-ID`. A valid incoming one is reused.
- **Pagination.** List endpoints take `limit` (1–100, default 20) and `offset`, and return
  `items` and `page` (`total`, `limit`, `offset`).
- **Asynchronous work.** Analyses, impact and test runs return `202` with a resource. Poll
  the resource, or its `job`, until it finishes:
  - Analyses and impact go `QUEUED → RUNNING`, then `SUCCEEDED`, `FAILED` or `CANCELLED`.
    Analyses can also end `PARTIAL` (some files not analysed) or `REUSED` (an equivalent
    run already existed).
  - Test runs go `QUEUED → RUNNING → COMPLETED`, and their result is in `outcome`.
  - While a request is still queued or running, repeating it returns the same resource
    instead of queuing duplicate work.

## Workflow

```bash
H="Authorization: Bearer $REPOLENS_API_TOKEN"; API=http://127.0.0.1:8100/api

# 1. register a repository and analyse a ref
repo=$(curl -s -H "$H" -H 'content-type: application/json' \
  -d '{"url":"https://github.com/pallets/itsdangerous"}' $API/repositories | jq -r .id)
curl -s -H "$H" -H 'content-type: application/json' -d '{"ref":"main"}' \
  $API/repositories/$repo/analyses

# 2. explore the graph
curl -s "$API/repositories/$repo/symbols?q=sign"
curl -s "$API/repositories/$repo/symbols/{symbol_id}"            # callers, callees, unresolved
curl -s "$API/repositories/$repo/paths?from={id}&to={id}"

# 3. impact of a change
impact=$(curl -s -H "$H" -H 'content-type: application/json' \
  -d '{"base":"<sha or ref>","target":"main"}' $API/repositories/$repo/impact | jq -r .id)
curl -s $API/impact/$impact/report                               # risk, completeness, scope
curl -s $API/impact/$impact/items                                # impacted symbols with paths

# 4. plan and run tests
plan=$(curl -s -H "$H" -H 'content-type: application/json' -d '{"policy":"CONSERVATIVE"}' \
  $API/impact/$impact/test-plan | jq -r .id)
curl -s $API/test-plans/$plan/items                              # evidence per test
run=$(curl -s -H "$H" -H 'content-type: application/json' -d "{\"plan_id\":\"$plan\"}" \
  $API/test-runs | jq -r .id)
curl -s $API/test-runs/$run/results

# 5. ask a question
curl -s -H "$H" -H 'content-type: application/json' \
  -d '{"question":"what calls Signer.sign?"}' $API/repositories/$repo/questions
```

## Endpoints

| Area | Endpoints |
|---|---|
| health | `GET /health`, `GET /ready` (database reachable, migrations at head). `/health` also reports `read_only`; with `REPOLENS_READ_ONLY=true` every request other than GET, HEAD and OPTIONS returns 403 with code `read_only`. It reports `sandbox`, false when `REPOLENS_SANDBOX_ENABLED=false`; test runs and collection then return 409 with code `sandbox_unavailable` |
| repositories | `GET/POST /repositories`, `GET /repositories/{id}`, `GET/POST /repositories/{id}/analyses` |
| analyses | `GET /analyses/{id}`, `POST /analyses/{id}/cancel`, `GET /analyses/{id}/snapshot-issues`, `…/files`, `…/warnings`, `…/modules`, `…/stats`, `…/tests`, `…/insights` |
| graph | `GET /repositories/{id}/symbols`, `…/symbols/{symbol_id}`, `…/graph`, `…/paths`, `…/files/{file_id}/source` |
| impact | `POST/GET /repositories/{id}/impact`, `GET /impact/{id}`, `POST /impact/{id}/cancel`, `…/files`, `…/changed-symbols`, `…/report`, `…/items`, `…/test-plans` |
| test plans | `POST /impact/{id}/test-plan`, `GET /test-plans/{id}`, `…/items` |
| test runs | `POST/GET /test-runs`, `GET /test-runs/{id}`, `POST /test-runs/{id}/cancel`, `…/results`, `…/logs`, `…/coverage`, `POST /analyses/{id}/collect` |
| jobs | `GET /jobs`, `GET /jobs/{id}`, `GET /metrics/queue` |
| questions | `POST /repositories/{id}/questions` |

Test plans, test runs and collection use pytest. For a repository without Python files they
return 422 with code `language_not_supported` and `details.language` (the most common parsed
language, for example `typescript`).

All paths are under `/api`.

`GET /analyses/{id}` includes `progress`, the worker's latest report for the run: `stage`
(one of `stages`, or `done`), `stage_index`, `stage_count`, `files_done` and `files_total`
where the stage works through files, `current` (the file being read), `detail`,
`stage_started_at`, `durations_ms` for the stages finished so far, and `updated_at`. It is
`null` until a worker starts the run, and the last report stays after the run ends.

### Repository health report

`GET /analyses/{id}/insights` returns a health score and the metrics behind it for a
finished analysis. It returns 404 for an unknown id and 409 with code `analysis_not_ready`
while the analysis is queued or running, or if it ended without results (`FAILED`,
`CANCELLED`). For a `REUSED` analysis it reads the results of the run in `results_run_id`.

```bash
curl -s $API/analyses/$run/insights | jq '.score'
```

The response has these sections:

- `score`: `overall` (0-100, or `null` if no component has data), `grade` (A-F) and
  `components`, each with `key`, `label`, `score` (`null` when excluded), the `weight`
  actually applied and a one-line `detail`.
- `overview`: file counts by role, files, bytes and lines per language, total bytes, and
  symbol counts by kind.
- `complexity`: `available` (false for analyses parsed before complexity was recorded),
  the distribution in buckets 1-5, 6-10, 11-20 and 21+, `mean`, `p90` and the ten most
  complex functions.
- `size`: the ten longest functions and the ten largest analysed source files.
- `documentation`: public symbols, how many have a docstring, and the most called
  undocumented ones.
- `testing`: test files and functions, testable modules with and without tests, and the
  untested modules other modules depend on most.
- `coupling`: per-module fan-in, fan-out and instability, import cycles (module names), and
  the definitions with the most callers.
- `dead_code`: the number of candidates, the ten largest, and a note on what static
  analysis cannot see.
- `references`: counts per resolution status (`resolved`, `ambiguous`, `external`,
  `unresolved`, `dynamic`) and `resolution_rate`.
- `findings`: rule-based findings with `id`, `severity`, `category`, `title`, `detail` and
  `evidence` (`path`, `line`, `symbol_id`, `file_id`, `label`).

Symbols carry `symbol_id`, `file_id`, `path` and `line`, so a client can link to
`/repositories/{id}/symbols/{symbol_id}` or `/repositories/{id}/files/{file_id}/source`.
Lists hold at most ten items and findings at most five pieces of evidence.
Formulas, thresholds and the dead-code rules are in [insights.md](insights.md).
