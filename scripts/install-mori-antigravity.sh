#!/usr/bin/env bash
# Bash installer script for Mori Antigravity Bridge
# Run from the root of the mori repository.

set -euo pipefail

MORI_URL="http://localhost:8968"
API_KEY=""
CLIENT_NAME=$(hostname 2>/dev/null || echo "antigravity-ide")
FORCE=false
DOCTOR=false
UPGRADE_SKILLS=false
TARGET="prompt"
TARGET_SPECIFIED=false

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MORI_REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALL_PY="${SCRIPT_DIR}/mori_antigravity_install.py"

show_help() {
  echo "Usage: install-mori-antigravity.sh [options]"
  echo ""
  echo "Options:"
  echo "  --url <url>           Mori server base URL (default: http://localhost:8968)"
  echo "  --api-key <key>       Optional API key for event ingestion auth"
  echo "  --client <name>       Client name to report in logs (default: hostname)"
  echo "  --target <target>     Install target: cli, ide, or both (default: prompt)"
  echo "  -f, --force           Proceed even if health check connection fails"
  echo "  --doctor              Run connectivity/config checks only (no changes)"
  echo "  --upgrade-skills      Refresh mori-* skills from repo skills/ subdirectories"
  echo "  -h, --help            Show this help message"
  echo ""
  echo "Post-install: Restart/reload your IDE if MCP config was just written."
  echo "Shared memory is on the Mori server — never local."
}

# Parse options
ARGS="$*"
while [[ $# -gt 0 ]]; do
  case $1 in
    --url)
      MORI_URL="$2"
      shift 2
      ;;
    --api-key)
      API_KEY="$2"
      shift 2
      ;;
    --client)
      CLIENT_NAME="$2"
      shift 2
      ;;
    --target)
      TARGET="$2"
      TARGET_SPECIFIED=true
      shift 2
      ;;
    -f|--force)
      FORCE=true
      shift
      ;;
    --doctor)
      DOCTOR=true
      shift
      ;;
    --upgrade-skills)
      UPGRADE_SKILLS=true
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

if [ "$DOCTOR" = true ]; then
  doc_target="ide"
  if [ "$TARGET" != "prompt" ]; then
    doc_target="$TARGET"
  fi
  exec python3 "$INSTALL_PY" doctor --url "${MORI_URL}" --client "${CLIENT_NAME}" --target "${doc_target}"
fi

echo "--- Mori Antigravity Bridge Setup Wizard ---"

# Step-by-step interactive inputs if not specified in arguments
# Check if options were passed to decide on wizard mode
HEADLESS=false
if [[ "$ARGS" == *"--url"* ]] || [[ "$ARGS" == *"--client"* ]]; then
  HEADLESS=true
fi

if [ "$HEADLESS" = "false" ]; then
  read -p "Enter Mori Server URL [http://localhost:8968] (e.g. http://192.168.0.100:8968): " input_url
  if [ -n "$input_url" ]; then
    MORI_URL="$input_url"
  fi

  read -p "Enter Mori API Key (optional, press Enter to skip): " input_key
  API_KEY="$input_key"

  DEFAULT_CLIENT=$(hostname 2>/dev/null || echo "antigravity-ide")
  read -p "Enter Client Name [$DEFAULT_CLIENT]: " input_client
  if [ -n "$input_client" ]; then
    CLIENT_NAME="$input_client"
  fi

  if [ "$TARGET_SPECIFIED" = "false" ]; then
    echo ""
    echo "Install for:"
    echo "  [C] CLI only (~/.gemini/antigravity)"
    echo "  [I] IDE only (~/.gemini/antigravity-ide)"
    echo "  [B] Both"
    read -p "Choose [C/I/B] (default: I): " target_choice
    case "${target_choice,,}" in
      c|cli)  TARGET="cli" ;;
      b|both) TARGET="both" ;;
      *)      TARGET="ide" ;;
    esac
  fi
elif [ "$TARGET" = "prompt" ]; then
  TARGET="ide"
fi

# Strip trailing slash from MORI_URL
MORI_URL="${MORI_URL%/}"

