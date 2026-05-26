#!/usr/bin/env bash
# Linux/macOS installer script for Mori — Claude Code bridge
# Run from the root of the mori repository.
#
# Installs MCP config + hooks + permissions + skills for Claude Code CLI
# and/or VS Code extension.

set -euo pipefail

MORI_URL="http://localhost:8968"
API_KEY=""
CLIENT_NAME=$(hostname 2>/dev/null || echo "claude-code")
FORCE=false
DOCTOR=false
UPGRADE_SKILLS=false

URL_SPECIFIED=false
KEY_SPECIFIED=false
CLIENT_SPECIFIED=false
TARGET_SPECIFIED=false

MORI_MCP_ALLOW=(
    # Core session tools
    "mcp__mori__brief" "mcp__mori__pensieve" "mcp__mori__consult_advisor"
    "mcp__mori__update" "mcp__mori__standards_reload"
    # Memory CRUD
    "mcp__mori__memory_list" "mcp__mori__memory_read" "mcp__mori__memory_search"
    "mcp__mori__memory_write" "mcp__mori__memory_req" "mcp__mori__memory_delete"
    # Memory management
    "mcp__mori__memory_export" "mcp__mori__memory_export_all" "mcp__mori__memory_import"
    "mcp__mori__memory_history" "mcp__mori__memory_diff" "mcp__mori__memory_rollback"
    "mcp__mori__memory_review" "mcp__mori__memory_session_summary"
    "mcp__mori__memory_pending_list" "mcp__mori__memory_approve"
    "mcp__mori__memory_reject" "mcp__mori__memory_protect"
    # Dream pipeline
    "mcp__mori__dream_run" "mcp__mori__dream_status"
    # Ingest
    "mcp__mori__mori_ingest" "mcp__mori__mori_ingest_status" "mcp__mori__mori_ingest_preview" "mcp__mori__mori_ingest_content"
    # NATS
    "mcp__mori__nats_pub" "mcp__mori__nats_sub" "mcp__mori__nats_ping"
)

show_help() {
  echo "Usage: install-mori-claude.sh [options]"
  echo "Options:"
  echo "  --url <url>          Mori server base URL (default: http://localhost:8968)"
  echo "  --api-key <key>      Optional API key for auth"
  echo "  --client <name>      Client name (default: hostname)"
  echo "  --target <target>    Install target: cli, vscode, or both (default: prompt)"
  echo "  --doctor             Run diagnostic checks and exit"
  echo "  --upgrade-skills     Overwrite existing mori-* skill folders"
  echo "  -f, --force          Skip health check"
  echo "  -h, --help           Show this help"
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --url)            MORI_URL="$2"; URL_SPECIFIED=true; shift 2 ;;
    --api-key)        API_KEY="$2"; KEY_SPECIFIED=true; shift 2 ;;
    --client)         CLIENT_NAME="$2"; CLIENT_SPECIFIED=true; shift 2 ;;
    --target)         TARGET="$2"; TARGET_SPECIFIED=true; shift 2 ;;
    --doctor)         DOCTOR=true; shift ;;
    --upgrade-skills) UPGRADE_SKILLS=true; shift ;;
    -f|--force)       FORCE=true; shift ;;
    -h|--help)        show_help; exit 0 ;;
    *) echo "Unknown option: $1" >&2; show_help; exit 1 ;;
  esac
done

CLAUDEDIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

