#Requires -Version 5.1
<#
.SYNOPSIS
    Remove mori's legacy bespoke entries from Claude Code settings.json (Windows).

.DESCRIPTION
    For users migrating from the legacy mori installer scripts to the plugin system.
    Idempotently removes:
      - .mcpServers.mori
      - Hook entries whose command matches mori telemetry/context patterns
      - mori entries from permissions.allow

    Does NOT touch the server, any plugin files, or anything outside settings.json.

.PARAMETER SettingsPath
    Path to settings.json. Defaults to $env:APPDATA\Claude\settings.json.
    On Windows, Claude Code typically stores settings at:
      %APPDATA%\Claude\settings.json  (global)
      .claude\settings.json            (project-level)

.EXAMPLE
    .\uninstall-mori-claude.ps1
    .\uninstall-mori-claude.ps1 -SettingsPath "C:\Users\you\AppData\Roaming\Claude\settings.json"
#>
param(
    [string]$SettingsPath = (Join-Path $env:APPDATA 'Claude\settings.json')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not (Test-Path $SettingsPath)) {
    Write-Host "settings.json not found at: $SettingsPath"
    Write-Host "Pass the correct path with -SettingsPath."
    exit 1
}

# Read + parse JSON
$raw = Get-Content -Raw $SettingsPath
try {
    $settings = $raw | ConvertFrom-Json
} catch {
    Write-Host "settings.json is not valid JSON. Aborting."
    exit 1
}

$changed = $false

# ---- 1. Remove .mcpServers.mori -----------------------------------------------
if ($settings.mcpServers -and $settings.mcpServers.PSObject.Properties['mori']) {
    $settings.mcpServers.PSObject.Properties.Remove('mori')
    Write-Host "[removed] .mcpServers.mori"
    $changed = $true
} else {
    Write-Host "[skip] .mcpServers.mori — not present"
}

# ---- 2. Remove mori hook entries -----------------------------------------------
$moriPattern = 'mori-ship-event|mori-post-compact-brief|mori-context-hook|/api/events/raw|/api/precompact'

$events = @('SessionStart','PostToolUse','PostToolUseFailure','UserPromptSubmit','Stop','PreCompact','PostCompact')

foreach ($event in $events) {
    if ($settings.hooks -and $settings.hooks.PSObject.Properties[$event]) {
        $groups = $settings.hooks.$event
        if ($groups -isnot [System.Array]) { $groups = @($groups) }

        $before = $groups.Count
        $kept = @($groups | Where-Object {
            $hookCmds = @(($_.hooks ?? @()) | ForEach-Object { $_.command ?? '' })
            $isMori = $hookCmds | Where-Object { $_ -match $moriPattern }
            -not $isMori
        })

        $removed = $before - $kept.Count
        if ($removed -gt 0) {
            if ($kept.Count -eq 0) {
                $settings.hooks.PSObject.Properties.Remove($event)
            } else {
                $settings.hooks.$event = $kept
            }
            Write-Host "[removed] $removed hook group(s) from hooks['$event']"
            $changed = $true
        }
    }
}

# ---- 3. Remove mori entries from permissions.allow --------------------------------
if ($settings.permissions -and $settings.permissions.PSObject.Properties['allow']) {
    $allow = $settings.permissions.allow
    if ($allow -isnot [System.Array]) { $allow = @($allow) }
    $before = $allow.Count
    $kept = @($allow | Where-Object { $_ -notmatch 'mori' })
    $removed = $before - $kept.Count
    if ($removed -gt 0) {
        $settings.permissions.allow = $kept
        Write-Host "[removed] $removed entry/entries from permissions.allow matching 'mori'"
        $changed = $true
    } else {
        Write-Host "[skip] permissions.allow — no mori entries found"
    }
}

# ---- Write back ---------------------------------------------------------------
if ($changed) {
    $settings | ConvertTo-Json -Depth 20 | Set-Content -Encoding UTF8 $SettingsPath
    Write-Host ""
    Write-Host "Done. settings.json updated: $SettingsPath"
    Write-Host "Reload your editor to apply changes."
} else {
    Write-Host ""
    Write-Host "Nothing to remove — settings.json is already clean."
}
