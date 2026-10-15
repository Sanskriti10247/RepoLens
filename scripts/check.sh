#!/usr/bin/env bash
# Run every local quality gate; stops at the first failure.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== python: format";  uv run ruff format --check .
echo "== python: lint";    uv run ruff check .
echo "== python: types";   uv run mypy
echo "== python: imports"; uv run lint-imports
echo "== python: tests";   uv run pytest -q "$@"
if [[ "${SKIP_WEB:-0}" != "1" ]]; then
  echo "== web: lint";  npm --workspace apps/web run --silent lint
  echo "== web: types"; npm --workspace apps/web run --silent typecheck
  echo "== web: tests"; npm --workspace apps/web run --silent test
fi
echo "== all checks passed"
