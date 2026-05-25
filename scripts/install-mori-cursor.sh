#!/usr/bin/env bash
# Linux/macOS installer script for Mori — Cursor bridge
# Run from the root of the mori repository.
#
# Installs MCP config for Cursor 2.4+, event capture hooks, and
# Mori slash commands. Works whether or not Claude Code is installed
# — Cursor loads hooks from ~/.claude/settings.json and skills from
# ~/.claude/skills/ natively.

set -euo pipefail

MORI_URL="http://localhost:8968"
API_KEY=""
CLIENT_NAME=$(hostname 2>/dev/null || echo "cursor")
FORCE=false

URL_SPECIFIED=false
KEY_SPECIFIED=false
CLIENT_SPECIFIED=false

show_help() {
  echo "Usage: install-mori-cursor.sh [options]"
  echo "Options:"
  echo "  --url <url>        Mori server base URL (default: http://localhost:8968)"
  echo "  --api-key <key>    Optional API key for auth"
  echo "  --client <name>    Client name (default: hostname)"
  echo "  -f, --force        Skip health check"
  echo "  -h, --help         Show this help"
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --url) MORI_URL="$2"; URL_SPECIFIED=true; shift 2 ;;
    --api-key) API_KEY="$2"; KEY_SPECIFIED=true; shift 2 ;;
    --client) CLIENT_NAME="$2"; CLIENT_SPECIFIED=true; shift 2 ;;
    -f|--force) FORCE=true; shift ;;
    -h|--help) show_help; exit 0 ;;
    *) echo "Unknown option: $1" >&2; show_help; exit 1 ;;
  esac
done

echo "--- Mori — Cursor Bridge Setup Wizard ---"

# Detect platform
if [[ "$(uname)" == "Darwin" ]]; then
  CURSOR_DIR="$HOME/Library/Application Support/Cursor"
else
  CURSOR_DIR="$HOME/.cursor"
fi

# Check Cursor is installed
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

# URL
if [ "$URL_SPECIFIED" = "false" ]; then
  read -p "Enter Mori Server URL [http://localhost:8968] (e.g. http://192.168.0.100:8968): " input_url
  if [ -n "$input_url" ]; then
    MORI_URL="$input_url"
  fi
fi

# API key
if [ "$KEY_SPECIFIED" = "false" ]; then
  read -p "Enter Mori API Key (optional, press Enter to skip): " input_key
  API_KEY="$input_key"
fi

# Client name
if [ "$CLIENT_SPECIFIED" = "false" ]; then
  DEFAULT_CLIENT=$(hostname 2>/dev/null || echo "cursor")
  read -p "Enter Client Name [$DEFAULT_CLIENT]: " input_client
  if [ -n "$input_client" ]; then
    CLIENT_NAME="$input_client"
  fi
fi

# Strip trailing slash
MORI_URL="${MORI_URL%/}"

