#!/usr/bin/env bash
# Start a throwaway PostgreSQL, restore the demo snapshot, then run the API and the worker.
# If either process exits the container exits too, so the host restarts it.
set -euo pipefail

HOME_DIR="${REPOLENS_DEMO_HOME:-/tmp/repolens-demo}"
PGBIN="$(echo /usr/lib/postgresql/*/bin)"
PGDATA="$HOME_DIR/pg"
SNAPSHOT="${REPOLENS_DEMO_SNAPSHOT:-/opt/demo/snapshot.tar.gz}"
PG_PORT=5432
started=$SECONDS

rm -rf "$HOME_DIR"
mkdir -p "$HOME_DIR/restore" "$HOME_DIR/data" "$HOME_DIR/sock"
chmod 0700 "$HOME_DIR"

tar -xzf "$SNAPSHOT" -C "$HOME_DIR/restore"
mv "$HOME_DIR/restore/mirrors" "$HOME_DIR/data/mirrors"
mkdir -p "$HOME_DIR/data/git-home" "$HOME_DIR/data/workspaces"

# Local-only trust is fine: PostgreSQL listens on loopback inside this container and only
# the API's port is published.
"$PGBIN/initdb" -D "$PGDATA" -U repolens --encoding=UTF8 --locale=C.UTF-8 \
  --auth-local=trust --auth-host=trust >/dev/null
cat >>"$PGDATA/postgresql.conf" <<CONF
listen_addresses = '127.0.0.1'
port = $PG_PORT
unix_socket_directories = '$HOME_DIR/sock'
max_connections = 20
shared_buffers = 16MB
work_mem = 4MB
maintenance_work_mem = 32MB
effective_cache_size = 64MB
wal_level = minimal
max_wal_senders = 0
fsync = off
synchronous_commit = off
full_page_writes = off
max_wal_size = 64MB
autovacuum = off
logging_collector = off
log_min_messages = warning
CONF
"$PGBIN/pg_ctl" -D "$PGDATA" -w -t 60 -l "$HOME_DIR/postgres.log" start >/dev/null
"$PGBIN/createdb" -h 127.0.0.1 -p "$PG_PORT" -U repolens repolens
"$PGBIN/pg_restore" -h 127.0.0.1 -p "$PG_PORT" -U repolens -d repolens --no-owner \
  --exit-on-error "$HOME_DIR/restore/db.dump"
"$PGBIN/psql" -h 127.0.0.1 -p "$PG_PORT" -U repolens -d repolens -qc "ANALYZE" >/dev/null
rm -rf "$HOME_DIR/restore"
echo "{\"event\": \"demo_database_ready\", \"seconds\": $((SECONDS - started))}"

export REPOLENS_DATABASE_URL="postgresql+psycopg://repolens@127.0.0.1:$PG_PORT/repolens"
export REPOLENS_DATA_DIR="$HOME_DIR/data"
export REPOLENS_API_PORT="${PORT:-8000}"

read_only="${REPOLENS_READ_ONLY:-false}"
if [[ "${read_only,,}" =~ ^(1|true|yes|on)$ ]]; then
  exec repolens-api
fi
if [[ -z "${REPOLENS_API_TOKEN:-}" ]]; then
  echo '{"event": "demo_writes_disabled", "detail": "REPOLENS_API_TOKEN is not set; registering and analyzing repositories is refused"}'
fi

pids=()
stop() {
  trap - TERM INT
  kill -TERM "${pids[@]}" 2>/dev/null || true
  wait "${pids[@]}" 2>/dev/null || true
  "$PGBIN/pg_ctl" -D "$PGDATA" -m fast -w stop >/dev/null || true
}
trap 'stop; exit 143' TERM INT

repolens-api &
pids+=($!)
# On a tenth of a CPU the two Python start-ups compete; let the API answer health checks first.
(
  until (exec 3<>"/dev/tcp/127.0.0.1/$REPOLENS_API_PORT") 2>/dev/null; do sleep 1; done
  exec repolens-worker
) &
pids+=($!)

status=0
wait -n "${pids[@]}" || status=$?
echo "{\"event\": \"demo_process_exited\", \"status\": $status}"
stop
exit $(( status == 0 ? 1 : status ))
