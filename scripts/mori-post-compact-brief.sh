#!/bin/bash
# Mori PostCompact hook — re-ground agent after context compression.
# Deployed to $CLAUDEDIR/ by scripts/legacy/install-mori-claude.sh (legacy) or the Mori plugin.
#
# Enabled by default. Disable with: export MORI_POST_COMPACT_BRIEF=false

if [ "${MORI_POST_COMPACT_BRIEF:-true}" = "false" ]; then
    exit 0
fi

jq -n '{
    "systemMessage": "Context compressed — running /brief --post-compact to re-ground.",
    "hookSpecificOutput": {
        "hookEventName": "PostCompact",
        "additionalContext": "Context was just compressed. Run /brief --post-compact to re-ground before continuing — a lightweight delta that surfaces only what changed in shared state since the last brief (new/superseded/evicted memories, pending mori-msg items, NATS traffic), without re-dumping the full memory base."
    }
}'
exit 0
