#!/usr/bin/env bash
# Linux/macOS installer script for Mori — Claude Code bridge
# Run from the root of the mori repository.
#
# Installs MCP config + hooks + skills for Claude Code CLI
# and/or VS Code extension.

set -euo pipefail

MORI_URL="http://localhost:8968"
API_KEY=""
CLIENT_NAME=$(hostname 2>/dev/null || echo "claude-code")
FORCE=false

URL_SPECIFIED=false
KEY_SPECIFIED=false
CLIENT_SPECIFIED=false
TARGET_SPECIFIED=false

show_help() {
  echo "Usage: install-mori-claude.sh [options]"
  echo "Options:"
  echo "  --url <url>        Mori server base URL (default: http://localhost:8968)"
  echo "  --api-key <key>    Optional API key for auth"
  echo "  --client <name>    Client name (default: hostname)"
  echo "  --target <target>  Install target: cli, vscode, or both (default: prompt)"
  echo "  -f, --force        Skip health check"
  echo "  -h, --help         Show this help"
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --url) MORI_URL="$2"; URL_SPECIFIED=true; shift 2 ;;
    --api-key) API_KEY="$2"; KEY_SPECIFIED=true; shift 2 ;;
    --client) CLIENT_NAME="$2"; CLIENT_SPECIFIED=true; shift 2 ;;
    --target) TARGET="$2"; TARGET_SPECIFIED=true; shift 2 ;;
    -f|--force) FORCE=true; shift ;;
    -h|--help) show_help; exit 0 ;;
    *) echo "Unknown option: $1" >&2; show_help; exit 1 ;;
  esac
done

echo "--- Mori — Claude Code Bridge Setup Wizard ---"

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
  DEFAULT_CLIENT=$(hostname 2>/dev/null || echo "claude-code")
  read -p "Enter Client Name [$DEFAULT_CLIENT]: " input_client
  if [ -n "$input_client" ]; then
    CLIENT_NAME="$input_client"
  fi
fi

# Target
if [ "$TARGET_SPECIFIED" = "false" ]; then
  echo ""
  echo "Install for:"
  echo "  [C] CLI only (~/.claude/settings.json)"
  echo "  [V] VS Code only (~/.config/Code/User/settings.json)"
  echo "  [B] Both"
  read -p "Choose [C/V/B] (default: C): " target_choice
  case "${target_choice,,}" in
    v|vscode) TARGET="vscode" ;;
    b|both) TARGET="both" ;;
    *) TARGET="cli" ;;
  esac
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
echo "Setting up Mori — Claude Code Bridge..."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MORI_REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Build the config fragment to inject
AUTH_HEADER=""
[ -n "$API_KEY" ] && AUTH_HEADER="-H \"X-Api-Key: $API_KEY\" "

generate_config_json() {
  cat <<EOF
{
  "mcpServers": {
    "mori": {
      "type": "http",
      "url": "${MORI_URL}/mcp"
    }
  },
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

merge_json() {
  local config_path="$1"
  local tmp
  tmp=$(mktemp)

  if [ -f "$config_path" ] && [ -s "$config_path" ]; then
    if command -v jq &>/dev/null; then
      generate_config_json | jq -c '.' > "$tmp"
      local mori_server
      mori_server=$(jq '.mcpServers.mori' "$tmp")
      local hooks_obj
      hooks_obj=$(jq '.hooks' "$tmp")

      jq --argjson mori "$mori_server" --argjson hooks "$hooks_obj" \
        '.mcpServers.mori = $mori | .hooks = (.hooks // {}) + $hooks' \
        "$config_path" > "$tmp.2" && mv "$tmp.2" "$config_path"
      echo "Updated $config_path"
    else
      echo "Warning: jq not found. Overwriting $config_path (existing config lost)."
      generate_config_json > "$config_path"
    fi
  else
    mkdir -p "$(dirname "$config_path")"
    generate_config_json > "$config_path"
    echo "Created $config_path"
  fi
  rm -f "$tmp"
}

deploy_skills() {
  local skills_dir="$1"
  local source_skills_dir="$MORI_REPO_ROOT/skills"

  if [ ! -d "$source_skills_dir" ]; then
    echo "Warning: Source skills folder not found at $source_skills_dir — skipping skill deploy." >&2
    return
  fi

  for file in "$source_skills_dir"/*.skill.md; do
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

    local skill_folder="$skills_dir/mori-$name"
    mkdir -p "$skill_folder"

    cat << SKILLEOF > "$skill_folder/SKILL.md"
---
name: mori-$name
description: "${desc//\"/\\\"}"
---

$(echo -n "$content" | sed -e 's/[[:space:]]*$//')
SKILLEOF
    echo "  Deployed skill: mori-$name → $skill_folder"
  done
}

# ---- Install ----

install_for_cli() {
  local config_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
  local config_path="$config_dir/settings.json"
  local skills_dir="$config_dir/skills"

  echo ""
  echo "[CLI] Installing to $config_path..."
  merge_json "$config_path"

  echo "[CLI] Deploying skills to $skills_dir..."
  deploy_skills "$skills_dir"

  echo "[CLI] Done."
}

install_for_vscode() {
  local vscode_base
  if [[ "$(uname)" == "Darwin" ]]; then
    vscode_base="$HOME/Library/Application Support/Code/User"
  else
    vscode_base="$HOME/.config/Code/User"
  fi

  local config_path="$vscode_base/settings.json"
  local skills_dir="$vscode_base/skills"

  # Check for VS Code profiles
  local profiles_dir="$vscode_base/profiles"
  if [ -d "$profiles_dir" ]; then
    local profiles=()
    for p in "$profiles_dir"/*/; do
      [ -d "$p" ] || continue
      pname=$(basename "$p")
      # Try to read profile name from settings
      pdisplay="$pname"
      if [ -f "$p/settings.json" ]; then
        stored=$(grep -o '"name"[[:space:]]*:[[:space:]]*"[^"]*"' "$p/settings.json" 2>/dev/null | head -1 | sed 's/.*"name"[[:space:]]*:[[:space:]]*"\(.*\)"/\1/')
        [ -n "$stored" ] && pdisplay="$stored ($pname)"
      fi
      profiles+=("$pname")
      echo "  [$(( ${#profiles[@]} ))] Profile: $pdisplay"
    done

    if [ ${#profiles[@]} -gt 0 ]; then
      echo ""
      echo "  VS Code profiles detected. Install to a profile or the default user config?"
      read -p "  Enter profile number, or press Enter for default user config: " profile_choice
      if [[ "$profile_choice" =~ ^[0-9]+$ ]] && [ "$profile_choice" -ge 1 ] && [ "$profile_choice" -le "${#profiles[@]}" ]; then
        local idx=$((profile_choice - 1))
        config_path="$profiles_dir/${profiles[$idx]}/settings.json"
        skills_dir="$profiles_dir/${profiles[$idx]}/skills"
      fi
    fi
  fi

  echo ""
  echo "[VS Code] Installing to $config_path..."
  merge_json "$config_path"

  echo "[VS Code] Deploying skills to $skills_dir..."
  deploy_skills "$skills_dir"

  echo "[VS Code] Done."
}

case "$TARGET" in
  vscode) install_for_vscode ;;
  both)
    install_for_cli
    install_for_vscode
    ;;
  *) install_for_cli ;;
esac

echo ""
echo "Mori — Claude Code Bridge installation complete!"