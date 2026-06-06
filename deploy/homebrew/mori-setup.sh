#!/usr/bin/env bash
# mori-setup — first-time setup wizard (run after `brew install mori`)
#
# Prompts for your LLM provider, generates a server API key,
# writes ~/.config/mori/env, starts the service, and optionally
# wires the Claude Code plugin.
set -euo pipefail

# ── Helpers ───────────────────────────────────────────────────────────────────

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; RESET='\033[0m'
ok()   { printf "${GREEN}✓${RESET}  %s\n" "$*"; }
warn() { printf "${YELLOW}⚠${RESET}  %s\n" "$*"; }
fail() { printf "${RED}✗${RESET}  %s\n" "$*"; }
ask()  { printf "${BOLD}%s${RESET}" "$*"; }

# Homebrew prefix (formula files live here)
BREW_PREFIX="$(brew --prefix)"
MORI_PREFIX="${BREW_PREFIX}/opt/mori"
MORI_SHARE="${BREW_PREFIX}/share/mori"

# Config and data locations
MORI_CONFIG_DIR="${HOME}/.config/mori"
MORI_ENV="${MORI_CONFIG_DIR}/env"
MORI_DATA="${HOME}/.local/share/mori"

SERVER_URL="http://localhost:8968"

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
printf "${BOLD}"
echo "╔══════════════════════════════════════════════════════╗"
echo "║           Mori — setup wizard                        ║"
echo "║  Your own memory server. Your own data. AGPL-3.0.    ║"
echo "╚══════════════════════════════════════════════════════╝"
printf "${RESET}"
echo ""
echo "This wizard configures your LLM provider, generates an API key,"
echo "starts the server, and optionally wires the Claude Code plugin."
echo ""

# ── LLM provider URL ─────────────────────────────────────────────────────────
echo "─────────────────────────────────────────────────────────"
echo "LLM provider base URL (OpenAI-compatible endpoint)"
echo ""
echo "  Local (Ollama, recommended for privacy):"
echo "    http://localhost:11434/v1"
echo "  Cloud providers (need an API key below):"
echo "    https://api.novita.ai/v3/openai      (Novita — kimi-k2.6 etc.)"
echo "    https://api.deepinfra.com/v1/openai  (DeepInfra)"
echo "    https://api.openai.com/v1            (OpenAI)"
echo ""
ask "Base URL [http://localhost:11434/v1]: "
read -r MORI_BASE_URL
MORI_BASE_URL="${MORI_BASE_URL:-http://localhost:11434/v1}"

# ── API key ───────────────────────────────────────────────────────────────────
echo ""
ask "Provider API key (leave blank for local/no-auth): "
read -rs MORI_API_KEY
echo ""

# ── Model ─────────────────────────────────────────────────────────────────────
echo ""
ask "Model name [moonshotai/kimi-k2.6]: "
read -r MORI_ADVISOR_MODEL
MORI_ADVISOR_MODEL="${MORI_ADVISOR_MODEL:-moonshotai/kimi-k2.6}"

# ── Server API key ────────────────────────────────────────────────────────────
HOSTNAME_SHORT="$(hostname -s 2>/dev/null || echo "local")"
SERVER_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
MORI_API_KEYS="${HOSTNAME_SHORT}:${SERVER_SECRET}"

# ── Write config ──────────────────────────────────────────────────────────────
mkdir -p "$MORI_CONFIG_DIR"
mkdir -p "$MORI_DATA"

cat > "$MORI_ENV" << EOF
# Mori configuration — written by mori-setup $(date -u +"%Y-%m-%dT%H:%M:%SZ")
MORI_PROVIDER_MODE=direct
MORI_BASE_URL=${MORI_BASE_URL}
MORI_API_KEY=${MORI_API_KEY}
MORI_ADVISOR_MODEL=${MORI_ADVISOR_MODEL}
MORI_TRUSTED_DREAMERS=${HOSTNAME_SHORT}
MORI_API_KEYS=${MORI_API_KEYS}
MORI_ADVISOR_DATA=${MORI_DATA}
MORI_DREAM_INTERVAL=60
MORI_MCP_SERVER_NAME=mori
EOF
chmod 600 "$MORI_ENV"
ok "Config written to ${MORI_ENV}"

