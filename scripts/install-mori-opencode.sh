#!/usr/bin/env bash
# Linux/macOS installer script for Mori — OpenCode bridge
# Run from the root of the mori repository.
#
# Installs the Mori TypeScript plugin for OpenCode, wires the MCP server
# config, and deploys Mori slash-command skills.

set -euo pipefail

MORI_URL="http://localhost:8968"
API_KEY=""
CLIENT_NAME=$(hostname 2>/dev/null || echo "opencode")
FORCE=false
DOCTOR=false
UPGRADE_SKILLS=false
PROJECT_SCOPED=false

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MORI_REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALL_PY="${SCRIPT_DIR}/mori_opencode_install.py"
PLUGIN_SRC="${MORI_REPO_ROOT}/plugins/mori/opencode"

show_help() {
  echo "Usage: install-mori-opencode.sh [options]"
  echo ""
  echo "Options:"
  echo "  --url <url>           Mori server base URL (default: http://localhost:8968)"
  echo "  --api-key <key>       Bare API key from MORI_API_KEYS (not name:secret)"
  echo "  --client <name>       Client name for event tagging (default: hostname)"
  echo "  --project             Install project-scoped (.opencode/) instead of global"
  echo "  -f, --force           Skip health check prompt on failure"
  echo "  --doctor              Run connectivity/config checks only (no changes)"
  echo "  --upgrade-skills      Refresh mori skills from repo skills/ directory"
  echo "  -h, --help            Show this help"
  echo ""
  echo "Post-install: Restart OpenCode to activate the plugin."
  echo "Shared memory lives on the Mori server, not your local disk."
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --url)            MORI_URL="$2"; shift 2 ;;
    --api-key)        API_KEY="$2"; shift 2 ;;
    --client)         CLIENT_NAME="$2"; shift 2 ;;
    --project)        PROJECT_SCOPED=true; shift ;;
    -f|--force)       FORCE=true; shift ;;
    --doctor)         DOCTOR=true; shift ;;
    --upgrade-skills) UPGRADE_SKILLS=true; shift ;;
    -h|--help)        show_help; exit 0 ;;
    *)                echo "Unknown option: $1" >&2; show_help; exit 1 ;;
  esac
done

if [ "$DOCTOR" = true ]; then
  exec python3 "$INSTALL_PY" doctor --url "${MORI_URL}" --api-key "${API_KEY}"
fi

# ── Interactive wizard (headless if --url was passed on CLI) ──────────────────

HEADLESS=false
if [[ "$*" == *"--url"* ]] || [[ "${MORI_URL}" != "http://localhost:8968" ]]; then
  HEADLESS=true
fi

if [ "$HEADLESS" = "false" ]; then
  echo "--- Mori — OpenCode Bridge Setup Wizard ---"
  echo ""

  read -p "Enter Mori Server URL [http://localhost:8968]: " input_url
  [ -n "$input_url" ] && MORI_URL="$input_url"

  read -p "Enter Mori API Key (bare secret, Enter to skip): " input_key
  API_KEY="$input_key"

  default_client=$(hostname 2>/dev/null || echo "opencode")
  read -p "Enter Client Name [${default_client}]: " input_client
  [ -n "$input_client" ] && CLIENT_NAME="$input_client"

  echo ""
  read -p "Install globally or project-scoped? [G/p]: " install_scope
  case "${install_scope,,}" in
    p|project) PROJECT_SCOPED=true ;;
    *)          PROJECT_SCOPED=false ;;
  esac
fi

MORI_URL="${MORI_URL%/}"

