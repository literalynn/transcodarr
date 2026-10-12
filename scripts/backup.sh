#!/usr/bin/env bash
# ============================================================================
# backup.sh — one-command backup:
#   1. full pg_dump (state, history, config);
#   2. JSON config export through the API (re-importable from the UI).
# Output: backups/transcoder-backup-<date>.tar.gz
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE=".env"
[[ -f "$ENV_FILE" ]] && set -a && source "$ENV_FILE" && set +a
HTTP_PORT="${HTTP_PORT:-8080}"
POSTGRES_USER="${POSTGRES_USER:-transcoder}"
POSTGRES_DB="${POSTGRES_DB:-transcoder}"

STAMP=$(date +%Y%m%d-%H%M%S)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

echo "1/3 pg_dump of $POSTGRES_DB…"
docker compose exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom \
    > "$WORK/db.dump"

echo "2/3 JSON config export…"
curl -fsS "http://localhost:${HTTP_PORT}/api/v1/config/export" > "$WORK/config.json"

echo "3/3 archive…"
mkdir -p backups
tar -czf "backups/transcoder-backup-${STAMP}.tar.gz" -C "$WORK" db.dump config.json
echo "OK: backups/transcoder-backup-${STAMP}.tar.gz"
echo "(the whole data/ folder can also be backed up with a plain tar)"
