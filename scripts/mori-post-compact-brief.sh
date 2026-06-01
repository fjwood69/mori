#!/bin/bash
# Mori PostCompact hook — re-ground agent after context compression.
# Deployed to $CLAUDEDIR/ by install-mori-claude.sh.
#
# Enabled by default. Disable with: export MORI_POST_COMPACT_BRIEF=false

if [ "${MORI_POST_COMPACT_BRIEF:-true}" = "false" ]; then
    exit 0
fi

jq -n '{
    "systemMessage": "Context compressed — running /brief to re-ground.",
    "hookSpecificOutput": {
        "hookEventName": "PostCompact",
        "additionalContext": "Context was just compressed. Run /brief to re-ground before continuing — this pulls the latest NATS messages, pending mori-msg items, and session state from before compaction."
    }
}'
exit 0
