#!/usr/bin/env bash
# Makes a dated backup of the site's data and (optionally) uploads it to Google Drive or Dropbox with rclone.
#   scripts/backup.sh                      -> backup into ./backups
#   RCLONE_REMOTE=gdrive:santa-backups scripts/backup.sh   -> and upload it there too
# The database is copied with SQLite's own backup command, so the copy is always consistent.
# Passwords and the session key (data/*.txt) are deliberately left out.
set -euo pipefail
cd "$(dirname "$0")/.."
DATA="${DATA_DIR:-data}"
OUT="${BACKUP_DIR:-backups}"
KEEP_DAYS="${KEEP_DAYS:-60}"
STAMP="$(date +%Y-%m-%d_%H%M)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$OUT" "$WORK/data"

python3 - "$DATA/santa.db" "$WORK/data/santa.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
PY
[ -d "$DATA/img" ] && cp -r "$DATA/img" "$WORK/data/img"

FILE="$OUT/santa-$STAMP.tar.gz"
tar -czf "$FILE" -C "$WORK" data
echo "Backup written: $FILE"

if [ -n "${RCLONE_REMOTE:-}" ]; then
  rclone copy "$FILE" "$RCLONE_REMOTE" --quiet
  echo "Uploaded to $RCLONE_REMOTE"
  rclone delete "$RCLONE_REMOTE" --min-age "${KEEP_DAYS}d" --quiet || true
fi
find "$OUT" -name 'santa-*.tar.gz' -mtime +"$KEEP_DAYS" -delete
