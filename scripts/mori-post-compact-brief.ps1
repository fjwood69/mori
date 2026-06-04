# Mori PostCompact hook — re-ground agent after context compression.
# Deployed to $CLAUDEDIR/ by install-mori-claude.ps1.
#
# Enabled by default. Disable with: $env:MORI_POST_COMPACT_BRIEF = "false"

if ($env:MORI_POST_COMPACT_BRIEF -eq "false") {
    exit 0
}

$output = @{
    systemMessage     = "Context compressed — running /brief --post-compact to re-ground."
    hookSpecificOutput = @{
        hookEventName   = "PostCompact"
        additionalContext = "Context was just compressed. Run /brief --post-compact to re-ground before continuing -- a lightweight delta that surfaces only what changed in shared state since the last brief (new/superseded/evicted memories, pending mori-msg items, NATS traffic), without re-dumping the full memory base."
    }
} | ConvertTo-Json -Depth 5 -Compress

Write-Output $output
exit 0