# Validate URL
if [[ ! "$MORI_URL" =~ ^https?:// ]]; then
  echo "Error: Invalid Mori URL. Must start with http:// or https://" >&2
  exit 1
fi

# Health check
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MORI_REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---- Step 1: MCP config for Cursor ----
echo "[1/3] Configuring MCP server..."

# macOS: Cursor config lives under ~/Library/Application Support
if [[ "$(uname)" == "Darwin" ]]; then
  MCP_CONFIG="$HOME/Library/Application Support/Cursor/mcp.json"
else
  MCP_CONFIG="$HOME/.cursor/mcp.json"
fi
mkdir -p "$(dirname "$MCP_CONFIG")"

AUTH_HEADER=""
[ -n "$API_KEY" ] && AUTH_HEADER="-H \"X-Api-Key: $API_KEY\" "

generate_mcp_config() {
  cat <<EOF
{
  "mcpServers": {
    "mori": {
      "type": "http",
      "url": "${MORI_URL}/mcp"
    }
  }
}
EOF
}

if [ -f "$MCP_CONFIG" ] && [ -s "$MCP_CONFIG" ]; then
  if command -v jq &>/dev/null; then
    TMP_FILE=$(mktemp)
    generate_mcp_config | jq -c '.' > "$TMP_FILE"
    mori_server=$(jq '.mcpServers.mori' "$TMP_FILE")

    jq --argjson mori "$mori_server" \
      '.mcpServers.mori = $mori' \
      "$MCP_CONFIG" > "$TMP_FILE.2" && mv "$TMP_FILE.2" "$MCP_CONFIG"
    rm -f "$TMP_FILE"
    echo "  Updated $MCP_CONFIG"
  else
    echo "  Warning: jq not found. Overwriting $MCP_CONFIG (existing config lost)."
    generate_mcp_config > "$MCP_CONFIG"
    echo "  Created $MCP_CONFIG"
  fi
else
  generate_mcp_config > "$MCP_CONFIG"
  echo "  Created $MCP_CONFIG"
fi

# ---- Step 2: Event capture hooks ----
echo "[2/3] Setting up event capture hooks..."

CLAUDEDIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
mkdir -p "$CLAUDEDIR"
HOOKS_FILE="$CLAUDEDIR/settings.json"

generate_hooks_json() {
  cat <<EOF
{
  "hooks": {
    "PostToolUse": [
      {
        "type": "command",
        "command": "curl -sf -X POST \"${MORI_URL}/api/events/raw?client=${CLIENT_NAME}\" ${AUTH_HEADER}-H \"Content-Type: application/json\" -d @- >/dev/null 2>&1; exit 0"
      }
    ],
    "PostToolUseFailure": [
      {
        "type": "command",
        "command": "curl -sf -X POST \"${MORI_URL}/api/events/raw?client=${CLIENT_NAME}\" ${AUTH_HEADER}-H \"Content-Type: application/json\" -d @- >/dev/null 2>&1; exit 0"
      }
    ],
    "UserPromptSubmit": [
      {
        "type": "command",
        "command": "curl -sf -X POST \"${MORI_URL}/api/events/raw?client=${CLIENT_NAME}\" ${AUTH_HEADER}-H \"Content-Type: application/json\" -d @- >/dev/null 2>&1; exit 0"
      }
    ],
    "Stop": [
      {
        "type": "command",
        "command": "curl -sf -X POST \"${MORI_URL}/api/events/raw?client=${CLIENT_NAME}\" ${AUTH_HEADER}-H \"Content-Type: application/json\" -d @- >/dev/null 2>&1; exit 0"
      }
    ],
    "PreCompact": [
      {
        "type": "command",
        "command": "curl -sf -X POST \"${MORI_URL}/api/precompact?client=${CLIENT_NAME}\" ${AUTH_HEADER}-H \"Content-Type: application/json\" -d @- >/dev/null 2>&1; exit 0"
      }
    ]
  }
}
EOF
}

if [ ! -f "$HOOKS_FILE" ]; then
  generate_hooks_json > "$HOOKS_FILE"
  echo "  Created $HOOKS_FILE with Mori event capture hooks"
elif ! grep -q "mori" "$HOOKS_FILE" 2>/dev/null && ! grep -q "8968" "$HOOKS_FILE" 2>/dev/null; then
  # File exists but no Mori hooks — merge them in
  if command -v jq &>/dev/null; then
    TMP_FILE=$(mktemp)
    generate_hooks_json | jq -c '.' > "$TMP_FILE"
    hooks_obj=$(jq '.hooks' "$TMP_FILE")

    jq --argjson hooks "$hooks_obj" \
      '.hooks = (.hooks // {}) + $hooks' \
      "$HOOKS_FILE" > "$TMP_FILE.2" && mv "$TMP_FILE.2" "$HOOKS_FILE"
    rm -f "$TMP_FILE"
    echo "  Merged Mori hooks into $HOOKS_FILE"
  else
    echo "  Warning: jq not found. Cannot merge hooks into $HOOKS_FILE"
    echo "  Please manually add hooks from examples/settings.json"
  fi
else
  echo "  Skipped — $HOOKS_FILE already has Mori hooks"
fi

# ---- Step 3: Deploy skills ----
echo "[3/3] Deploying skills..."

SKILLS_DIR="$CLAUDEDIR/skills"
SOURCE_SKILLS_DIR="$MORI_REPO_ROOT/skills"

if [ ! -d "$SOURCE_SKILLS_DIR" ]; then
  echo "  Warning: Source skills folder not found at $SOURCE_SKILLS_DIR — skipping."
else
  if [ -d "$SKILLS_DIR" ] && [ "$(ls -A "$SKILLS_DIR" 2>/dev/null)" ]; then
    echo "  Skipped — $SKILLS_DIR already has skills"
  else
    mkdir -p "$SKILLS_DIR"

    for file in "$SOURCE_SKILLS_DIR"/*.skill.md; do
      [ -e "$file" ] || continue
      filename=$(basename "$file")
      base_skill="${filename%.skill.md}"

      name=""
      desc=""
      content=""

      while IFS= read -r line || [ -n "$line" ]; do
        if [[ "$line" =~ ^-[[:space:]]+name:[[:space:]]*(.*)$ ]]; then
          name="${BASH_REMATCH[1]}"
        elif [[ "$line" =~ ^-[[:space:]]+description:[[:space:]]*(.*)$ ]]; then
          desc="${BASH_REMATCH[1]}"
        elif [[ -z "$line" && -z "$name" && -z "$desc" ]]; then
          :
        else
          content+="$line"$'\n'
        fi
      done < "$file"

      [ -z "$name" ] && name="$base_skill"
      name=$(echo "$name" | xargs)
      desc=$(echo "$desc" | xargs)

      skill_folder="$SKILLS_DIR/mori-$name"
      mkdir -p "$skill_folder"

      cat << SKILLEOF > "$skill_folder/SKILL.md"
---
name: mori-$name
description: "${desc//\"/\\\"}"
---

$(echo -n "$content" | sed -e 's/[[:space:]]*$//')
SKILLEOF
      echo "  Deployed skill: mori-$name"
    done
  fi
fi

echo ""
echo "Mori — Cursor Bridge installation complete!"
echo ""
echo "--- Post-Install Steps ---"
echo ""
echo "1. Enable Third-party skills in Cursor:"
echo "   Settings → Features → Third-party skills → Enable"
echo ""
echo "2. Restart Cursor for changes to take effect."
echo ""
echo "3. Verify:"
echo "   - Open Cursor Agent, type /brief — shared memories should load"
echo "   - Run: curl $MORI_URL/health"
echo ""
echo "No Claude Code required — Mori creates ~/.claude/settings.json and"
echo "~/.claude/skills/ for you if they don't already exist."
