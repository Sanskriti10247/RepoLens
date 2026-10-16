# RepoLens

RepoLens builds a structured model of a Python repository — symbols, imports, calls,
inheritance and tests — and uses it to explain the impact of a code change, select the
tests that exercise the affected code, and run them in a restricted container.

> Status: early development. Only the capabilities listed under **What works today** are
> implemented. Everything else is planned, not available.

## What works today

- **Repository ingestion** of public GitHub repositories (`https://github.com/<owner>/<repo>`):
  a background worker fetches a bare mirror with a hardened Git configuration, resolves the
  requested branch, tag or commit to a full SHA, and records an immutable snapshot (file
  inventory, content hashes, languages, unsafe-path findings). Repository code is never run.
- **Python code intelligence** with Tree-sitter: modules, classes, functions, methods, nested
  definitions, decorators, imports, calls, base classes and callback references, with exact
  source spans. Syntax errors are contained to the affected region.
- **Static name resolution** across the repository. Every reference is stored with an explicit
  status — `RESOLVED`, `AMBIGUOUS`, `EXTERNAL`, `UNRESOLVED` or `DYNAMIC` — and the rule that
  produced it. Unknown relationships are reported, not guessed.
- **Typed dependency graph** in PostgreSQL (`CONTAINS`, `IMPORTS`, `CALLS`, `INHERITS`,
  `REFERENCES`) with provenance and confidence (`EXACT` or `HEURISTIC`).
- **Graph API**: symbol search and detail (callers, callees, dependents, unresolved references),
  bounded neighbourhood traversal, shortest dependency paths, module dependency summary with
  import cycles, statistics, per-file analysis status, warnings and source at the analysed commit.

Not yet implemented: change-impact analysis, test selection, sandboxed test execution,
repository questions and most of the web interface. See the API documentation at
`/api/docs` for the current surface.

### Known analysis limits

Python is dynamic. RepoLens does not infer the types of variables or return values, so calls on
instance attributes (`self.client.get()`) or on values returned by other calls are reported as
unresolved. `getattr`, `importlib`, `eval` and subscripted calls are reported as dynamic.
Method calls through `self`, `cls` and `super()` use an approximate method resolution order and
are marked heuristic, with additional heuristic edges to overrides in known subclasses.

## Development

Requirements: Python ≥ 3.12 with [uv](https://docs.astral.sh/uv/), Docker with Compose,
Node.js ≥ 22 with npm.

```bash
cp .env.example .env                 # then set REPOLENS_API_TOKEN
docker compose up -d postgres        # PostgreSQL on 127.0.0.1:5433
uv sync                              # Python dependencies
uv run alembic upgrade head          # apply migrations
uv run repolens-api                  # API on http://127.0.0.1:8100 (docs at /api/docs)

npm ci                               # web dependencies
REPOLENS_API_TOKEN=... npm run web:dev   # UI on http://127.0.0.1:3100
```

The web app proxies `/api/*` to the API and attaches the API token on the server side, so the
token is never sent to the browser. Mutating requests through the proxy must be same-origin
JSON requests.

### Checks

```bash
./scripts/check.sh                   # everything below, stopping at the first failure
uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run lint-imports
uv run pytest                        # unit + integration (integration needs the compose database)
npm run web:check                    # lint, type check and tests for the web app
```

Integration tests use the `repolens_test` database and reset its schema on each run. They are
skipped when PostgreSQL is unreachable unless `REPOLENS_REQUIRE_INTEGRATION=1` is set (as in CI).
