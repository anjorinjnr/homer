#!/usr/bin/env bash
# backup.sh — Nightly backup of Homer's sensitive files to Cloudflare R2
#
# Files backed up:
#   - context/familycontext.md
#   - ~/.nanobot/workspace/MEMORY.md (nanobot session memory)
#   - ~/.nanobot/workspace/HISTORY.md (nanobot interaction history)
#
# Requires: aws CLI configured with R2 endpoint (s3-compatible)
# Schedule: run nightly via cron or systemd timer (see runbook.md)
# Configure: set variables in secrets/.env before running

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# Load env
if [[ -f "$REPO_ROOT/secrets/.env" ]]; then
    # shellcheck disable=SC1091
    set -o allexport
    source "$REPO_ROOT/secrets/.env"
    set +o allexport
else
    echo "ERROR: secrets/.env not found. Run setup first." >&2
    exit 1
fi

: "${R2_ACCOUNT_ID:?R2_ACCOUNT_ID not set in .env}"
: "${R2_ACCESS_KEY_ID:?R2_ACCESS_KEY_ID not set in .env}"
: "${R2_SECRET_ACCESS_KEY:?R2_SECRET_ACCESS_KEY not set in .env}"
: "${R2_BUCKET_NAME:?R2_BUCKET_NAME not set in .env}"

R2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H-%M-%SZ")
BACKUP_PREFIX="backups/${TIMESTAMP}"

log() { echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] $*"; }

upload() {
    local src="$1"
    local dest_key="$2"
    if [[ -f "$src" ]]; then
        AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" \
        AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
        aws s3 cp "$src" "s3://${R2_BUCKET_NAME}/${dest_key}" \
            --endpoint-url "$R2_ENDPOINT" \
            --quiet
        log "Backed up: $src → $dest_key"
    else
        log "SKIP (not found): $src"
    fi
}

log "=== Homer backup starting ==="

# familycontext.md — primary household knowledge base
upload "$REPO_ROOT/context/familycontext.md" \
    "${BACKUP_PREFIX}/familycontext.md"

# nanobot session memory (if workspace is configured)
NANOBOT_WS="${HOMER_WORKSPACE:-$HOME/.nanobot/workspace}"
upload "$NANOBOT_WS/MEMORY.md"  "${BACKUP_PREFIX}/MEMORY.md"
upload "$NANOBOT_WS/HISTORY.md" "${BACKUP_PREFIX}/HISTORY.md"

log "=== Homer backup complete ==="