# Validate URL format
if [[ ! "$MORI_URL" =~ ^https?:// ]]; then
  echo "Error: Invalid Mori URL. Must start with http:// or https://" >&2
  exit 1
fi

# Check connection to Mori server
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

TARGETS=()
if [ "$TARGET" = "cli" ] || [ "$TARGET" = "both" ]; then
  TARGETS+=("cli")
fi
if [ "$TARGET" = "ide" ] || [ "$TARGET" = "both" ]; then
  TARGETS+=("ide")
fi

MCP_OK=0
HOOKS_OK=0

for target in "${TARGETS[@]}"; do
  echo ""
  echo "Installing to $target profile..."
  
  if [ "$target" = "cli" ]; then
    APP_DATA_DIR="$HOME/.gemini/antigravity"
    CONFIG_DIR="$HOME/.gemini/antigravity"
  else
    APP_DATA_DIR="$HOME/.gemini/antigravity-ide"
    CONFIG_DIR="$HOME/.gemini/antigravity-ide"
  fi
  
  PLUGINS_DIR="$CONFIG_DIR/plugins/mori-bridge"
  SKILLS_TARGET_DIR="$PLUGINS_DIR/skills"

  # Ensure directories exist
  mkdir -p "$APP_DATA_DIR"
  mkdir -p "$CONFIG_DIR"
  mkdir -p "$PLUGINS_DIR"
  mkdir -p "$SKILLS_TARGET_DIR"

  # ---- Step 1: MCP config (required) ----
  echo "[1/3] Configuring MCP server..."
  MCP_CONFIG_PATH="$APP_DATA_DIR/mcp_config.json"
  if python3 "$INSTALL_PY" merge-mcp --mcp-path "$MCP_CONFIG_PATH" --url "$MORI_URL" --api-key "$API_KEY"; then
    MCP_OK=1
  else
    echo "  Error: failed to write MCP config" >&2
  fi

  # ---- Step 2: Event capture hooks ----
  echo "[2/3] Setting up event capture hooks..."
  HOOKS_PATH="$CONFIG_DIR/hooks.json"
  SHIPPER_SRC="${SCRIPT_DIR}/mori-ship-event.sh"
  SHIPPER_DST="${PLUGINS_DIR}/mori-ship-event.sh"
  
  BRIEF_SRC="${SCRIPT_DIR}/mori-post-compact-brief.sh"
  BRIEF_DST="${PLUGINS_DIR}/mori-post-compact-brief.sh"

  if [ -f "$SHIPPER_SRC" ]; then
    cp "$SHIPPER_SRC" "$SHIPPER_DST" && chmod +x "$SHIPPER_DST"
    echo "  Deployed mori-ship-event.sh to ${PLUGINS_DIR}"
  else
    echo "  Warning: mori-ship-event.sh not found alongside installer — hooks will not work correctly." >&2
  fi
  
  if [ -f "$BRIEF_SRC" ]; then
    cp "$BRIEF_SRC" "$BRIEF_DST" && chmod +x "$BRIEF_DST"
    echo "  Deployed mori-post-compact-brief.sh to ${PLUGINS_DIR}"
  else
    echo "  Warning: mori-post-compact-brief.sh not found alongside installer — PostCompact hook will not work correctly." >&2
  fi

  if python3 "$INSTALL_PY" merge-hooks \
    --hooks-path "$HOOKS_PATH" \
    --shipper "$SHIPPER_DST" \
    --url "$MORI_URL" \
    --client "$CLIENT_NAME" \
    --api-key "$API_KEY"; then
    HOOKS_OK=1
  else
    echo "  Error: failed to merge hooks config" >&2
  fi

  # ---- Step 3: Deploy plugin.json and skills ----
  echo "[3/3] Deploying skills..."
  PLUGIN_JSON_PATH="$PLUGINS_DIR/plugin.json"
  cat << 'EOF' > "$PLUGIN_JSON_PATH"
{
  "name": "mori-bridge",
  "version": "1.0.0",
  "description": "Antigravity plugin providing Mori shared memory skills.",
  "author": "fjwood69"
}
EOF
  echo "  Created plugin.json"

  UPGRADE_FLAG=""
  [ "$UPGRADE_SKILLS" = true ] && UPGRADE_FLAG="--upgrade"

  python3 "$INSTALL_PY" deploy-skills \
    --source "$MORI_REPO_ROOT/skills" \
    --dest "$SKILLS_TARGET_DIR" \
    $UPGRADE_FLAG
done

echo ""
if [ "$MCP_OK" -eq 1 ]; then
  echo "Mori Antigravity Bridge installation complete!"
else
  echo "Mori Antigravity Bridge installation FAILED (MCP config not written)." >&2
fi

echo ""
echo "--- Post-Install Steps ---"
echo ""
echo "1. Confirm MCP: Check your IDE settings to ensure 'mori' is connected."
echo "2. Verify: ./scripts/install-mori-antigravity.sh --doctor --url \"$MORI_URL\""
echo "3. In Agent chat: /brief — loads shared memory from the server (not local disk)"
echo ""
echo "Hook failures are logged to: ${TMPDIR:-/tmp}/mori-hook.log"
echo ""

if [ "$MCP_OK" -ne 1 ]; then
  exit 1
fi
exit 0
