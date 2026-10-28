#!/usr/bin/env bash
# Pack a demo snapshot for the read-only demo image (infra/docker/demo/Dockerfile).
#
# The snapshot is one tarball holding a pg_dump of the showcase database and the bare Git
# mirrors the API reads source from. Build the showcase first with the normal local stack
# (README, "Getting started") against a separate database and data directory, then:
#
#   ./scripts/make_demo_snapshot.sh                    # repolens_demo, var/demo-data
#   DEMO_DB=other DEMO_DATA_DIR=/path ./scripts/make_demo_snapshot.sh
#
# Needs `docker compose up -d postgres` (pg_dump and psql run inside that container). The
# dump is taken from a scratch copy of the database, in which host-specific job fields are
# cleared, and the script fails if the result still contains this host's name, home
# directory or the API token.
set -euo pipefail
cd "$(dirname "$0")/.."

DB="${DEMO_DB:-repolens_demo}"
DATA_DIR="${DEMO_DATA_DIR:-var/demo-data}"
OUT="${DEMO_SNAPSHOT:-infra/demo/snapshot.tar.gz}"
SCRATCH_DB="${DB}_snapshot"
PG_USER="${DEMO_PG_USER:-repolens}"

[[ -d "$DATA_DIR/mirrors" ]] || { echo "no mirrors in $DATA_DIR" >&2; exit 1; }
work="$(mktemp -d -t repolens-snapshot-XXXXXX)"
pg() { docker compose exec -T postgres "$@"; }
cleanup() {
  pg psql -U "$PG_USER" -d postgres -qc "DROP DATABASE IF EXISTS \"$SCRATCH_DB\"" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

# Scratch copy (the live database may have connections, so dump and restore instead of
# CREATE DATABASE ... TEMPLATE), then clear worker identity and lease fields.
pg psql -U "$PG_USER" -d postgres -qc "DROP DATABASE IF EXISTS \"$SCRATCH_DB\"" \
  -c "CREATE DATABASE \"$SCRATCH_DB\""
pg pg_dump -U "$PG_USER" -d "$DB" -Fc | pg pg_restore -U "$PG_USER" -d "$SCRATCH_DB" --no-owner
pg psql -U "$PG_USER" -d "$SCRATCH_DB" -q -v ON_ERROR_STOP=1 <<'SQL'
UPDATE jobs SET lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL
 WHERE status NOT IN ('CLAIMED', 'RUNNING');
-- Only the schema is needed for the parse cache; it only speeds up new analyses.
TRUNCATE parse_cache;
SQL
pg pg_dump -U "$PG_USER" -d "$SCRATCH_DB" -Fc --no-owner --no-privileges >"$work/db.dump"

# Secret and host-name check over the restored text of the dump.
leaks=0
patterns=("$HOME" "$(hostname)")
[[ -n "${REPOLENS_API_TOKEN:-}" ]] && patterns+=("$REPOLENS_API_TOKEN")
text="$(pg pg_restore -f - <"$work/db.dump" 2>/dev/null || true)"
for p in "${patterns[@]}"; do
  [[ -n "$p" ]] || continue
  if grep -qF -- "$p" <<<"$text"; then echo "snapshot contains '$p'" >&2; leaks=1; fi
done
[[ $leaks -eq 0 ]] || exit 1

mkdir -p "$work/pack"
cp "$work/db.dump" "$work/pack/db.dump"
mkdir -p "$work/pack/mirrors"
for m in "$DATA_DIR"/mirrors/*.git; do
  cp -r "$m" "$work/pack/mirrors/"
  rm -rf "$work/pack/mirrors/$(basename "$m")/hooks"
  git -C "$work/pack/mirrors/$(basename "$m")" -c gc.auto=0 repack -a -d -q 2>/dev/null || true
done
if grep -rqIF -- "$HOME" "$work/pack/mirrors"; then
  echo "mirrors contain $HOME" >&2; exit 1
fi

mkdir -p "$(dirname "$OUT")"
tar --owner=0 --group=0 --numeric-owner --sort=name -C "$work/pack" -czf "$OUT" .
du -h "$OUT" | cut -f1 | xargs echo "wrote $OUT:"
tar -tzf "$OUT" | grep -c . | xargs echo "entries:"