# ---- Doctor mode ----
doctor() {
    local errors=0
    local settings="$CLAUDEDIR/settings.json"
    local detected_url=""

    echo "--- Mori Claude Code doctor ---"
    echo ""

    if [ -f "$settings" ]; then
        echo "OK  settings.json: $settings"
        if command -v jq &>/dev/null; then
            detected_url=$(jq -r '.mcpServers.mori.url // empty' "$settings" 2>/dev/null || true)
            if [ -n "$detected_url" ]; then
                echo "OK  mcpServers.mori.url: $detected_url"
                MORI_URL="${detected_url%/mcp}"
            else
                echo "FAIL  mcpServers.mori missing or no URL — re-run installer"
                errors=$((errors + 1))
            fi
        fi
        if grep -q "mori-ship-event" "$settings" 2>/dev/null; then
            echo "OK  Event hooks present"
        else
            echo "WARN  No Mori hooks found — re-run installer"
            errors=$((errors + 1))
        fi
        if grep -q "mcp__mori__brief" "$settings" 2>/dev/null; then
            echo "OK  permissions.allow seeded"
        else
            echo "WARN  permissions.allow missing Mori tools — re-run installer to fix"
        fi
    else
        echo "FAIL  settings.json missing: $settings — run installer first"
        errors=$((errors + 1))
    fi

    if [ -n "$MORI_URL" ]; then
        if curl -sf --max-time 5 "$MORI_URL/health" >/dev/null 2>&1; then
            echo "OK  Server health: $MORI_URL/health"
        else
            echo "FAIL  Server not reachable at $MORI_URL — check mori-advisor is running"
            errors=$((errors + 1))
        fi
        local events_result
        if events_result=$(curl -sf --max-time 5 "$MORI_URL/api/events/health" 2>/dev/null); then
            echo "OK  Events: $events_result"
        else
            echo "WARN  Events endpoint not responding"
        fi
    fi

    local skill_count
    skill_count=$(find "$CLAUDEDIR/skills" -maxdepth 1 -name "mori-*" -type d 2>/dev/null | wc -l | tr -d ' ')
    if [ "$skill_count" -gt 0 ]; then
        echo "OK  Skills: $skill_count mori-* found"
    else
        echo "WARN  No mori-* skills — run installer with --upgrade-skills"
    fi

    echo ""
    echo "Client: $CLIENT_NAME | Memory lives on the Mori server, not this PC."
    echo ""
    if [ "$errors" -gt 0 ]; then
        echo "Doctor: $errors check(s) failed."
        exit 1
    fi
    echo "Doctor: all critical checks passed."
    exit 0
}

if [ "$DOCTOR" = "true" ]; then
    MORI_URL="${MORI_URL%/}"
    doctor
fi

# ---- Interactive prompts ----
echo "--- Mori — Claude Code Bridge Setup Wizard ---"

if [ "$URL_SPECIFIED" = "false" ]; then
  read -r -p "Enter Mori Server URL [http://localhost:8968] (e.g. http://192.168.0.100:8968): " input_url
  if [ -n "$input_url" ]; then MORI_URL="$input_url"; fi
fi

if [ "$KEY_SPECIFIED" = "false" ]; then
  read -r -p "Enter Mori API Key (optional, press Enter to skip): " input_key
  API_KEY="$input_key"
fi

if [ "$CLIENT_SPECIFIED" = "false" ]; then
  DEFAULT_CLIENT=$(hostname 2>/dev/null || echo "claude-code")
  read -r -p "Enter Client Name [$DEFAULT_CLIENT]: " input_client
  if [ -n "$input_client" ]; then CLIENT_NAME="$input_client"; fi
fi

if [ "$TARGET_SPECIFIED" = "false" ]; then
  echo ""
  echo "Install for:"
  echo "  [C] CLI only (~/.claude/settings.json)"
  echo "  [V] VS Code only (~/.config/Code/User/settings.json)"
  echo "  [B] Both"
  read -r -p "Choose [C/V/B] (default: C): " target_choice
  case "${target_choice,,}" in
    v|vscode) TARGET="vscode" ;;
    b|both)   TARGET="both" ;;
    *)        TARGET="cli" ;;
  esac
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
  read -r -p "Health check failed. Proceed anyway? (y/N) " confirm
  if [[ ! "$confirm" =~ ^[yY] ]]; then echo "Installation aborted."; exit 1; fi
fi

echo ""
echo "Setting up Mori — Claude Code Bridge..."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MORI_REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

