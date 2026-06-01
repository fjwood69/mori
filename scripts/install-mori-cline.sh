#!/usr/bin/env bash
# Linux/macOS installer script for Mori — Cline bridge
# Run from the root of the mori repository.
#
# Installs env vars, plugin registration, MCP config, and skills
# for the Cline AI coding assistant.

set -euo pipefail

MORI_URL="http://localhost:8968"
API_KEY=""
CLIENT_NAME=$(hostname 2>/dev/null || echo "cline")
FORCE=false

URL_SPECIFIED=false
KEY_SPECIFIED=false
CLIENT_SPECIFIED=false

show_help() {
  echo "Usage: install-mori-cline.sh [options]"
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

echo "--- Mori — Cline Bridge Setup Wizard ---"

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
  DEFAULT_CLIENT=$(hostname 2>/dev/null || echo "cline")
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
echo "Setting up Mori — Cline Bridge..."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MORI_REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PLUGIN_PATH="$MORI_REPO_ROOT/extensions/mori-cline-plugin"

# 1. Set persistent environment variables
SHELL_PROFILE="${HOME}/.bashrc"
if [ -f "$HOME/.zshrc" ]; then
  SHELL_PROFILE="$HOME/.zshrc"
fi

echo ""
echo "[1/4] Setting environment variables..."

# Remove any existing Mori env var exports to avoid duplicates
if [ -f "$SHELL_PROFILE" ]; then
  grep -v "^export MORI_API_URL=" "$SHELL_PROFILE" | grep -v "^export MORI_API_KEY=" | grep -v "^export MORI_CLIENT=" > "${SHELL_PROFILE}.tmp" || true
  mv "${SHELL_PROFILE}.tmp" "$SHELL_PROFILE"
fi

{
  echo ""
  echo "# Mori — Cline bridge"
  echo "export MORI_API_URL=${MORI_URL}"
  if [ -n "$API_KEY" ]; then
    echo "export MORI_API_KEY=${API_KEY}"
  fi
  echo "export MORI_CLIENT=${CLIENT_NAME}"
} >> "$SHELL_PROFILE"

# Export for current session
export MORI_API_URL="$MORI_URL"
[ -n "$API_KEY" ] && export MORI_API_KEY="$API_KEY"
export MORI_CLIENT="$CLIENT_NAME"

echo "  Added to $SHELL_PROFILE"
echo "  MORI_API_URL=$MORI_URL"
echo "  MORI_CLIENT=$CLIENT_NAME"

# 2. Register the plugin
echo ""
echo "[2/4] Registering Cline plugin..."

if command -v cline &>/dev/null; then
  if [ -d "$PLUGIN_PATH" ]; then
    cline plugin install "$PLUGIN_PATH" 2>&1 || echo "  Warning: cline plugin install failed — you may need to register manually."
    echo "  Plugin registered via Cline CLI."
  else
    echo "  Warning: Plugin directory not found at $PLUGIN_PATH" >&2
  fi
else
  echo "  Cline CLI not found. Skipping plugin registration."
  echo "  To register manually, add to VS Code settings.json:"
  echo "  \"cline.agentRuntimePlugins\": [\"$PLUGIN_PATH/dist/mori-plugin.js\"]"
fi

# 3. Add MCP server to Cline config
echo ""
echo "[3/4] Configuring MCP server..."

CLINE_CONFIG_DIR="${HOME}/.cline"
mkdir -p "$CLINE_CONFIG_DIR"

CLINE_SETTINGS="$CLINE_CONFIG_DIR/settings.json"

auth_flag=""
[ -n "$API_KEY" ] && auth_flag=" --api-key \"${API_KEY}\""

CLAUDEDIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SHIPPER_SRC="${SCRIPT_DIR}/mori-ship-event.sh"
SHIPPER_DST="${CLAUDEDIR}/mori-ship-event.sh"

mkdir -p "$CLAUDEDIR"
if [ -f "$SHIPPER_SRC" ]; then
  cp "$SHIPPER_SRC" "$SHIPPER_DST" && chmod +x "$SHIPPER_DST"
  echo "  Deployed mori-ship-event.sh to ${CLAUDEDIR}"
else
  echo "  Warning: mori-ship-event.sh not found alongside installer — hooks will not work correctly."
fi

generate_mcp_hooks() {
  cat <<EOF
{
  "cline.mcpServers": {
    "mori": {
      "type": "http",
      "url": "${MORI_URL}/mcp"
    }
  },
  "hooks": {
    "PostToolUse": [
      {
        "type": "command",
        "command": "\"${SHIPPER_DST}\" --url \"${MORI_URL}\" --client \"${CLIENT_NAME}\"${auth_flag} --mode raw"
      }
    ],
    "PostToolUseFailure": [
      {
        "type": "command",
        "command": "\"${SHIPPER_DST}\" --url \"${MORI_URL}\" --client \"${CLIENT_NAME}\"${auth_flag} --mode raw"
      }
    ],
    "UserPromptSubmit": [
      {
        "type": "command",
        "command": "\"${SHIPPER_DST}\" --url \"${MORI_URL}\" --client \"${CLIENT_NAME}\"${auth_flag} --mode raw"
      }
    ],
    "Stop": [
      {
        "type": "command",
        "command": "\"${SHIPPER_DST}\" --url \"${MORI_URL}\" --client \"${CLIENT_NAME}\"${auth_flag} --mode raw"
      }
    ],
    "PreCompact": [
      {
        "type": "command",
        "command": "\"${SHIPPER_DST}\" --url \"${MORI_URL}\" --client \"${CLIENT_NAME}\"${auth_flag} --mode precompact"
      }
    ]
  }
}
EOF
}

if [ -f "$CLINE_SETTINGS" ] && [ -s "$CLINE_SETTINGS" ]; then
  if command -v jq &>/dev/null; then
    TMP_FILE=$(mktemp)
    generate_mcp_hooks | jq -c '.' > "$TMP_FILE"
    local mcp_server
    mcp_server=$(jq '."cline.mcpServers"' "$TMP_FILE")
    local hooks_obj
    hooks_obj=$(jq '.hooks' "$TMP_FILE")

    jq --argjson mori "$mcp_server" --argjson hooks "$hooks_obj" \
      '."cline.mcpServers" = (."cline.mcpServers" // {}) | ."cline.mcpServers".mori = $mori.mori | .hooks = (.hooks // {}) + $hooks' \
      "$CLINE_SETTINGS" > "$TMP_FILE.2" && mv "$TMP_FILE.2" "$CLINE_SETTINGS"
    rm -f "$TMP_FILE"
    echo "  Merged into $CLINE_SETTINGS"
  else
    echo "  Warning: jq not found. Overwriting $CLINE_SETTINGS (existing config lost)."
    generate_mcp_hooks > "$CLINE_SETTINGS"
  fi
else
  mkdir -p "$(dirname "$CLINE_SETTINGS")"
  generate_mcp_hooks > "$CLINE_SETTINGS"
  echo "  Created $CLINE_SETTINGS"
fi

# 4. Deploy skills
echo ""
echo "[4/4] Deploying skills..."

SKILLS_DIR="$CLINE_CONFIG_DIR/skills"
SOURCE_SKILLS_DIR="$MORI_REPO_ROOT/skills"

if [ -d "$SOURCE_SKILLS_DIR" ]; then
  for skill_subdir in "$SOURCE_SKILLS_DIR"/*/; do
    [ -d "$skill_subdir" ] || continue
    skill_file="${skill_subdir}SKILL.md"
    [ -f "$skill_file" ] || continue

    base_skill=$(basename "$skill_subdir")

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
    done < "$skill_file"

    [ -z "$name" ] && name="$base_skill"
    name=$(echo "$name" | xargs)
    desc=$(echo "$desc" | xargs)

    skill_folder="$SKILLS_DIR/$name"
    mkdir -p "$skill_folder"

    cat << SKILLEOF > "$skill_folder/SKILL.md"
---
name: $name
description: "${desc//\"/\\\"}"
---

$(echo -n "$content" | sed -e 's/[[:space:]]*$//')
SKILLEOF
    echo "  Deployed skill: $name"
  done
else
  echo "  Warning: Source skills folder not found at $SOURCE_SKILLS_DIR — skipping."
fi

echo ""
echo "Mori — Cline Bridge installation complete!"
echo "Restart Cline / VS Code for the changes to take effect."