# ── Linux: linger check ───────────────────────────────────────────────────────
if [ "$(uname)" = "Linux" ]; then
  echo ""
  LINGER_STATUS="$(loginctl show-user "$USER" --property=Linger 2>/dev/null || echo "Linger=unknown")"
  if [ "$LINGER_STATUS" != "Linger=yes" ]; then
    warn "Linux: linger is not enabled. The service will stop when you log out."
    echo ""
    ask "  Enable linger now so the service persists after logout? [Y/n]: "
    read -r ENABLE_LINGER
    if [[ ! "${ENABLE_LINGER:-Y}" =~ ^[Nn]$ ]]; then
      if loginctl enable-linger "$USER" 2>/dev/null; then
        ok "Linger enabled for ${USER}"
      else
        warn "Could not enable linger automatically."
        echo "     Run manually: loginctl enable-linger ${USER}"
      fi
    fi
  else
    ok "Linger already enabled"
  fi
fi

# ── Start service ─────────────────────────────────────────────────────────────
echo ""
echo "Starting the mori server..."
if brew services list 2>/dev/null | grep -q "^mori.*started"; then
  brew services restart mori >/dev/null 2>&1 && ok "Service restarted" || warn "Restart failed — check: brew services list"
else
  brew services start mori >/dev/null 2>&1 && ok "Service started" || warn "Start failed — check: brew services list"
fi

# ── Health check ──────────────────────────────────────────────────────────────
echo ""
echo "Waiting for server to be ready..."
READY=false
for i in 1 2 3 4 5; do
  sleep $((i * 2))
  if curl -sf --max-time 3 "${SERVER_URL}/health" >/dev/null 2>&1; then
    READY=true
    break
  fi
done

if [ "$READY" = "true" ]; then
  ok "Server healthy at ${SERVER_URL}"
else
  warn "Server not yet responding at ${SERVER_URL}"
  echo "     It may still be starting. Check with:"
  echo "       curl ${SERVER_URL}/health"
  echo "       brew services list"
fi

# ── Plugin wiring ─────────────────────────────────────────────────────────────
echo ""
echo "─────────────────────────────────────────────────────────"
echo "Claude Code / Cursor plugin"
echo ""
ask "Auto-wire the mori plugin for Claude Code? [Y/n]: "
read -r WIRE_PLUGIN

if [[ ! "${WIRE_PLUGIN:-Y}" =~ ^[Nn]$ ]]; then
  INSTALLER="${MORI_SHARE}/scripts/legacy/install-mori-claude.sh"
  if [ -f "$INSTALLER" ]; then
    bash "$INSTALLER" \
      --url "${SERVER_URL}" \
      --api-key "${SERVER_SECRET}" \
      --no-deploy 2>/dev/null \
      && ok "Claude Code plugin wired (reload Claude Code to activate)" \
      || warn "Installer encountered an issue — wire manually using the details below"
  else
    warn "Installer not found at ${INSTALLER}"
    echo "     Wire manually using the plugin details below."
  fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
printf "${BOLD}"
echo "╔══════════════════════════════════════════════════════╗"
echo "║  Setup complete 🌲                                   ║"
echo "╚══════════════════════════════════════════════════════╝"
printf "${RESET}"
echo ""
echo "  Server URL : ${SERVER_URL}"
echo "  API key    : ${MORI_API_KEYS}"
echo ""
echo "  In Claude Code / Cursor / Antigravity:"
echo "    Server URL → ${SERVER_URL}"
echo "    API key    → ${MORI_API_KEYS}"
echo ""
echo "  Or via plugin marketplace (Claude Code):"
echo "    /plugin marketplace add fjwood69/mori"
echo "    /plugin install mori@mori"
echo ""
echo "  Manage the server:"
echo "    brew services stop mori"
echo "    brew services restart mori"
echo "    tail -f $(brew --prefix)/var/log/mori.log"
echo ""
echo "  Re-run this wizard anytime: mori-setup"
echo ""
