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
| health | `GET /health`, `GET /ready` (database reachable, migrations at head) |
| repositories | `GET/POST /repositories`, `GET /repositories/{id}`, `GET/POST /repositories/{id}/analyses` |
| analyses | `GET /analyses/{id}`, `POST /analyses/{id}/cancel`, `GET /analyses/{id}/snapshot-issues`, `…/files`, `…/warnings`, `…/modules`, `…/stats`, `…/tests` |
| graph | `GET /repositories/{id}/symbols`, `…/symbols/{symbol_id}`, `…/graph`, `…/paths`, `…/files/{file_id}/source` |
| impact | `POST/GET /repositories/{id}/impact`, `GET /impact/{id}`, `POST /impact/{id}/cancel`, `…/files`, `…/changed-symbols`, `…/report`, `…/items`, `…/test-plans` |
| test plans | `POST /impact/{id}/test-plan`, `GET /test-plans/{id}`, `…/items` |
| test runs | `POST/GET /test-runs`, `GET /test-runs/{id}`, `POST /test-runs/{id}/cancel`, `…/results`, `…/logs`, `…/coverage`, `POST /analyses/{id}/collect` |
| jobs | `GET /jobs`, `GET /jobs/{id}`, `GET /metrics/queue` |
| questions | `POST /repositories/{id}/questions` |

All paths are under `/api`.
