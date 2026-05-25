#!/usr/bin/env bash
# Bash installer script for Mori Antigravity Bridge
# Run from the root of the mori repository.

set -euo pipefail

MORI_URL="http://localhost:8968"
API_KEY=""
CLIENT_NAME=$(hostname 2>/dev/null || echo "antigravity-ide")
FORCE=false

URL_SPECIFIED=false
KEY_SPECIFIED=false
CLIENT_SPECIFIED=false

# Help function
show_help() {
  echo "Usage: install-mori-antigravity.sh [options]"
  echo "Options:"
  echo "  --url <url>      Mori server base URL (default: http://localhost:8968)"
  echo "  --api-key <key>  Optional API key for event logging auth"
  echo "  --client <name>  Client name to report in logs (default: hostname)"
  echo "  -f, --force      Proceed even if health check connection fails"
  echo "  -h, --help       Show this help message"
}

# Parse options
while [[ $# -gt 0 ]]; do
  case $1 in
    --url)
      MORI_URL="$2"
      URL_SPECIFIED=true
      shift 2
      ;;
    --api-key)
      API_KEY="$2"
      KEY_SPECIFIED=true
      shift 2
      ;;
    --client)
      CLIENT_NAME="$2"
      CLIENT_SPECIFIED=true
      shift 2
      ;;
    -f|--force)
      FORCE=true
      shift
      ;;
    -h|--help)
      show_help
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      show_help
      exit 1
      ;;
  esac
done

echo "--- Mori Antigravity Bridge Setup Wizard ---"

# Step-by-step interactive inputs
if [ "$URL_SPECIFIED" = "false" ]; then
  read -p "Enter Mori Server URL [http://localhost:8968] (e.g. http://192.168.0.100:8968): " input_url
  if [ -n "$input_url" ]; then
    MORI_URL="$input_url"
  fi
fi

if [ "$KEY_SPECIFIED" = "false" ]; then
  read -p "Enter Mori API Key (optional, press Enter to skip): " input_key
  API_KEY="$input_key"
fi

if [ "$CLIENT_SPECIFIED" = "false" ]; then
  DEFAULT_CLIENT=$(hostname 2>/dev/null || echo "antigravity-ide")
  read -p "Enter Client Name [$DEFAULT_CLIENT]: " input_client
  if [ -n "$input_client" ]; then
    CLIENT_NAME="$input_client"
  fi
fi

# Strip trailing slash from MORI_URL
MORI_URL="${MORI_URL%/}"

