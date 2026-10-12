#!/usr/bin/env bash
# ============================================================================
# restore.sh — restore a backup.sh archive
#
# Usage:
#   ./scripts/restore.sh backups/transcoder-backup-XXXX.tar.gz          # full DB
#   ./scripts/restore.sh backups/transcoder-backup-XXXX.tar.gz --config # JSON config only
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

ARCHIVE="${1:?usage: restore.sh <archive.tar.gz> [--config]}"
MODE="${2:-full}"

ENV_FILE=".env"
[[ -f "$ENV_FILE" ]] && set -a && source "$ENV_FILE" && set +a
HTTP_PORT="${HTTP_PORT:-8080}"
POSTGRES_USER="${POSTGRES_USER:-transcoder}"
POSTGRES_DB="${POSTGRES_DB:-transcoder}"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
tar -xzf "$ARCHIVE" -C "$WORK"

if [[ "$MODE" == "--config" ]]; then
    echo "Importing JSON config through the API (database untouched)…"
    curl -fsS -X POST -H "Content-Type: application/json" \
        --data-binary "@$WORK/config.json" \
        "http://localhost:${HTTP_PORT}/api/v1/config/import"
    echo
    echo "Config imported."
    exit 0
fi

read -r -p "FULL restore: the current database will be overwritten. Confirm? (yes) " answer
[[ "$answer" == "yes" ]] || { echo "Cancelled."; exit 1; }

echo "Stopping the app (database stays up)…"
docker compose stop app
echo "pg_restore…"
docker compose exec -T db pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    --clean --if-exists --no-owner < "$WORK/db.dump"
echo "Restarting…"
docker compose start app
echo "Restore complete."
