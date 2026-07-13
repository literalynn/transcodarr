#!/usr/bin/env bash
# ============================================================================
# restore.sh — restauration d'une sauvegarde backup.sh
#
# Usage :
#   ./scripts/restore.sh backups/transcoder-backup-XXXX.tar.gz          # DB complète
#   ./scripts/restore.sh backups/transcoder-backup-XXXX.tar.gz --config # config JSON seule
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
    echo "Import de la configuration JSON via l'API (DB intacte)…"
    curl -fsS -X POST -H "Content-Type: application/json" \
        --data-binary "@$WORK/config.json" \
        "http://localhost:${HTTP_PORT}/api/v1/config/import"
    echo
    echo "Config importée."
    exit 0
fi

read -r -p "Restauration COMPLÈTE : la base actuelle sera écrasée. Confirmer ? (oui) " answer
[[ "$answer" == "oui" ]] || { echo "Annulé."; exit 1; }

echo "Arrêt de l'app (la DB reste up)…"
docker compose stop app
echo "pg_restore…"
docker compose exec -T db pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    --clean --if-exists --no-owner < "$WORK/db.dump"
echo "Redémarrage…"
docker compose start app
echo "Restauration terminée."
