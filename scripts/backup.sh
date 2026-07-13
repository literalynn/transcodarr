#!/usr/bin/env bash
# ============================================================================
# backup.sh — sauvegarde en UNE commande (§9.5.10) :
#   1. pg_dump complet (état, historique, config) ;
#   2. export JSON de la configuration via l'API (réimportable via l'UI).
# Produit : backups/transcoder-backup-<date>.tar.gz
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

echo "1/3 pg_dump de $POSTGRES_DB…"
docker compose exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom \
    > "$WORK/db.dump"

echo "2/3 export JSON de la config…"
curl -fsS "http://localhost:${HTTP_PORT}/api/v1/config/export" > "$WORK/config.json"

echo "3/3 archive…"
mkdir -p backups
tar -czf "backups/transcoder-backup-${STAMP}.tar.gz" -C "$WORK" db.dump config.json
echo "OK : backups/transcoder-backup-${STAMP}.tar.gz"
echo "(le dossier data/ complet reste sauvegardable par un simple tar, §9.1)"
