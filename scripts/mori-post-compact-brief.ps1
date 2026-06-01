# Mori PostCompact hook — re-ground agent after context compression.
# Deployed to $CLAUDEDIR/ by install-mori-claude.ps1.
#
# Enabled by default. Disable with: $env:MORI_POST_COMPACT_BRIEF = "false"

if ($env:MORI_POST_COMPACT_BRIEF -eq "false") {
    exit 0
}

$output = @{
    systemMessage     = "Context compressed — running /brief to re-ground."
    hookSpecificOutput = @{
        hookEventName   = "PostCompact"
        additionalContext = "Context was just compressed. Run /brief to re-ground before continuing -- this pulls the latest NATS messages, pending mori-msg items, and session state from before compaction."
    }
} | ConvertTo-Json -Depth 5 -Compress

Write-Output $output
exit 0
