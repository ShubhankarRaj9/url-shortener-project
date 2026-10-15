#!/usr/bin/env bash
# PostgreSQL backup: dump -> verify -> checksum -> optional off-site copy -> prune old copies.
#
#   scripts/backup.sh
#
# Settings (environment variables or .env):
#   BACKUP_DIR              where dumps are kept on this host              (default ./backups)
#   BACKUP_KEEP_DAYS        delete local dumps older than this              (default 14)
#   BACKUP_RCLONE_REMOTE    off-site destination, e.g. "offsite:my-bucket/shortener"   (default: none)
#   BACKUP_REMOTE_KEEP_DAYS delete off-site dumps older than this            (default 30)
#   POSTGRES_USER / POSTGRES_DB                                              (default shortener)
#   PG_EXEC                 how to reach the PostgreSQL tools               (default: docker compose exec -T postgres)
#
# The off-site copy uses rclone (https://rclone.org), which speaks S3, Backblaze, GCS, SFTP, ... Use an
# rclone "crypt" remote so dumps (which contain password hashes) are encrypted before they leave this host.
set -euo pipefail
cd "$(dirname "$0")/.."

# Read a setting from the environment first, then from .env. (.env is not sourced: it isn't valid shell.)
cfg() {
  local v="${!1:-}"
  if [ -z "$v" ] && [ -f .env ]; then v="$(grep -E "^$1=" .env | tail -n1 | cut -d= -f2- || true)"; fi
  printf '%s' "${v:-${2:-}}"
}
log() { printf '%s backup: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }
sha() { sha256sum "$1" 2>/dev/null || shasum -a 256 "$1"; }

BACKUP_DIR="$(cfg BACKUP_DIR ./backups)"
KEEP_DAYS="$(cfg BACKUP_KEEP_DAYS 14)"
REMOTE="$(cfg BACKUP_RCLONE_REMOTE)"
REMOTE_KEEP_DAYS="$(cfg BACKUP_REMOTE_KEEP_DAYS 30)"
PGUSER_="$(cfg POSTGRES_USER shortener)"
PGDB="$(cfg POSTGRES_DB shortener)"
PG_EXEC="${PG_EXEC:-docker compose exec -T postgres}"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

# One backup at a time (cron overlap protection).
if command -v flock >/dev/null; then
  exec 9>"$BACKUP_DIR/.lock"
  flock -n 9 || die "another backup is already running"
fi

name="shortener-$(date -u +%Y%m%dT%H%M%SZ).dump"
[ ! -e "$BACKUP_DIR/$name" ] || name="${name%.dump}-$$.dump"   # never overwrite an existing backup
final="$BACKUP_DIR/$name"
partial="$final.partial"
trap 'rm -f "$partial"' EXIT

log "dumping database '$PGDB'"
# shellcheck disable=SC2086 # PG_EXEC is intentionally word-split
$PG_EXEC pg_dump -U "$PGUSER_" -d "$PGDB" --format=custom --no-owner > "$partial" || die "pg_dump failed"
[ -s "$partial" ] || die "dump is empty"

# A dump that cannot be listed is not a backup.
# shellcheck disable=SC2086
$PG_EXEC pg_restore --list < "$partial" > /dev/null || die "dump failed verification"

mv "$partial" "$final"
( cd "$BACKUP_DIR" && sha "$name" > "$name.sha256" )
log "wrote $final ($(wc -c < "$final" | tr -d ' ') bytes)"

if [ -n "$REMOTE" ]; then
  command -v rclone >/dev/null || die "BACKUP_RCLONE_REMOTE is set but rclone is not installed"
  log "uploading to $REMOTE"
  rclone copy "$final" "$REMOTE/" --checksum || die "off-site upload failed"
  rclone copy "$final.sha256" "$REMOTE/" || die "off-site upload of checksum failed"
  log "pruning off-site dumps older than ${REMOTE_KEEP_DAYS}d"
  rclone delete "$REMOTE/" --min-age "${REMOTE_KEEP_DAYS}d" --include 'shortener-*' || log "WARNING: off-site prune failed"
else
  log "WARNING: BACKUP_RCLONE_REMOTE not set; the only copy of this backup is on this host"
fi

log "pruning local dumps older than ${KEEP_DAYS}d"
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'shortener-*' -mtime "+$KEEP_DAYS" -delete

log "done: $name"
