# RepoLens

RepoLens builds a structured model of a Python repository — symbols, imports, calls,
inheritance and tests — and uses it to explain the impact of a code change, select the
tests that exercise the affected code, and run them in a restricted container.

> Status: early development. Only the capabilities listed under **What works today** are
> implemented. Everything else is planned, not available.

## What works today

- Project scaffolding, tooling and health endpoints.

## Development

Requirements: Python ≥ 3.12 with [uv](https://docs.astral.sh/uv/), Docker with Compose,
Node.js ≥ 20 with npm.

```bash
uv sync                      # install Python dependencies
uv run pytest                # unit tests
```
