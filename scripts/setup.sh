#!/usr/bin/env bash
# setup.sh — Local dev setup for Homer (macOS)
#
# Run this once after cloning the repo to:
#   1. Install nanobot
#   2. Set up the nanobot workspace (symlinks familycontext.md in)
#   3. Generate nanobot config.json from template + .env
#   4. Set correct permissions on secrets/
#
# After running this: scan WhatsApp QR with `nanobot channels login`
# Then start Homer: `nanobot gateway`

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
NANOBOT_CONFIG_DIR="$HOME/.nanobot"
NANOBOT_WORKSPACE="$REPO_ROOT/context/.nanobot_workspace"

# ── Dev mode flag ──────────────────────────────────────────────────────────────
DEV_MODE=false
for arg in "$@"; do
    [[ "$arg" == "--dev" ]] && DEV_MODE=true
done

log() { echo "[setup] $*"; }
err() { echo "[setup] ERROR: $*" >&2; exit 1; }

# ── 1. Validate prerequisites ──────────────────────────────────────────────────
log "Checking prerequisites..."
command -v python3 >/dev/null || err "python3 not found. Install Python 3.12+."
command -v node   >/dev/null || err "node not found. Install Node.js 20+ (nanobot WhatsApp bridge requires it)."
command -v git    >/dev/null || err "git not found."
command -v gog    >/dev/null || err "gog (gogcli) not found. Homer's Gmail/Drive/Calendar tools shell out to it. Install: brew install steipete/tap/gogcli  OR  go install github.com/steipete/gogcli/cmd/gog@latest"

PYTHON_VER=$(python3 -c 'import sys; print(sys.version_info.minor)')
if [[ $PYTHON_VER -lt 12 ]]; then
    err "Python 3.12+ required. Found 3.${PYTHON_VER}."
fi

NODE_VER=$(node --version | sed 's/v//' | cut -d. -f1)
if [[ $NODE_VER -lt 20 ]]; then
    err "Node.js 20+ required. Found v${NODE_VER}."
fi

# ── 2. Install nanobot ─────────────────────────────────────────────────────────
# Using our fork (anjorinjnr/nanobot@homer-patches) which adds send_reasoning: false
# to HeartbeatConfig — silences plain-text heartbeat responses (upstream PR #1443).
# When PR #1443 merges into HKUDS/nanobot, switch back to: pip install nanobot-ai
NANOBOT_FORK="git+https://github.com/anjorinjnr/nanobot.git@homer-patches"
log "Installing nanobot from fork (homer-patches)..."
export PATH="$HOME/.local/bin:$PATH"
if command -v uv >/dev/null; then
    uv tool install "$NANOBOT_FORK" --force 2>/dev/null || uv pip install "$NANOBOT_FORK"
else
    pip3 install "$NANOBOT_FORK" --break-system-packages
fi
log "nanobot installed: $(nanobot --version 2>/dev/null || echo 'check: nanobot --version')"

# ── 3. Set up secrets ─────────────────────────────────────────────────────────
if [[ ! -f "$REPO_ROOT/secrets/.env" ]]; then
    log "Creating secrets/.env from template..."
    cp "$REPO_ROOT/secrets/.env.template" "$REPO_ROOT/secrets/.env"
    log "⚠  Fill in secrets/.env before starting Homer."
else
    log "secrets/.env already exists — skipping copy."
fi

if [[ ! -f "$REPO_ROOT/context/users.yaml" ]]; then
    log "Creating context/users.yaml from template..."
    cp "$REPO_ROOT/config/users.yaml.template" "$REPO_ROOT/context/users.yaml"
    log "⚠  Fill in context/users.yaml with your household users before starting Homer."
else
    log "context/users.yaml already exists — skipping copy."
fi

chmod 700 "$REPO_ROOT/secrets"
chmod 600 "$REPO_ROOT/secrets/.env" 2>/dev/null || true

# ── 4. Load env ───────────────────────────────────────────────────────────────
# shellcheck disable=SC1091
set -o allexport
source "$REPO_ROOT/secrets/.env"
set +o allexport

: "${ANTHROPIC_API_KEY:?Set ANTHROPIC_API_KEY in secrets/.env first}"
: "${PRIMARY_TELEGRAM_ID:?Set PRIMARY_TELEGRAM_ID in secrets/.env first}"

if $DEV_MODE; then
    : "${TELEGRAM_BOT_TOKEN_DEV:?Set TELEGRAM_BOT_TOKEN_DEV in secrets/.env for local dev}"
    TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN_DEV"
    log "DEV MODE: using TELEGRAM_BOT_TOKEN_DEV (separate bot from prod)"
else
    : "${TELEGRAM_BOT_TOKEN:?Set TELEGRAM_BOT_TOKEN in secrets/.env first}"
fi

# ── 5. Set up nanobot workspace ───────────────────────────────────────────────
log "Setting up nanobot workspace at $NANOBOT_WORKSPACE..."
mkdir -p "$NANOBOT_WORKSPACE"

# Build MEMORY.md from context files
log "Building MEMORY.md from context files..."
python3 "$REPO_ROOT/tools/build_context.py"

# ── 6. Generate nanobot config.json ──────────────────────────────────────────
log "Generating nanobot config.json..."
mkdir -p "$NANOBOT_CONFIG_DIR"
CONFIG_OUT="$NANOBOT_CONFIG_DIR/config.json"

# Substitute env vars into template and write config
HOMER_WORKSPACE_ESC=$(echo "$NANOBOT_WORKSPACE" | sed 's/\//\\\//g')

# Outbound scope guard: HOMER_OUTBOUND_SCOPE_GUARD=1 to enable.
if [[ "${HOMER_OUTBOUND_SCOPE_GUARD:-0}" == "1" ]]; then
    SCOPE_OUTBOUND_LOOKUP="outbound_scope_lookup:resolve"
