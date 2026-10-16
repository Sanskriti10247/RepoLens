#!/usr/bin/env bash
# Start the full stack against a fresh database and a fixture repository, then run the
# browser end-to-end tests. Requires Docker (Postgres + sandbox image) and Playwright browsers.
set -euo pipefail
cd "$(dirname "$0")/.."

API_PORT="${E2E_API_PORT:-8101}"
WEB_PORT="${E2E_WEB_PORT:-3101}"
WORK="$(mktemp -d -t repolens-e2e-XXXXXX)"
TOKEN="e2e-$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
DB_URL="postgresql+psycopg://repolens:repolens@127.0.0.1:5433/repolens_e2e"
pids=()

cleanup() {
  # Each service runs in its own session; kill the whole group (npx/uv spawn children).
  for pid in "${pids[@]}"; do kill -- "-$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

docker compose up -d --wait postgres >/dev/null
docker compose exec -T postgres psql -U repolens -d repolens -qc "DROP DATABASE IF EXISTS repolens_e2e" >/dev/null
docker compose exec -T postgres psql -U repolens -d repolens -qc "CREATE DATABASE repolens_e2e" >/dev/null
REPOLENS_DATABASE_URL="$DB_URL" uv run alembic upgrade head >/dev/null

FIXTURE_URL="$(uv run python scripts/make_fixture_repo.py "$WORK/repos/inventory")"

export REPOLENS_DATABASE_URL="$DB_URL" REPOLENS_API_TOKEN="$TOKEN" REPOLENS_DATA_DIR="$WORK/data" \
  REPOLENS_API_PORT="$API_PORT" REPOLENS_ALLOW_LOCAL_REPOSITORIES=true \
  REPOLENS_LOCAL_REPOSITORY_ROOTS="[\"$WORK/repos\"]" REPOLENS_LOG_FORMAT=json \
  REPOLENS_MIN_FREE_DISK_MB=0
for port in "$API_PORT" "$WEB_PORT"; do
  if curl -s -o /dev/null "http://127.0.0.1:$port/"; then
    echo "port $port is already in use; stop the process using it first" >&2
    exit 1
  fi
done
setsid uv run repolens-api >"$WORK/api.log" 2>&1 & pids+=($!)
setsid uv run repolens-worker >"$WORK/worker.log" 2>&1 & pids+=($!)
(cd apps/web && REPOLENS_API_URL="http://127.0.0.1:$API_PORT" REPOLENS_API_TOKEN="$TOKEN" \
  NEXT_TELEMETRY_DISABLED=1 exec setsid npx next start --port "$WEB_PORT" --hostname 127.0.0.1 \
  >"$WORK/web.log" 2>&1) & pids+=($!)

for _ in $(seq 1 60); do
  curl -sf "http://127.0.0.1:$WEB_PORT/api/ready" >/dev/null && break
  sleep 1
done

status=0
(cd apps/web && E2E_BASE_URL="http://127.0.0.1:$WEB_PORT" E2E_FIXTURE_URL="$FIXTURE_URL" \
  npx playwright test "$@") || status=$?
if [[ $status -ne 0 ]]; then
  echo "--- api.log (tail)"; tail -20 "$WORK/api.log"
  echo "--- worker.log (tail)"; tail -20 "$WORK/worker.log"
fi
exit $status