auth_flag=""
[ -n "$API_KEY" ] && auth_flag=" --api-key \"${API_KEY}\""

SHIPPER_SRC="${SCRIPT_DIR}/mori-ship-event.sh"
SHIPPER_DST="${CLAUDEDIR}/mori-ship-event.sh"
RAW_CMD="\"${SHIPPER_DST}\" --url \"${MORI_URL}\" --client \"${CLIENT_NAME}\"${auth_flag} --mode raw"
COMPACT_CMD="\"${SHIPPER_DST}\" --url \"${MORI_URL}\" --client \"${CLIENT_NAME}\"${auth_flag} --mode precompact"

merge_json() {
  local config_path="$1"
  local tmp
  tmp=$(mktemp)

  # Build JSON array of allow tools for jq
  local allow_tools_json
  allow_tools_json=$(printf '%s\n' "${MORI_MCP_ALLOW[@]}" | jq -R . | jq -s .)

  if [ -f "$config_path" ] && [ -s "$config_path" ]; then
    if command -v jq &>/dev/null; then
      jq \
        --arg mori_url "$MORI_URL/mcp" \
        --arg raw "$RAW_CMD" \
        --arg compact "$COMPACT_CMD" \
        --argjson allow "$allow_tools_json" \
        '
        # Detect a mori hook command
        def is_mori_cmd: . // "" | test("mori-ship-event|/api/events/raw|/api/precompact");

        # Per-event hook merge: update existing mori entry in-place, or prepend new one
        def upsert_hook(cmd):
          if . == null then [{"type": "command", "command": cmd}]
          elif any(.[]; .command? | is_mori_cmd) then
            map(if (.command? | is_mori_cmd) then .command = cmd else . end)
          else
            [{"type": "command", "command": cmd}] + .
          end;

        # mcpServers.mori
        .mcpServers.mori = {"type": "http", "url": $mori_url} |

        # hooks — per-event merge preserves non-Mori hooks
        .hooks.PostToolUse        = (.hooks.PostToolUse        | upsert_hook($raw)) |
        .hooks.PostToolUseFailure = (.hooks.PostToolUseFailure | upsert_hook($raw)) |
        .hooks.UserPromptSubmit   = (.hooks.UserPromptSubmit   | upsert_hook($raw)) |
        .hooks.Stop               = (.hooks.Stop               | upsert_hook($raw)) |
        .hooks.PreCompact         = (.hooks.PreCompact         | upsert_hook($compact)) |

        # permissions.allow — additive, no duplicates
        .permissions.allow = ((.permissions.allow // []) + $allow | unique)
        ' \
        "$config_path" > "$tmp" && mv "$tmp" "$config_path"
      echo "  Updated $config_path"
    else
      echo "  Warning: jq not found. Overwriting $config_path (existing config lost)."
      generate_config_json "$config_path"
    fi
  else
    mkdir -p "$(dirname "$config_path")"
    generate_config_json "$config_path"
    echo "  Created $config_path"
  fi
  rm -f "$tmp"
}

generate_config_json() {
  local config_path="$1"
  # Build permissions array
  local allow_json="["
  local first=true
  for tool in "${MORI_MCP_ALLOW[@]}"; do
    if [ "$first" = "true" ]; then
      allow_json+="\"$tool\""
      first=false
    else
      allow_json+=",\"$tool\""
    fi
  done
  allow_json+="]"

  cat > "$config_path" <<EOF
{
  "mcpServers": {
    "mori": {
      "type": "http",
      "url": "${MORI_URL}/mcp"
    }
  },
  "hooks": {
    "PostToolUse": [{"type": "command", "command": "${RAW_CMD}"}],
    "PostToolUseFailure": [{"type": "command", "command": "${RAW_CMD}"}],
    "UserPromptSubmit": [{"type": "command", "command": "${RAW_CMD}"}],
    "Stop": [{"type": "command", "command": "${RAW_CMD}"}],
    "PreCompact": [{"type": "command", "command": "${COMPACT_CMD}"}]
  },
  "permissions": {
    "allow": ${allow_json}
  }
}
EOF
}

deploy_skills() {
  local skills_dir="$1"
  local source_skills_dir="$MORI_REPO_ROOT/skills"

  if [ ! -d "$source_skills_dir" ]; then
    echo "  Warning: Source skills folder not found at $source_skills_dir — skipping." >&2
    return
  fi

  for file in "$source_skills_dir"/*.skill.md; do
    [ -e "$file" ] || continue
    filename=$(basename "$file")
    base_skill="${filename%.skill.md}"

    local name="" desc="" content=""
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
    local skill_out="$skill_folder/SKILL.md"

    if [ -f "$skill_out" ] && [ "$UPGRADE_SKILLS" = "false" ]; then
      echo "  Skipped existing skill: mori-$name (use --upgrade-skills to refresh)"
      continue
    fi

    mkdir -p "$skill_folder"
    cat > "$skill_out" <<SKILLEOF
---
name: mori-$name
description: "${desc//\"/\\\"}"
---

$(echo -n "$content" | sed -e 's/[[:space:]]*$//')
SKILLEOF

    if [ -f "$skill_out" ] && [ "$UPGRADE_SKILLS" = "true" ]; then
      echo "  Overwrote existing skill: mori-$name → $skill_folder"
    else
      echo "  Deployed skill: mori-$name → $skill_folder"
    fi
  done
}

# ---- Deploy shipper ----
deploy_shipper() {
  local target_dir="$1"
  mkdir -p "$target_dir"
  if [ -f "$SHIPPER_SRC" ]; then
    cp "$SHIPPER_SRC" "$SHIPPER_DST" && chmod +x "$SHIPPER_DST"
    echo "    Deployed mori-ship-event.sh to ${target_dir}"
  else
    echo "    Warning: mori-ship-event.sh not found alongside installer — hooks will not work."
  fi
}

# ---- Install functions ----
install_for_cli() {
  local config_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
  local config_path="$config_dir/settings.json"
  local skills_dir="$config_dir/skills"

  echo ""
  echo "[CLI] Installing to $config_path..."

  echo "  [1/3] Deploying event shipper..."
  deploy_shipper "$config_dir"

  echo "  [2/3] Merging MCP config, hooks, and permissions..."
  merge_json "$config_path"

  echo "  [3/3] Deploying skills..."
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

  local profiles_dir="$vscode_base/profiles"
  if [ -d "$profiles_dir" ]; then
    local profiles=()
    for p in "$profiles_dir"/*/; do
      [ -d "$p" ] || continue
      pname=$(basename "$p")
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
      read -r -p "  Enter profile number, or press Enter for default user config: " profile_choice
      if [[ "$profile_choice" =~ ^[0-9]+$ ]] && [ "$profile_choice" -ge 1 ] && [ "$profile_choice" -le "${#profiles[@]}" ]; then
        local idx=$((profile_choice - 1))
        config_path="$profiles_dir/${profiles[$idx]}/settings.json"
        skills_dir="$profiles_dir/${profiles[$idx]}/skills"
      fi
    fi
  fi

  echo ""
  echo "[VS Code] Installing to $config_path..."

  echo "  [1/3] Deploying event shipper..."
  deploy_shipper "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

  echo "  [2/3] Merging MCP config, hooks, and permissions..."
  merge_json "$config_path"

  echo "  [3/3] Deploying skills..."
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
echo ""
echo "--- Post-Install Steps ---"
echo ""
echo "1. Reload VS Code window: Command Palette -> Developer: Reload Window"
echo "2. Confirm MCP: Settings -> MCP -> mori connected"
echo "3. Verify: ./scripts/install-mori-claude.sh --doctor --url \"$MORI_URL\""
echo "4. In Agent chat: /brief  (memory comes from the server, not local disk)"
echo ""
echo "Hook failures: /tmp/mori-hook.log"
echo ""
