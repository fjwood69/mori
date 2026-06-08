#!/usr/bin/env bash
# Install Mori for Cursor via the unified plugin package (recommended path).
#
# Copies plugins/mori → ~/.cursor/plugins/local/mori, merges MCP config,
# wires native hooks (~/.cursor/hooks.json), and optionally --parity compat
# hooks (~/.claude/settings.json PreCompact/PostCompact + permissions).
#
# Usage:
#   ./scripts/install-mori-cursor-plugin.sh --url http://SERVER:8968 [--api-key KEY] [--parity] [--force]
#   ./scripts/install-mori-cursor-plugin.sh --doctor [--parity] --url http://SERVER:8968

set -euo pipefail

MORI_URL="http://localhost:8968"
API_KEY=""
CLIENT_NAME=$(hostname 2>/dev/null || echo "cursor")
FORCE=false
DOCTOR=false
PARITY=false
UPGRADE=false

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MORI_REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALL_PY="${SCRIPT_DIR}/mori_cursor_install.py"
PLUGIN_SRC="${MORI_REPO_ROOT}/plugins/mori"
PLUGIN_DST="${HOME}/.cursor/plugins/local/mori"
CLAUDEDIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

show_help() {
  cat <<'EOF'
Usage: install-mori-cursor-plugin.sh [options]

Install Mori for Cursor using plugins/mori/ (recommended).

Options:
  --url <url>        Mori server base URL (default: http://localhost:8968)
  --api-key <key>    Optional API key (bare secret, not name:secret)
  --client <name>    Client tag for events (default: hostname)
  --parity           True up to legacy hook depth (native + compat layers)
  --upgrade          Re-copy plugin from repo even if destination exists
  --doctor           Capability-matrix doctor (no changes)
  -f, --force        Skip health-check prompt on failure
  -h, --help         Show this help

Post-install: Reload Cursor window. Enable third-party skills.

Legacy fallback: ./scripts/install-mori-cursor.sh
EOF
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --url) MORI_URL="$2"; shift 2 ;;
    --api-key) API_KEY="$2"; shift 2 ;;
    --client) CLIENT_NAME="$2"; shift 2 ;;
    --parity) PARITY=true; shift ;;
    --upgrade) UPGRADE=true; shift ;;
    -f|--force) FORCE=true; shift ;;
    --doctor) DOCTOR=true; shift ;;
    -h|--help) show_help; exit 0 ;;
    *) echo "Unknown option: $1" >&2; show_help; exit 1 ;;
  esac
done

MORI_URL="${MORI_URL%/}"

if [[ ! "$MORI_URL" =~ ^https?:// ]]; then
  echo "Error: Invalid Mori URL. Must start with http:// or https://" >&2
  exit 1
fi

PARITY_FLAG=""
[ "$PARITY" = true ] && PARITY_FLAG="--parity"

if [ "$DOCTOR" = true ]; then
  exec python3 "$INSTALL_PY" doctor-plugin --url "${MORI_URL}" --client "${CLIENT_NAME}" ${PARITY_FLAG}
fi

if [ ! -d "$PLUGIN_SRC" ]; then
  echo "Error: plugin source not found: $PLUGIN_SRC" >&2
  exit 1
fi

echo "--- Mori — Cursor Plugin Installer ---"
echo ""

CONNECTED=false
if curl -sf --max-time 5 "$MORI_URL/health" >/dev/null 2>&1; then
  echo "OK  Server health: $MORI_URL/health"
  CONNECTED=true
else
  echo "WARN  Could not reach $MORI_URL/health"
fi

if [ "$CONNECTED" = false ] && [ "$FORCE" = false ]; then
  read -p "Health check failed. Proceed anyway? (y/N) " confirm
  if [[ ! "$confirm" =~ ^[yY] ]]; then
    echo "Installation aborted."
    exit 1
  fi
fi

# ---- Step 1: Deploy plugin package ----
echo ""
echo "[1/4] Deploying plugin to $PLUGIN_DST ..."
mkdir -p "$(dirname "$PLUGIN_DST")"
if [ -d "$PLUGIN_DST" ] && [ "$UPGRADE" = false ]; then
  echo "  Plugin directory exists — updating MCP/hooks only (use --upgrade to refresh files)"
else
  rm -rf "$PLUGIN_DST"
  cp -a "$PLUGIN_SRC" "$PLUGIN_DST"
  echo "  Copied $PLUGIN_SRC → $PLUGIN_DST"
fi

# ---- Step 2: MCP in plugin mcp.json ----
echo "[2/4] Configuring plugin MCP ..."
MCP_PATH="$PLUGIN_DST/mcp.json"
PY_ARGS=(merge-plugin-mcp --mcp-path "$MCP_PATH" --url "$MORI_URL")
[ -n "$API_KEY" ] && PY_ARGS+=(--api-key "$API_KEY")
python3 "$INSTALL_PY" "${PY_ARGS[@]}"
echo "  Updated $MCP_PATH"

# ---- Step 3: Native Cursor hooks ----
echo "[3/4] Wiring native hooks (~/.cursor/hooks.json) ..."
HOOK_INSTALLER="$PLUGIN_DST/scripts/install-hooks-cursor.mjs"
if [ ! -f "$HOOK_INSTALLER" ]; then
  echo "  Error: $HOOK_INSTALLER not found" >&2
  exit 1
fi
HOOK_ARGS=(--url "$MORI_URL")
[ -n "$API_KEY" ] && HOOK_ARGS+=(--api-key "$API_KEY")
[ "$PARITY" = true ] && HOOK_ARGS+=(--parity)
node "$HOOK_INSTALLER" "${HOOK_ARGS[@]}"

# ---- Step 4: Compat layer (--parity) ----
if [ "$PARITY" = true ]; then
  echo "[4/4] Wiring compat hooks (PreCompact/PostCompact) ..."
  mkdir -p "$CLAUDEDIR"
  SHIPPER_SRC="${SCRIPT_DIR}/mori-ship-event.sh"
  SHIPPER_DST="${CLAUDEDIR}/mori-ship-event.sh"
  BRIEF_SRC="${SCRIPT_DIR}/mori-post-compact-brief.sh"
  BRIEF_DST="${CLAUDEDIR}/mori-post-compact-brief.sh"
  cp "$SHIPPER_SRC" "$SHIPPER_DST" && chmod +x "$SHIPPER_DST"
  cp "$BRIEF_SRC" "$BRIEF_DST" && chmod +x "$BRIEF_DST"
  echo "  Deployed shippers to $CLAUDEDIR"
  python3 "$INSTALL_PY" merge-settings-compat \
    --settings-path "$CLAUDEDIR/settings.json" \
    --shipper "$SHIPPER_DST" \
    --url "$MORI_URL" \
    --client "$CLIENT_NAME" \
    --api-key "$API_KEY"
  echo "  Merged PreCompact/PostCompact + permissions into $CLAUDEDIR/settings.json"
else
  echo "[4/4] Skipping compat layer (use --parity for legacy hook depth)"
fi

echo ""
echo "Mori Cursor plugin install complete."
echo ""
echo "--- Post-install ---"
echo "1. Reload Cursor: Developer → Reload Window"
echo "2. Enable third-party skills (Settings → Rules, Skills, Subagents)"
echo "3. Doctor: ./scripts/install-mori-cursor-plugin.sh --doctor --url \"$MORI_URL\" ${PARITY_FLAG}"
echo "4. Agent chat: /brief"
echo ""
if [ "$PARITY" = false ]; then
  echo "Tip: run with --parity to match legacy hook capabilities (PreCompact/PostCompact)."
fi
