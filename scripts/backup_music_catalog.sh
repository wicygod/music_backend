#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${MUSIC_BACKEND_APP_DIR:-/opt/music_backend}"
REPO_DIR="${MUSIC_BACKEND_BACKUP_REPO:-/opt/music_backend_backup_repo}"
DB_FILE="${MUSIC_CATALOG_DB:-$APP_DIR/music_catalog.db}"
BRANCH="${MUSIC_BACKUP_BRANCH:-main}"
LOG_PREFIX="[music-catalog-backup]"

if [[ ! -f "$DB_FILE" ]]; then
  echo "$LOG_PREFIX database not found: $DB_FILE"
  exit 1
fi

if [[ ! -d "$REPO_DIR/.git" ]]; then
  echo "$LOG_PREFIX git backup repo not found: $REPO_DIR"
  exit 1
fi

tmp_db="$(mktemp --suffix=.db)"
cleanup() {
  rm -f "$tmp_db"
}
trap cleanup EXIT

if command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 "$DB_FILE" ".backup '$tmp_db'"
else
  cp "$DB_FILE" "$tmp_db"
fi

cd "$REPO_DIR"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

cp "$tmp_db" "$REPO_DIR/music_catalog.db"
git add music_catalog.db

if git diff --cached --quiet -- music_catalog.db; then
  echo "$LOG_PREFIX no database changes"
  exit 0
fi

git -c user.name="music-backend-bot" \
  -c user.email="music-backend-bot@users.noreply.github.com" \
  commit -m "Backup music catalog $(date -u +'%Y-%m-%d %H:%M:%S UTC')"

git push origin "$BRANCH"
echo "$LOG_PREFIX pushed database backup"