else
    SCOPE_OUTBOUND_LOOKUP=""
fi

# Org mode: HOMER_ORG_MODE scopes each member's turn to their team(s) via the
# team_scope_context provider (see tools/team_scope_context.py). Accept the
# same truthy spellings the Python side honours (1/true/True) so the provider
# and the org persona never disagree about whether org mode is on.
if [[ "${HOMER_ORG_MODE:-0}" =~ ^(1|true|True|TRUE)$ ]]; then
    SCOPE_CONTEXT_PROVIDER="team_scope_context:render_team_context_for_sender"
else
    SCOPE_CONTEXT_PROVIDER=""
fi

sed \
    -e "s|\${ANTHROPIC_API_KEY}|${ANTHROPIC_API_KEY}|g" \
    -e "s|\${GEMINI_API_KEY}|${GEMINI_API_KEY:-}|g" \
    -e "s|\${HOMER_DEFAULT_MODEL}|${HOMER_DEFAULT_MODEL}|g" \
    -e "s|\${HOMER_DEFAULT_PROVIDER}|${HOMER_DEFAULT_PROVIDER}|g" \
    -e "s|\${HOMER_WORKSPACE}|${NANOBOT_WORKSPACE}|g" \
    -e "s|\${TELEGRAM_BOT_TOKEN}|${TELEGRAM_BOT_TOKEN}|g" \
    -e "s|\${PRIMARY_TELEGRAM_ID}|${PRIMARY_TELEGRAM_ID}|g" \
    -e "s|\${SECONDARY_TELEGRAM_ID}|${SECONDARY_TELEGRAM_ID:-}|g" \
    -e "s|\${PRIMARY_WHATSAPP}|${PRIMARY_WHATSAPP:-}|g" \
    -e "s|\${SECONDARY_WHATSAPP}|${SECONDARY_WHATSAPP:-}|g" \
    -e "s|\${VOICE_TRANSCRIPTION_PROVIDER}|${VOICE_TRANSCRIPTION_PROVIDER:-gemini}|g" \
    -e "s|\${VOICE_TRANSCRIPTION_MODEL}|${VOICE_TRANSCRIPTION_MODEL:-gemini-2.5-flash}|g" \
    -e "s|\${HOMER_TIMEZONE}|${HOMER_TIMEZONE:-America/New_York}|g" \
    -e "s|\${HOMER_VENV}|${REPO_ROOT}/.venv/bin/python|g" \
    -e "s|\${HOMER_TOOLS}|${REPO_ROOT}/tools|g" \
    -e "s|\${HOMER_SECRETS}|${REPO_ROOT}/secrets|g" \
    -e "s|\${HOMER_EMAIL_ADDRESS}|${HOMER_EMAIL_ADDRESS:-homer@example.com}|g" \
    -e "s|\${SCOPE_OUTBOUND_LOOKUP}|${SCOPE_OUTBOUND_LOOKUP:-}|g" \
    -e "s|\${SCOPE_CONTEXT_PROVIDER}|${SCOPE_CONTEXT_PROVIDER:-}|g" \
    "$REPO_ROOT/config/config.json.template" > "$CONFIG_OUT"

chmod 600 "$CONFIG_OUT"
log "nanobot config written to $CONFIG_OUT"

# Dev mode: disable WhatsApp (bridge not available locally; Telegram only)
if $DEV_MODE; then
    python3 -c "
import json
with open('$CONFIG_OUT') as f:
    c = json.load(f)
c.setdefault('channels', {}).setdefault('whatsapp', {})['enabled'] = False
with open('$CONFIG_OUT', 'w') as f:
    json.dump(c, f, indent=2)
"
    log "DEV MODE: WhatsApp disabled (Telegram only)"
fi

# ── 7. Verify git repo state ──────────────────────────────────────────────────
log "Verifying git state..."
cd "$REPO_ROOT"
if git status --short | grep "secrets/" | grep -qv "secrets/\.env\.template"; then
    err "DANGER: git is tracking files in secrets/. Check .gitignore immediately."
fi
if git status --short | grep -qE "context/[a-z_]+\.md"; then
    err "DANGER: git is tracking context .md files. Check .gitignore immediately."
fi
log "Git state looks clean — no secrets or context files tracked."

# ── 8. Install git hooks ──────────────────────────────────────────────────────
log "Installing pre-commit hooks..."
if [[ ! -x "$REPO_ROOT/.venv/bin/pre-commit" ]]; then
    log "Creating .venv and installing dependencies..."
    if command -v uv >/dev/null; then
        uv venv "$REPO_ROOT/.venv"
        uv pip install -r "$REPO_ROOT/requirements.txt" --python "$REPO_ROOT/.venv/bin/python" -q
    else
        python3 -m venv "$REPO_ROOT/.venv"
        "$REPO_ROOT/.venv/bin/pip" install -r "$REPO_ROOT/requirements.txt" -q
    fi
fi
"$REPO_ROOT/.venv/bin/pre-commit" install
log "Pre-commit hooks installed (pytest runs on every commit)."

# ── Done ──────────────────────────────────────────────────────────────────────
if $DEV_MODE; then
cat << EOF

✅ Homer dev setup complete (using dev bot token).

Start local dev:
  nanobot gateway

Note: prod bot on VPS is unaffected — dev and prod use separate bot tokens.
EOF
else
cat << EOF

✅ Homer setup complete.

Local dev:
  bash scripts/setup.sh --dev        # requires TELEGRAM_BOT_TOKEN_DEV in secrets/.env
  nanobot gateway

Deployment scripts (Dockerfile, server provisioning, build/release workflow)
live in your private deployment repo — this checkout ships only the agent
code + dev tooling.
EOF
fi
