#!/usr/bin/env bash
# uninstall-mori-claude.sh — Remove mori's legacy bespoke entries from Claude Code settings.json
#
# For users migrating from the legacy mori installer scripts to the plugin system.
# Idempotently removes:
#   - .mcpServers.mori
#   - Hook entries whose command matches mori telemetry/context patterns
#   - mori entries from permissions.allow
#
# Does NOT touch the server, any plugin files, or anything outside settings.json.
# Run from the directory containing your settings.json, or pass its path as $1.
#
# Usage:
#   bash uninstall-mori-claude.sh [path/to/settings.json]

set -euo pipefail

SETTINGS="${1:-${HOME}/.claude/settings.json}"

if [[ ! -f "$SETTINGS" ]]; then
  echo "settings.json not found at: $SETTINGS"
  echo "Pass the correct path as the first argument."
  exit 1
fi

command -v jq >/dev/null 2>&1 || { echo "jq is required but not installed. Aborting."; exit 1; }

# Validate JSON
jq . "$SETTINGS" >/dev/null 2>&1 || { echo "settings.json is not valid JSON. Aborting."; exit 1; }

# Work on a temp copy
TMP="$(mktemp)"
cp "$SETTINGS" "$TMP"

changed=0

# ---- 1. Remove .mcpServers.mori -----------------------------------------------
if jq -e '.mcpServers.mori' "$TMP" >/dev/null 2>&1; then
  jq 'del(.mcpServers.mori)' "$TMP" > "${TMP}.new" && mv "${TMP}.new" "$TMP"
  echo "[removed] .mcpServers.mori"
  changed=1
else
  echo "[skip] .mcpServers.mori — not present"
fi

# ---- 2. Remove mori hook entries -----------------------------------------------
# Matches commands referencing legacy mori hook scripts/endpoints.
MORI_PATTERN='mori-ship-event|mori-post-compact-brief|mori-context-hook|/api/events/raw|/api/precompact'

remove_hooks_from_event() {
  local event="$1"
  local key=".hooks[\"$event\"]"
  if jq -e "$key" "$TMP" >/dev/null 2>&1; then
    local before
    before=$(jq -r "$key | length" "$TMP" 2>/dev/null || echo 0)
    # Filter out hook groups where any hook command matches the mori pattern
    jq --arg event "$event" --arg pat "$MORI_PATTERN" '
      .hooks[$event] = (
        .hooks[$event] // []
        | map(
            select(
              (.hooks // [] | map(.command // "") | any(test($pat))) | not
            )
          )
      )
      | if (.hooks[$event] | length) == 0 then del(.hooks[$event]) else . end
    ' "$TMP" > "${TMP}.new" && mv "${TMP}.new" "$TMP"
    local after
    after=$(jq -r "(.hooks[\"$event\"] // []) | length" "$TMP" 2>/dev/null || echo 0)
    local removed=$(( before - after ))
    if [[ $removed -gt 0 ]]; then
      echo "[removed] $removed hook group(s) from hooks[\"$event\"]"
      changed=1
    fi
  fi
}

for event in SessionStart PostToolUse PostToolUseFailure UserPromptSubmit Stop PreCompact PostCompact; do
  remove_hooks_from_event "$event"
done

# ---- 3. Remove mori entries from permissions.allow -----------------------------
if jq -e '.permissions.allow' "$TMP" >/dev/null 2>&1; then
  before=$(jq '.permissions.allow | length' "$TMP")
  jq --arg pat 'mori' '
    .permissions.allow = (.permissions.allow // [] | map(select(test($pat) | not)))
  ' "$TMP" > "${TMP}.new" && mv "${TMP}.new" "$TMP"
  after=$(jq '.permissions.allow | length' "$TMP")
  removed=$(( before - after ))
  if [[ $removed -gt 0 ]]; then
    echo "[removed] $removed entry/entries from permissions.allow matching 'mori'"
    changed=1
  else
    echo "[skip] permissions.allow — no mori entries found"
  fi
fi

# ---- Write back ----------------------------------------------------------------
if [[ $changed -eq 1 ]]; then
  cp "$TMP" "$SETTINGS"
  echo ""
  echo "Done. settings.json updated: $SETTINGS"
  echo "Reload your editor to apply changes."
else
  echo ""
  echo "Nothing to remove — settings.json is already clean."
fi

rm -f "$TMP" "${TMP}.new"
