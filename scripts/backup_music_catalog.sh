#!/usr/bin/env bash
set -euo pipefail

DB_FILE="${MUSIC_CATALOG_DB:-/var/lib/music-backend/music_catalog.db}"
BACKUP_DIR="${MUSIC_BACKUP_DIR:-/var/backups/music-backend}"
KEEP_DAYS="${MUSIC_BACKUP_KEEP_DAYS:-7}"
LOG_PREFIX="[music-backend-backup]"

if [[ ! -f "$DB_FILE" ]]; then
  echo "$LOG_PREFIX database not found: $DB_FILE"
  exit 1
fi

install -d -m 0700 "$BACKUP_DIR"
timestamp="$(date -u +'%Y%m%dT%H%M%SZ')"
backup_file="$BACKUP_DIR/music_backend-$timestamp.db"
tmp_db="$(mktemp --tmpdir="$BACKUP_DIR" .music-backend-backup.XXXXXX.db)"
cleanup() {
  rm -f "$tmp_db"
}
trap cleanup EXIT

if command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 "$DB_FILE" ".backup '$tmp_db'"
else
  cp "$DB_FILE" "$tmp_db"
fi

mv "$tmp_db" "$backup_file"
chmod 0600 "$backup_file"
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'music_backend-*.db' -mtime "+$KEEP_DAYS" -delete
echo "$LOG_PREFIX created private local backup: $backup_file"
echo "$LOG_PREFIX backups intentionally never enter Git; copy them to encrypted private storage"
