# Mori post-compaction re-grounding nudge — wired to the SessionStart event.
#
# Claude Code's PostCompact hook is observability-only and cannot inject context the
# model sees. The sanctioned mechanism is SessionStart, which re-fires after a
# compaction with source="compact" and whose additionalContext IS honoured. This
# script reads the hook payload on stdin and only nudges when source == "compact",
# so it stays silent on ordinary startup/resume/clear even if wired without a matcher.
#
# Deployed to $CLAUDEDIR/ by scripts/legacy/install-mori-claude.ps1 (legacy) or the Mori plugin.
# Enabled by default. Disable with: $env:MORI_POST_COMPACT_BRIEF = "false"

if ($env:MORI_POST_COMPACT_BRIEF -eq "false") {
    exit 0
}

$raw = [Console]::In.ReadToEnd()
$source = ""
try { $source = ($raw | ConvertFrom-Json).source } catch { exit 0 }
if ($source -ne "compact") {
    exit 0
}

$output = @{
    hookSpecificOutput = @{
        hookEventName     = "SessionStart"
        additionalContext = "Context was just compacted. Before doing anything else, run /brief --post-compact to re-ground -- a lightweight delta of what changed in shared state since the last brief (new/superseded memories, pending mori-msg items, NATS traffic). Run it first, then continue."
    }
} | ConvertTo-Json -Depth 5 -Compress

Write-Output $output
exit 0
