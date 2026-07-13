#!/usr/bin/env bash
# ============================================================================
# reset.sh — remise à zéro TOTALE (§9.5.11) : conteneurs, DB, data/, sandbox.
# Ne touche JAMAIS aux bibliothèques réelles sous ${MEDIA_ROOT} (hors sandbox).
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE=".env"
[[ -f "$ENV_FILE" ]] && set -a && source "$ENV_FILE" && set +a
SANDBOX_ROOT="${SANDBOX_ROOT:-/srv/storage/_transcoder_sandbox}"

echo "Cette commande va :"
echo "  1. arrêter et supprimer les conteneurs (docker compose down)"
echo "  2. purger ./data/ (PostgreSQL, logs, corbeille, tmp, sorties dry-run)"
echo "  3. purger le bac à sable $SANDBOX_ROOT"
echo "Les bibliothèques réelles ne sont PAS touchées."
read -r -p "Confirmer la remise à zéro totale ? (taper 'RESET') " answer
[[ "$answer" == "RESET" ]] || { echo "Annulé."; exit 1; }

docker compose down --remove-orphans || true

if [[ -d data ]]; then
    # data/postgres appartient à l'uid postgres du conteneur : passer par docker
    docker run --rm -v "$(pwd)/data:/purge" alpine sh -c 'rm -rf /purge/* /purge/.[!.]*' \
        || rm -rf data/* 2>/dev/null || {
            echo "Purge de data/ impossible sans droits : sudo rm -rf data/*" >&2
            exit 1
        }
fi
mkdir -p data/postgres data/logs data/tmp data/trash data/out

if [[ -d "$SANDBOX_ROOT" ]]; then
    rm -rf "${SANDBOX_ROOT:?}"/*
    echo "Sandbox purgée : $SANDBOX_ROOT"
fi

echo "Remise à zéro terminée. Relancer avec : docker compose up -d --build"
