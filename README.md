# RepoLens

RepoLens builds a structured model of a Python repository (symbols, imports, calls,
inheritance and tests) and uses it to explain what a code change affects, choose the tests
that exercise the affected code, and run them in a locked-down container. Every conclusion
carries the evidence it was drawn from. What RepoLens cannot determine is reported as unknown,
not guessed.

Scope: Python repositories tested with pytest, hosted on GitHub (public, HTTPS).

## What it does

- **Ingestion.** A background worker fetches a bare mirror with a hardened Git configuration
  (no hooks, no system or global config, no protocol other than HTTPS, object checks on).
  It resolves a branch, tag or commit to a full SHA and records an immutable snapshot: the file
  inventory, content hashes, languages and unsafe-path findings. Repository code is never run
  outside the sandbox.
- **Code intelligence.** Tree-sitter extracts modules, classes, functions, methods, nested
  definitions, decorators, imports, calls, base classes and callback references, with exact
  source spans. A syntax error affects only the region it is in. Parse results for unchanged
  files are reused between analyses.
- **Name resolution and dependency graph.** Every reference is stored with an explicit status
  (`RESOLVED`, `AMBIGUOUS`, `EXTERNAL`, `UNRESOLVED` or `DYNAMIC`) and the rule that produced
  it. The typed graph (`CONTAINS`, `IMPORTS`, `CALLS`, `INHERITS`, `REFERENCES`) lives in
  PostgreSQL. Each edge records its provenance and an `EXACT` or `HEURISTIC` confidence.
- **Change impact.** Two refs are compared down to the changed symbols. Impact is propagated
  backwards through the graph, and each impacted symbol keeps its path back to a change.
  Each report includes:
  - a risk level with the factors behind it;
  - a completeness verdict, with the reason for every gap (unparsed files, unresolved names,
    non-Python changes);
  - a recommended test scope.
  Documentation and CI or editor configuration are treated as having no runtime effect.
- **Test selection.** pytest tests are discovered statically, and each selected test lists
  its evidence: test changed, direct or transitive static dependency, import, heuristic link,
  module dependency, coverage from an earlier run, or previously failed.
  - The default policy is conservative: when a change cannot be traced (packaging config,
    `conftest.py`, data files with no linked test), the plan falls back to the full suite and
    records the reason.
  - A budgeted policy is available. It lists every test it leaves out and why.
- **Sandboxed execution.** Selected or full test runs execute in a Docker container with:
  - no network;
  - a non-root user and no Linux capabilities;
  - a read-only root and a read-only source mount;
  - memory, CPU, process and time limits.

  Results come back only through a bounded, validated channel. Each run ends in one of these
  outcomes: `PASSED`, `FAILED`, `TIMED_OUT`, `CANCELLED`, `COLLECTION_ERROR`,
  `RESOURCE_LIMIT_EXCEEDED`, `SECURITY_POLICY_BLOCKED` or `INFRASTRUCTURE_ERROR`. See
  [docs/security/sandbox.md](docs/security/sandbox.md).
- **Repository questions.** Questions are answered from evidence retrieved deterministically
  from the graph, analysis warnings and test results, with citations. A language model is
  optional (see below). Without one, RepoLens returns the retrieved evidence and a structured
  summary.
- **Web interface.** A Next.js app for:
  - repositories and analyses;
  - symbol exploration with source;
  - impact reports;
  - test plans and runs;
  - questions;
  - the job queue.
- **Reliable jobs.** A PostgreSQL-backed queue with leases, heartbeats, retries with backoff,
  cancellation, recovery of abandoned jobs, and per-repository and sandbox concurrency limits.

### Known analysis limits

Python is dynamic, so static analysis has blind spots:
- RepoLens does not infer the types of variables or return values. Calls on instance
  attributes (`self.client.get()`) or on values returned by other calls are therefore
  reported as unresolved.
- `getattr`, `importlib`, `eval` and subscripted calls are reported as dynamic.
- Calls through `self`, `cls` and `super()` follow an approximate method resolution order and
  are marked heuristic.

These gaps lower the completeness of an impact report and push test selection towards a
broader scope. Coverage from an earlier sandboxed run, when available, adds evidence that
static analysis misses.

### Language model policy

The model provider is off by default (`REPOLENS_LLM_PROVIDER=none`), and analysis, impact,
selection and execution never depend on it. When it is enabled
(`REPOLENS_LLM_PROVIDER=anthropic`, `uv sync --extra llm`, `ANTHROPIC_API_KEY` set):
- Source code is withheld unless `REPOLENS_LLM_ALLOW_SOURCE_UPLOAD=true`.
- With source withheld, only symbol names, paths, graph facts, warnings and test outcomes
  are sent. Docstrings and source snippets count as source and are withheld too.
- Answers must cite the retrieved evidence. An answer with unknown citations, a refusal or an
  error falls back to the deterministic answer.
- RepoLens checks that every cited evidence id exists. It cannot check that the explanation
  is true, and repository text can try to steer the model. The cited evidence is therefore
  always shown next to the explanation.

The provider integration is tested against a fake client only. It has not been exercised
against the live API in this repository's test suite.

## Running locally

Requirements:
- Linux;
- Python ≥ 3.12 with [uv](https://docs.astral.sh/uv/);
- Docker with Compose (cgroup v2);
- Node.js ≥ 22 with npm.

```bash
cp .env.example .env                 # then set REPOLENS_API_TOKEN
docker compose up -d postgres        # PostgreSQL on 127.0.0.1:5433
uv sync                              # Python dependencies
uv run alembic upgrade head          # apply migrations
./scripts/build-sandbox.sh           # build the pinned sandbox image (repolens-sandbox:py3.12-1)

uv run repolens-api                  # API on http://127.0.0.1:8100 (OpenAPI docs at /api/docs)
uv run repolens-worker               # background worker (ingestion, analysis, impact, test runs)

npm ci                               # web dependencies
REPOLENS_API_TOKEN=... npm run web:dev   # UI on http://127.0.0.1:3100
```

The web app proxies `/api/*` to the API and adds the API token on the server side, so the
token never reaches the browser. Mutating requests through the proxy must be same-origin JSON
requests. Reads are open on the API; every mutation requires the bearer token.

Configuration is read from `REPOLENS_*` environment variables or `.env`. See
`src/repolens/config.py` for the limits on repository size, file counts, timeouts, sandbox
resources, concurrency and the parse cache.

### Checks

```bash
./scripts/check.sh                   # format, lint, types, layering, Python tests, web checks
REPOLENS_REQUIRE_DOCKER=1 ./scripts/check.sh   # also fail if sandbox tests cannot run
./scripts/e2e.sh                     # browser workflow against a real API, worker and database
```

Integration tests use the `repolens_test` database and reset its schema on each run. They are
skipped when PostgreSQL is unreachable unless `REPOLENS_REQUIRE_INTEGRATION=1` is set, as in
CI. Sandbox tests need Docker and the sandbox image.

### Benchmarks

`scripts/benchmark.py` measures analysis, incremental analysis, impact, selection overhead,
sandbox startup and selected-versus-full test runs on real repositories. Results and
methodology are in [docs/benchmarks](docs/benchmarks/).