if [[ ! "$MORI_URL" =~ ^https?:// ]]; then
  echo "Error: Invalid Mori URL. Must start with http:// or https://" >&2
  exit 1
fi

# ── Resolve install directory ─────────────────────────────────────────────────

if [ "$PROJECT_SCOPED" = true ]; then
  PLUGIN_DEST=".opencode/plugins/mori"
  CONFIG_PATH="opencode.json"
else
  CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
  PLUGIN_DEST="${CONFIG_HOME}/opencode/plugins/mori"
  CONFIG_PATH="${CONFIG_HOME}/opencode/opencode.json"
fi

# ── Health check ──────────────────────────────────────────────────────────────

echo ""
echo "Validating connection to Mori server at $MORI_URL..."
CONNECTED=false
if curl -sf --max-time 5 "$MORI_URL/health" >/dev/null 2>&1; then
  echo "Connection successful! Mori server health check: ok"
  CONNECTED=true
else
  echo "Warning: Could not connect to Mori server at $MORI_URL"
fi

if [ "$CONNECTED" = "false" ] && [ "$FORCE" = "false" ]; then
  read -p "Health check failed. Proceed anyway? (y/N) " confirm
  if [[ ! "$confirm" =~ ^[yY] ]]; then
    echo "Installation aborted."
    exit 1
  fi
fi

echo ""
echo "Setting up Mori — OpenCode Bridge..."

MCP_OK=0
PLUGIN_OK=0
SKILLS_OK=0

# ── Step 1: Copy plugin files ─────────────────────────────────────────────────

echo "[1/3] Installing plugin..."
if [ ! -d "$PLUGIN_SRC" ]; then
  echo "  Error: plugin source not found at ${PLUGIN_SRC}" >&2
  echo "  Run this script from the mori repo root." >&2
  exit 1
fi

mkdir -p "$PLUGIN_DEST"
cp -r "$PLUGIN_SRC/." "$PLUGIN_DEST/"

# Write mcp.json with actual values (env var placeholders not expanded by OpenCode on all versions)
cat > "$PLUGIN_DEST/mcp.json" << EOF
{
  "mcpServers": {
    "mori": {
      "type": "remote",
      "url": "${MORI_URL}/mcp",
      "headers": {
        "x-api-key": "${API_KEY:-YOUR-64-CHAR-BARE-SECRET}"
      }
    }
  }
}
EOF

PLUGIN_OK=1
echo "  Plugin installed to ${PLUGIN_DEST}"

# ── Step 2: Merge MCP server into opencode.json ───────────────────────────────

echo "[2/3] Configuring MCP server..."
if python3 "$INSTALL_PY" merge-config \
  --config-path "$CONFIG_PATH" \
  --url "$MORI_URL" \
  --api-key "$API_KEY"; then
  MCP_OK=1
else
  echo "  Error: failed to update ${CONFIG_PATH}" >&2
fi

# ── Step 3: Deploy skills ─────────────────────────────────────────────────────

echo "[3/3] Deploying skills..."
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
SKILLS_DEST="${CONFIG_HOME}/opencode/skills"
UPGRADE_FLAG=""
[ "$UPGRADE_SKILLS" = true ] && UPGRADE_FLAG="--upgrade"

if python3 "$INSTALL_PY" deploy-skills \
  --source "${MORI_REPO_ROOT}/skills" \
  --dest "$SKILLS_DEST" \
  $UPGRADE_FLAG; then
  SKILLS_OK=1
else
  echo "  Warning: skill deploy had issues (skills may still work via .claude/skills/)" >&2
  SKILLS_OK=1  # non-fatal; .claude/skills/ is also discovered by OpenCode
fi

# ── Set env vars in shell profile ─────────────────────────────────────────────

PROFILE=""
if   [ -f "$HOME/.zshrc" ];        then PROFILE="$HOME/.zshrc"
elif [ -f "$HOME/.bashrc" ];       then PROFILE="$HOME/.bashrc"
elif [ -f "$HOME/.bash_profile" ]; then PROFILE="$HOME/.bash_profile"
elif [ -f "$HOME/.profile" ];      then PROFILE="$HOME/.profile"
fi

if [ -n "$PROFILE" ] && [ -n "$API_KEY" ]; then
  if grep -q "MORI_SERVER_URL" "$PROFILE" 2>/dev/null; then
    echo "  Note: MORI_SERVER_URL already in ${PROFILE} — skipping (update manually if URL changed)"
  else
    cat >> "$PROFILE" << EOF

# Mori shared memory (added by install-mori-opencode.sh)
export MORI_SERVER_URL="${MORI_URL}"
export MORI_API_KEY="${API_KEY}"
EOF
    echo "  Env vars appended to ${PROFILE}"
  fi
elif [ -n "$PROFILE" ] && [ -z "$API_KEY" ]; then
  echo "  Note: no API key provided — set MORI_SERVER_URL and MORI_API_KEY in ${PROFILE} manually"
fi

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
if [ "$PLUGIN_OK" -eq 1 ] && [ "$MCP_OK" -eq 1 ]; then
  echo "Mori — OpenCode Bridge installation complete!"
else
  echo "Mori — OpenCode Bridge installation FAILED." >&2
fi

echo ""
echo "--- Post-Install Steps ---"
echo ""
echo "1. Restart OpenCode to activate the plugin"
echo "2. Confirm MCP:    opencode mcp list  (mori should appear)"
echo "3. Verify:         ./scripts/install-mori-opencode.sh --doctor --url \"$MORI_URL\""
echo "4. In a session:   /brief  — loads shared memory from the server"
echo ""
echo "Hook failures are logged to: ${TMPDIR:-/tmp}/mori-hook.log"
echo "Shared memory lives on the Mori server, not your local disk."
echo ""

if [ "$PLUGIN_OK" -ne 1 ] || [ "$MCP_OK" -ne 1 ]; then
  exit 1
fi
exit 0
