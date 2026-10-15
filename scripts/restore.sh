#!/usr/bin/env bash
# Restore a dump made by scripts/backup.sh.
#
#   scripts/restore.sh --check backups/shortener-XXXX.dump   # SAFE drill: restores into a scratch
#                                                            # database, prints row counts, drops it
#   scripts/restore.sh backups/shortener-XXXX.dump           # DESTRUCTIVE: replaces the live database
#   scripts/restore.sh --yes backups/...                     # same, without the confirmation prompt
#
# Run the --check drill regularly (e.g. monthly): an untested backup is only a hope.
# Fetch an off-site copy first with:  rclone copy "$BACKUP_RCLONE_REMOTE/<file>" backups/
set -euo pipefail
cd "$(dirname "$0")/.."

cfg() {
  local v="${!1:-}"
  if [ -z "$v" ] && [ -f .env ]; then v="$(grep -E "^$1=" .env | tail -n1 | cut -d= -f2- || true)"; fi
  printf '%s' "${v:-${2:-}}"
}
log() { printf '%s restore: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

MODE=restore; ASSUME_YES=0; FILE=""
for arg in "$@"; do
  case "$arg" in
    --check) MODE=check ;;
    --yes) ASSUME_YES=1 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    -*) die "unknown option $arg" ;;
    *) FILE="$arg" ;;
  esac
done
[ -n "$FILE" ] && [ -f "$FILE" ] || die "usage: $0 [--check] [--yes] <dump file>"

PGUSER_="$(cfg POSTGRES_USER shortener)"
PGDB="$(cfg POSTGRES_DB shortener)"
PG_EXEC="${PG_EXEC:-docker compose exec -T postgres}"
# SKIP_APP_CONTROL=1 leaves the api container and migrations alone (used by the tests).
SKIP_APP_CONTROL="${SKIP_APP_CONTROL:-0}"

# shellcheck disable=SC2086 # PG_EXEC is intentionally word-split
sql()  { $PG_EXEC psql -U "$PGUSER_" -d "${2:-postgres}" -v ON_ERROR_STOP=1 -qAt -c "$1"; }
# shellcheck disable=SC2086
load() { $PG_EXEC pg_restore -U "$PGUSER_" -d "$1" --no-owner --exit-on-error < "$FILE"; }

# Verify the checksum written by backup.sh, when present.
if [ -f "$FILE.sha256" ]; then
  expected="$(cut -d' ' -f1 "$FILE.sha256")"
  actual="$( (sha256sum "$FILE" 2>/dev/null || shasum -a 256 "$FILE") | cut -d' ' -f1)"
  [ "$expected" = "$actual" ] || die "checksum mismatch: the dump is corrupt or was modified"
  log "checksum OK"
fi

if [ "$MODE" = check ]; then
  scratch="${PGDB}_restore_check"
  cleanup() { sql "DROP DATABASE IF EXISTS \"$scratch\" WITH (FORCE)" >/dev/null 2>&1 || true; }
  trap cleanup EXIT
  cleanup
  sql "CREATE DATABASE \"$scratch\""
  load "$scratch" || die "restore into scratch database failed"
  users="$(sql 'SELECT count(*) FROM users' "$scratch")"
  links="$(sql 'SELECT count(*) FROM links' "$scratch")"
  rev="$(sql 'SELECT version_num FROM alembic_version' "$scratch")"
  log "RESTORE CHECK OK: users=$users links=$links schema_revision=$rev"
  exit 0
fi

if [ "$ASSUME_YES" != 1 ]; then
  printf 'This REPLACES the live database "%s" with %s.\nType RESTORE to continue: ' "$PGDB" "$FILE" >&2
  read -r answer
  [ "$answer" = RESTORE ] || die "aborted"
fi

if [ "$SKIP_APP_CONTROL" != 1 ]; then
  log "stopping api"
  docker compose stop api
fi

log "recreating database '$PGDB'"
sql "DROP DATABASE IF EXISTS \"$PGDB\" WITH (FORCE)"
sql "CREATE DATABASE \"$PGDB\""
load "$PGDB" || die "restore failed; the database is empty. Fix the cause and re-run."

if [ "$SKIP_APP_CONTROL" != 1 ]; then
  log "applying any newer migrations, then starting api"
  docker compose run --rm migrate
  docker compose up -d api
fi
log "restore complete: $(sql 'SELECT count(*) FROM users' "$PGDB") users, $(sql 'SELECT count(*) FROM links' "$PGDB") links"