# 2. Validate URL format
if [[ ! "$MORI_URL" =~ ^https?:// ]]; then
  echo "Error: Invalid Mori URL. Must start with http:// or https://" >&2
  exit 1
fi

# 3. Check connection to Mori server
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
  read -p "Mori server health check failed. Do you want to proceed with the installation anyway? (y/N) " confirm
  if [[ ! "$confirm" =~ ^[yY] ]]; then
    echo "Installation aborted."
    exit 1
  fi
fi

echo ""
echo "Setting up Mori Antigravity Bridge..."

# Paths configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MORI_REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

APP_DATA_DIR="$HOME/.gemini/antigravity-ide"
CONFIG_DIR="$HOME/.gemini/config"
PLUGINS_DIR="$CONFIG_DIR/plugins/mori-bridge"
SKILLS_TARGET_DIR="$PLUGINS_DIR/skills"

# Ensure directories exist
mkdir -p "$APP_DATA_DIR"
mkdir -p "$CONFIG_DIR"
mkdir -p "$PLUGINS_DIR"
mkdir -p "$SKILLS_TARGET_DIR"

# 4. Deploy mcp_config.json
MCP_CONFIG_PATH="$APP_DATA_DIR/mcp_config.json"
MCP_CONFIG_JSON=$(cat <<EOF
{
  "mcpServers": {
    "mori": {
      "type": "http",
      "url": "${MORI_URL}/mcp"
    }
  }
}
EOF
)

if [ -f "$MCP_CONFIG_PATH" ] && [ -s "$MCP_CONFIG_PATH" ]; then
  # Merge configuration if jq is available, otherwise overwrite
  if command -v jq &> /dev/null; then
    TEMP_FILE=$(mktemp)
    jq --argjson mori '{"type": "http", "url": "'"${MORI_URL}/mcp"'"}' \
       '.mcpServers.mori = $mori' "$MCP_CONFIG_PATH" > "$TEMP_FILE"
    mv "$TEMP_FILE" "$MCP_CONFIG_PATH"
    echo "Updated existing mcp_config.json using jq."
  else
    echo "$MCP_CONFIG_JSON" > "$MCP_CONFIG_PATH"
    echo "Warning: jq not found. Overwrote mcp_config.json."
  fi
else
  echo "$MCP_CONFIG_JSON" > "$MCP_CONFIG_PATH"
  echo "Created mcp_config.json."
fi

# 5. Deploy hooks.json
HOOKS_PATH="$CONFIG_DIR/hooks.json"

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

HOOKS_JSON=$(cat <<EOF
{
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
)

if [ -f "$HOOKS_PATH" ] && [ -s "$HOOKS_PATH" ]; then
  if command -v jq &> /dev/null; then
    TEMP_FILE=$(mktemp)
    jq --argjson hooks "$HOOKS_JSON" '.hooks += $hooks.hooks' "$HOOKS_PATH" > "$TEMP_FILE"
    mv "$TEMP_FILE" "$HOOKS_PATH"
    echo "Updated existing hooks.json using jq."
  else
    echo "$HOOKS_JSON" > "$HOOKS_PATH"
    echo "Warning: jq not found. Overwrote hooks.json."
  fi
else
  echo "$HOOKS_JSON" > "$HOOKS_PATH"
  echo "Created hooks.json."
fi

# 6. Deploy plugin.json
PLUGIN_JSON_PATH="$PLUGINS_DIR/plugin.json"
cat << 'EOF' > "$PLUGIN_JSON_PATH"
{
  "name": "mori-bridge",
  "version": "1.0.0",
  "description": "Antigravity plugin providing Mori shared memory skills.",
  "author": "fjwood69"
}
EOF
echo "Created plugin.json."

# 7. Translate and Deploy Skills
SOURCE_SKILLS_DIR="$MORI_REPO_ROOT/skills"
if [ ! -d "$SOURCE_SKILLS_DIR" ]; then
  echo "Error: Source skills folder not found at $SOURCE_SKILLS_DIR" >&2
  exit 1
fi

for file in "$SOURCE_SKILLS_DIR"/*.skill.md; do
  [ -e "$file" ] || continue
  filename=$(basename "$file")
  base_skill="${filename%.skill.md}"
  
  name=""
  desc=""
  content=""
  
  # Read line by line to extract metadata
  while IFS= read -r line || [ -n "$line" ]; do
    if [[ "$line" =~ ^-[[:space:]]+name:[[:space:]]*(.*)$ ]]; then
      name="${BASH_REMATCH[1]}"
    elif [[ "$line" =~ ^-[[:space:]]+description:[[:space:]]*(.*)$ ]]; then
      desc="${BASH_REMATCH[1]}"
    elif [[ -z "$line" && -z "$name" && -z "$desc" ]]; then
      # skip blank lines at top
      :
    else
      content+="$line"$'\n'
    fi
  done < "$file"
  
  if [ -z "$name" ]; then
    name="$base_skill"
  fi
  
  # Clean name & desc from spaces
  name=$(echo "$name" | xargs)
  desc=$(echo "$desc" | xargs)
  
  # Build YAML format
  skill_folder="$SKILLS_TARGET_DIR/mori-$name"
  mkdir -p "$skill_folder"
  
  cat << EOF > "$skill_folder/SKILL.md"
---
name: mori-$name
description: "${desc//\"/\\\"}"
---

$(echo -n "$content" | sed -e 's/[[:space:]]*$//')
EOF
  echo "Translated and deployed skill: mori-$name"
done

echo "Mori Antigravity Bridge installation complete!"
