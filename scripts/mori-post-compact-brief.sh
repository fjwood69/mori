#!/bin/bash
# Mori post-compaction re-grounding nudge — wired to the SessionStart event.
#
# Claude Code's PostCompact hook is OBSERVABILITY-ONLY: its stdout/JSON cannot
# inject anything the model sees (a hookSpecificOutput with hookEventName
# "PostCompact" is rejected by the harness). The sanctioned mechanism is
# SessionStart, which re-fires after a compaction with source="compact" and
# whose additionalContext IS honoured. This script reads the hook payload on
# stdin and only nudges when source == "compact", so it stays silent on
# ordinary startup/resume/clear even if wired without a matcher.
#
# Deployed to $CLAUDEDIR/ by scripts/legacy/install-mori-claude.sh (legacy) or
# the Mori plugin. Enabled by default; disable with MORI_POST_COMPACT_BRIEF=false.

if [ "${MORI_POST_COMPACT_BRIEF:-true}" = "false" ]; then
    exit 0
fi

payload="$(cat)"
if [ "$(printf '%s' "$payload" | jq -r '.source // empty' 2>/dev/null)" != "compact" ]; then
    exit 0
fi

jq -n '{
    hookSpecificOutput: {
        hookEventName: "SessionStart",
        additionalContext: "Context was just compacted. Before doing anything else, run `/brief --post-compact` to re-ground — a lightweight delta of what changed in shared state since the last brief (new/superseded memories, pending mori-msg items, NATS traffic). Run it first, then continue."
    }
}'
exit 0
