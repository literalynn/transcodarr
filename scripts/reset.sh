#!/usr/bin/env bash
# ============================================================================
# reset.sh — FULL reset: containers, database, data/, sandbox.
# NEVER touches your real libraries under ${MEDIA_ROOT} (outside the sandbox).
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE=".env"
[[ -f "$ENV_FILE" ]] && set -a && source "$ENV_FILE" && set +a
SANDBOX_ROOT="${SANDBOX_ROOT:-/srv/storage/_transcoder_sandbox}"

echo "This will:"
echo "  1. stop and remove the containers (docker compose down)"
echo "  2. wipe ./data/ (PostgreSQL, logs, trash, tmp, dry-run outputs)"
echo "  3. wipe the sandbox $SANDBOX_ROOT"
echo "Your real libraries are NOT touched."
read -r -p "Confirm full reset? (type 'RESET') " answer
[[ "$answer" == "RESET" ]] || { echo "Cancelled."; exit 1; }

docker compose down --remove-orphans || true

if [[ -d data ]]; then
    # data/postgres is owned by the container's postgres uid: go through docker
    docker run --rm -v "$(pwd)/data:/purge" alpine sh -c 'rm -rf /purge/* /purge/.[!.]*' \
        || rm -rf data/* 2>/dev/null || {
            echo "Cannot wipe data/ without privileges: sudo rm -rf data/*" >&2
            exit 1
        }
fi
mkdir -p data/postgres data/logs data/tmp data/trash data/out

if [[ -d "$SANDBOX_ROOT" ]]; then
    rm -rf "${SANDBOX_ROOT:?}"/*
    echo "Sandbox wiped: $SANDBOX_ROOT"
fi

echo "Reset complete. Start again with: docker compose up -d --build"
