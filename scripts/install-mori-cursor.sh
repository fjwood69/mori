#!/usr/bin/env bash
# Linux/macOS installer script for Mori — Cursor bridge
# Run from the root of the mori repository.
#
# Installs MCP config for Cursor 2.4+, event capture hooks, and
# Mori slash commands. Works whether or not Claude Code is installed
# — Cursor loads hooks from ~/.claude/settings.json and skills from
# ~/.claude/skills/ natively.

echo "NOTE: The plugin package plugins/mori/ now provides the MCP connection and skills for Cursor; platform-specific hooks are a fast-follow. See plugins/mori/README.md for the recommended install path." >&2

set -euo pipefail

MORI_URL="http://localhost:8968"
API_KEY=""
CLIENT_NAME=$(hostname 2>/dev/null || echo "cursor")
FORCE=false
DOCTOR=false
UPGRADE_SKILLS=false
SKILLS_ONLY=false

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MORI_REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALL_PY="${SCRIPT_DIR}/mori_cursor_install.py"

show_help() {
  echo "Usage: install-mori-cursor.sh [options]"
  echo ""
  echo "Options:"
  echo "  --url <url>           Mori server base URL (default: http://localhost:8968)"
  echo "  --api-key <key>       Optional API key for event ingestion auth"
  echo "  --client <name>       Client name for event tagging (default: hostname)"
  echo "  -f, --force           Skip health check prompt on failure"
  echo "  --doctor              Run connectivity/config checks only (no changes)"
  echo "  --upgrade-skills      Refresh mori skills from repo skills/ subdirectories"
  echo "  -h, --help            Show this help"
  echo ""
  echo "Post-install: Reload Cursor window (Developer: Reload Window)."
  echo "Shared memory is on the Mori server — never a local memories.db on your laptop."
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --url) MORI_URL="$2"; shift 2 ;;
    --api-key) API_KEY="$2"; shift 2 ;;
    --client) CLIENT_NAME="$2"; shift 2 ;;
    -f|--force) FORCE=true; shift ;;
    --doctor) DOCTOR=true; shift ;;
    --upgrade-skills) UPGRADE_SKILLS=true; shift ;;
    -h|--help) show_help; exit 0 ;;
    *) echo "Unknown option: $1" >&2; show_help; exit 1 ;;
  esac
done

if [ "$DOCTOR" = true ]; then
  exec python3 "$INSTALL_PY" doctor --url "${MORI_URL}" --client "${CLIENT_NAME}"
fi

echo "--- Mori — Cursor Bridge Setup Wizard ---"

if [[ "$(uname)" == "Darwin" ]]; then
  CURSOR_DIR="$HOME/Library/Application Support/Cursor"
  MCP_CONFIG="$HOME/Library/Application Support/Cursor/mcp.json"
else
  CURSOR_DIR="$HOME/.cursor"
  MCP_CONFIG="$HOME/.cursor/mcp.json"
fi

if [ ! -d "$CURSOR_DIR" ]; then
  echo "Warning: Cursor config directory not found at $CURSOR_DIR."
  echo "Make sure Cursor is installed and has been launched at least once."
  if [ "$FORCE" = "false" ]; then
    read -p "Proceed anyway? (y/N) " confirm
    if [[ ! "$confirm" =~ ^[yY] ]]; then
      echo "Installation aborted."
      exit 1
    fi
  fi
fi

MORI_URL="${MORI_URL%/}"

if [[ ! "$MORI_URL" =~ ^https?:// ]]; then
  echo "Error: Invalid Mori URL. Must start with http:// or https://" >&2
  exit 1
fi

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
echo "Setting up Mori — Cursor Bridge..."

MCP_OK=0
HOOKS_OK=0
SKILLS_OK=0

# ---- Step 1: MCP config (required) ----
echo "[1/3] Configuring MCP server..."
if python3 "$INSTALL_PY" merge-mcp --mcp-path "$MCP_CONFIG" --url "$MORI_URL"; then
  echo "  Updated $MCP_CONFIG"
  MCP_OK=1
else
  echo "  Error: failed to write MCP config" >&2
fi

# ---- Step 2: Event capture hooks + permissions ----
echo "[2/3] Setting up event capture hooks..."
CLAUDEDIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
mkdir -p "$CLAUDEDIR"
HOOKS_FILE="$CLAUDEDIR/settings.json"
SHIPPER_SRC="${SCRIPT_DIR}/mori-ship-event.sh"
SHIPPER_DST="${CLAUDEDIR}/mori-ship-event.sh"
BRIEF_SRC="${SCRIPT_DIR}/mori-post-compact-brief.sh"
BRIEF_DST="${CLAUDEDIR}/mori-post-compact-brief.sh"

if [ -f "$SHIPPER_SRC" ]; then
  cp "$SHIPPER_SRC" "$SHIPPER_DST" && chmod +x "$SHIPPER_DST"
  echo "  Deployed mori-ship-event.sh to ${CLAUDEDIR}"
else
  echo "  Warning: mori-ship-event.sh not found alongside installer" >&2
fi

if [ -f "$BRIEF_SRC" ]; then
  cp "$BRIEF_SRC" "$BRIEF_DST" && chmod +x "$BRIEF_DST"
  echo "  Deployed mori-post-compact-brief.sh to ${CLAUDEDIR}"
else
  echo "  Warning: mori-post-compact-brief.sh not found alongside installer — PostCompact hook will not work." >&2
fi

if python3 "$INSTALL_PY" merge-settings \
  --settings-path "$HOOKS_FILE" \
  --shipper "$SHIPPER_DST" \
  --url "$MORI_URL" \
  --client "$CLIENT_NAME" \
  --api-key "$API_KEY"; then
  echo "  Merged Mori hooks + MCP permissions into $HOOKS_FILE"
  HOOKS_OK=1
else
  echo "  Error: failed to merge settings (see above)" >&2
fi

# ---- Step 3: Deploy skills ----
echo "[3/3] Deploying skills..."
SKILLS_DIR="$CLAUDEDIR/skills"
SOURCE_SKILLS_DIR="$MORI_REPO_ROOT/skills"
UPGRADE_FLAG=""
[ "$UPGRADE_SKILLS" = true ] && UPGRADE_FLAG="--upgrade"

if python3 "$INSTALL_PY" deploy-skills \
  --source "$SOURCE_SKILLS_DIR" \
  --dest "$SKILLS_DIR" \
  $UPGRADE_FLAG; then
  SKILLS_OK=1
else
  echo "  Warning: skill deploy had issues" >&2
fi

echo ""
if [ "$MCP_OK" -eq 1 ]; then
  echo "Mori — Cursor Bridge installation complete!"
else
  echo "Mori — Cursor Bridge installation FAILED (MCP config not written)." >&2
fi

echo ""
echo "--- Post-Install Steps ---"
echo ""
echo "1. Reload Cursor window: Command Palette → 'Developer: Reload Window'"
echo "2. Enable Third-party skills: Settings → Rules, Skills, Subagents → Enable third-party skills"
echo "3. Confirm MCP: Settings → MCP → 'mori' connected"
echo "4. Verify: ./scripts/install-mori-cursor.sh --doctor --url \"$MORI_URL\""
echo "5. In Agent chat: /brief — loads shared memory from the server (not local disk)"
echo ""
echo "No Claude Code required — Mori creates ~/.claude/settings.json and"
echo "~/.claude/skills/ for you if they don't already exist."
echo ""
echo "Hook failures are logged to: ${TMPDIR:-/tmp}/mori-hook.log"
echo ""

if [ "$MCP_OK" -ne 1 ]; then
  exit 1
fi
exit 0
