# Windows installer script for Mori  -  Claude Code bridge
# Run from the root of the mori repository.
#
# Installs MCP config + hooks + permissions + skills for Claude Code CLI
# and/or VS Code extension.

param(
    [string]$MoriUrl = "http://localhost:8968",
    [string]$ApiKey = "",
    [string]$ClientName = "",
    [string]$Target,
    [switch]$Force,
    [switch]$Doctor,
    [switch]$UpgradeSkills
)

# NOTE: param() must be the first statement in a PowerShell script (only comments may
# precede it), so the deprecation notice is emitted here, immediately after it.
Write-Warning "This bespoke installer is superseded by the Mori plugin. Recommended: in Claude Code run '/plugin marketplace add fjwood69/mori' then '/plugin install mori@mori'. See plugins/mori/README.md. This script still works during the deprecation window."

$ErrorActionPreference = "Stop"

$Script:MoriMcpAllow = @(
    # Core session tools
    "mcp__mori__brief", "mcp__mori__pensieve", "mcp__mori__consult_advisor",
    "mcp__mori__consult_status",
    "mcp__mori__update", "mcp__mori__standards_reload",
    # Memory CRUD
    "mcp__mori__memory_list", "mcp__mori__memory_read", "mcp__mori__memory_search",
    "mcp__mori__memory_write", "mcp__mori__memory_req", "mcp__mori__memory_delete",
    # Memory management
    "mcp__mori__memory_export", "mcp__mori__memory_export_all", "mcp__mori__memory_import",
    "mcp__mori__memory_history", "mcp__mori__memory_diff", "mcp__mori__memory_rollback",
    "mcp__mori__memory_review", "mcp__mori__memory_session_summary",
    "mcp__mori__memory_pending_list", "mcp__mori__memory_approve",
    "mcp__mori__memory_reject", "mcp__mori__memory_protect",
    # Dream pipeline
    "mcp__mori__dream_run", "mcp__mori__dream_status",
    # Ingest
    "mcp__mori__mori_ingest", "mcp__mori__mori_ingest_status", "mcp__mori__mori_ingest_preview", "mcp__mori__mori_ingest_content",
    # NATS
    "mcp__mori__nats_pub", "mcp__mori__nats_sub", "mcp__mori__nats_ping",
    # Messaging
    "mcp__mori__msg_send", "mcp__mori__msg_recv", "mcp__mori__msg_thread"
)

function Write-Utf8File {
    param([string]$Path, [string]$Content)
    $dir = Split-Path $Path -Parent
    if ($dir) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    [System.IO.File]::WriteAllText($Path, $Content, (New-Object System.Text.UTF8Encoding $false))
}

function Test-MoriHookCommand {
    param([string]$Command)
    if ([string]::IsNullOrWhiteSpace($Command)) { return $false }
    return ($Command -like "*mori-ship-event*" -or $Command -like "*/api/events/raw*" -or $Command -like "*/api/precompact*" -or $Command -like "*mori-post-compact-brief*")
}

function Get-MoriShipperCommands {
    param([string]$ShipperPath, [string]$Url, [string]$Client, [string]$Key)
    $apiFlag = if ($Key) { " -ApiKey `"$Key`"" } else { "" }
    $base = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$ShipperPath`" -MoriUrl `"$Url`" -Client `"$Client`"${apiFlag}"
    return @{ raw = "$base -Mode raw"; precompact = "$base -Mode precompact" }
}

function Update-HookEntry {
    param($Entry, [string]$NewCommand)
    if ($null -eq $Entry) { return $false }
    if ($Entry.PSObject.Properties.Name -contains "hooks") {
        foreach ($h in @($Entry.hooks)) {
            if ($h.type -eq "command" -and (Test-MoriHookCommand $h.command)) {
                $h | Add-Member -NotePropertyName command -NotePropertyValue $NewCommand -Force
                return $true
            }
        }
        $Entry.hooks = @([PSCustomObject]@{ type = "command"; command = $NewCommand }) + @($Entry.hooks)
        return $true
    }
    if ($Entry.type -eq "command" -and (Test-MoriHookCommand $Entry.command)) {
        $Entry | Add-Member -NotePropertyName command -NotePropertyValue $NewCommand -Force
        return $true
    }
    return $false
}

function Merge-MoriSettings {
    param([string]$Path, [string]$ShipperPath, [string]$BriefPath, [string]$Url, [string]$Client, [string]$Key)
    $cmds = Get-MoriShipperCommands -ShipperPath $ShipperPath -Url $Url -Client $Client -Key $Key
    $briefCmd = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$BriefPath`""
    # Post-compaction re-ground is a SessionStart[compact] hook, NOT PostCompact —
    # Claude Code's PostCompact hook cannot inject context the model sees.
    $events = @{
        PostToolUse = $cmds.raw; PostToolUseFailure = $cmds.raw
        UserPromptSubmit = $cmds.raw; Stop = $cmds.raw; PreCompact = $cmds.precompact
        SessionStart = $briefCmd
    }

    if (Test-Path $Path) {
        $raw = Get-Content $Path -Raw -Encoding UTF8
        if ([string]::IsNullOrWhiteSpace($raw)) {
            $existing = [PSCustomObject]@{}
        } else {
            try { $existing = $raw | ConvertFrom-Json }
            catch {
                Write-Host "  Warning: Failed to parse existing $Path — overwriting." -ForegroundColor Yellow
                $existing = [PSCustomObject]@{}
            }
        }
    } else {
        $existing = [PSCustomObject]@{}
    }

    # mcpServers.mori
    if ($null -eq $existing.mcpServers) {
        $existing | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([PSCustomObject]@{})
    }
    $mcpEntry = [PSCustomObject]@{ type = "http"; url = "$Url/mcp" }
    if ($Key) {
        $mcpEntry | Add-Member -NotePropertyName headers -NotePropertyValue ([PSCustomObject]@{ "x-api-key" = $Key })
    }
    $existing.mcpServers | Add-Member -NotePropertyName mori -NotePropertyValue $mcpEntry -Force

    # hooks — per-event merge, preserves non-Mori hooks
    if ($null -eq $existing.hooks) {
        $existing | Add-Member -NotePropertyName hooks -NotePropertyValue ([PSCustomObject]@{})
    }
    foreach ($name in $events.Keys) {
        $cmd = $events[$name]
        $list = @()
        if ($existing.hooks.PSObject.Properties.Name -contains $name) {
            # Strip all existing mori hooks (flat format or wrapped format) — replace, don't update in-place
            $list = @($existing.hooks.$name | Where-Object {
                $isFlatMori = ($_.type -eq "command" -and (Test-MoriHookCommand ($_.command)))
                $isWrappedMori = ($_.PSObject.Properties.Name -contains "hooks") -and
                                 (@($_.hooks) | Where-Object { Test-MoriHookCommand ($_.command) }).Count -gt 0
                -not ($isFlatMori -or $isWrappedMori)
            })
        }
        # Prepend fresh wrapped-format entry. PostToolUse matches all tools ("*");
        # SessionStart scopes to the post-compaction boundary ("compact").
        $matcher = switch ($name) { "PostToolUse" { "*" } "SessionStart" { "compact" } default { $null } }
        $newEntry = if ($null -ne $matcher) {
            [PSCustomObject]@{
                matcher = $matcher
                hooks = @([PSCustomObject]@{ type = "command"; command = $cmd })
            }
        } else {
            [PSCustomObject]@{
                hooks = @([PSCustomObject]@{ type = "command"; command = $cmd })
            }
        }
        $list = @($newEntry) + $list
        $existing.hooks | Add-Member -NotePropertyName $name -NotePropertyValue $list -Force
    }

    # Clean up the legacy broken PostCompact mori hook on upgrade: strip mori entries,
    # and drop the key entirely if nothing non-mori remains.
    if ($existing.hooks.PSObject.Properties.Name -contains "PostCompact") {
        $kept = @($existing.hooks.PostCompact | Where-Object {
            $isFlatMori = ($_.type -eq "command" -and (Test-MoriHookCommand ($_.command)))
            $isWrappedMori = ($_.PSObject.Properties.Name -contains "hooks") -and
                             (@($_.hooks) | Where-Object { Test-MoriHookCommand ($_.command) }).Count -gt 0
            -not ($isFlatMori -or $isWrappedMori)
        })
        if ($kept.Count -gt 0) {
            $existing.hooks | Add-Member -NotePropertyName PostCompact -NotePropertyValue $kept -Force
        } else {
            $existing.hooks.PSObject.Properties.Remove("PostCompact")
        }
    }

    # permissions.allow — additive, no duplicates
    if ($null -eq $existing.permissions) {
        $existing | Add-Member -NotePropertyName permissions -NotePropertyValue ([PSCustomObject]@{ allow = @() })
    }
    if ($null -eq $existing.permissions.allow) {
        $existing.permissions | Add-Member -NotePropertyName allow -NotePropertyValue @() -Force
    }
    $allow = [System.Collections.ArrayList]@($existing.permissions.allow)
    foreach ($tool in $Script:MoriMcpAllow) {
        if ($allow -notcontains $tool) { [void]$allow.Add($tool) }
    }
    $existing.permissions | Add-Member -NotePropertyName allow -NotePropertyValue $allow.ToArray() -Force

    New-Item -ItemType Directory -Force -Path (Split-Path $Path -Parent) | Out-Null
    Write-Utf8File $Path ($existing | ConvertTo-Json -Depth 20)
    Write-Host "  Merged MCP config, hooks, and permissions into $Path" -ForegroundColor Cyan
}

function Deploy-MoriSkills {
    param([string]$SourceDir, [string]$DestDir, [switch]$Upgrade)
    if (-not (Test-Path $SourceDir)) {
        Write-Host "  Warning: skills source not found: $SourceDir" -ForegroundColor Yellow
        return
    }
    New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
    $count = 0
    foreach ($File in Get-ChildItem -Path $SourceDir -Filter "*.skill.md") {
        $Lines = Get-Content -Path $File.FullName -Encoding UTF8
        $Name = ""; $Desc = ""; $Rest = @()
        foreach ($Line in $Lines) {
            if ($Line -match "^-\s+name:\s*(.*)$") { $Name = $Matches[1].Trim() }
            elseif ($Line -match "^-\s+description:\s*(.*)$") { $Desc = $Matches[1].Trim() }
            elseif ($Name -or $Desc -or $Line.Trim()) { $Rest += $Line }
        }
        if ($Name -eq "") { $Name = $File.BaseName.Replace(".skill", "") }
        $Folder = Join-Path $DestDir "mori-$Name"
        $Out = Join-Path $Folder "SKILL.md"
        $Exists = Test-Path $Out
        if ($Exists -and -not $Upgrade) {
            Write-Host "  Skipped existing skill: mori-$Name (use -UpgradeSkills to refresh)" -ForegroundColor Yellow
            continue
        }
        New-Item -ItemType Directory -Force -Path $Folder | Out-Null
        $body = ($Rest -join "`n").Trim()
        Write-Utf8File $Out "---`nname: mori-$Name`ndescription: `"$($Desc.Replace('"','\"'))`"`n---`n`n$body`n"
        if ($Exists) {
            Write-Host "  Overwrote existing skill: mori-$Name" -ForegroundColor Cyan
        } else {
            Write-Host "  Deployed new skill: mori-$Name" -ForegroundColor Green
        }
        $count++
    }
    if ($count -eq 0 -and -not $Upgrade) {
        Write-Host "  No new skills deployed (all present; use -UpgradeSkills to refresh)" -ForegroundColor Cyan
    }
}

function Invoke-MoriDoctor {
    param([string]$Url, [string]$Client)
    $errors = 0
    $ClaudeDir = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { "$env:USERPROFILE\.claude" }
    $settings = Join-Path $ClaudeDir "settings.json"

    Write-Host "--- Mori Claude Code doctor ---`n" -ForegroundColor Cyan

    if (-not (Test-Path $settings)) {
        Write-Host "FAIL  settings.json missing: $settings — run installer first" -ForegroundColor Red
        $errors++
    } else {
        Write-Host "OK  settings.json: $settings" -ForegroundColor Green
        try {
            $cfg = Get-Content $settings -Raw | ConvertFrom-Json
            if ($cfg.mcpServers.mori.url) {
                Write-Host "OK  mcpServers.mori.url: $($cfg.mcpServers.mori.url)" -ForegroundColor Green
                if ($cfg.mcpServers.mori.url -match "^(https?://[^/]+)") { $Url = $Matches[1] }
            } else {
                Write-Host "FAIL  mcpServers.mori missing or no URL — re-run installer" -ForegroundColor Red
                $errors++
            }
        } catch {
            Write-Host "FAIL  Could not parse settings.json: $_ — re-run installer" -ForegroundColor Red
            $errors++
        }

        $text = Get-Content $settings -Raw
        if (Test-MoriHookCommand $text) {
            Write-Host "OK  Event hooks present" -ForegroundColor Green
        } else {
            Write-Host "WARN  No Mori hooks found — re-run installer" -ForegroundColor Yellow
            $errors++
        }
        if ($text -match "mcp__mori__brief") {
            Write-Host "OK  permissions.allow seeded" -ForegroundColor Green
        } else {
            Write-Host "WARN  permissions.allow missing Mori tools — re-run installer to fix" -ForegroundColor Yellow
        }
    }

    if ($Url) {
        try {
            Invoke-WebRequest -Uri "$Url/health" -UseBasicParsing -TimeoutSec 5 | Out-Null
            Write-Host "OK  Server health: $Url/health" -ForegroundColor Green
        } catch {
            Write-Host "FAIL  Server not reachable at $Url — check mori-advisor is running: $_" -ForegroundColor Red
            $errors++
        }
        try {
            $r = Invoke-WebRequest -Uri "$Url/api/events/health" -UseBasicParsing -TimeoutSec 5
            Write-Host "OK  Events: $($r.Content.Trim())" -ForegroundColor Green
        } catch {
            Write-Host "WARN  Events endpoint not responding: $_" -ForegroundColor Yellow
        }
    }

    $skills = Get-ChildItem (Join-Path $ClaudeDir "skills") -Directory -Filter "mori-*" -ErrorAction SilentlyContinue
    if ($skills) {
        Write-Host "OK  Skills: $($skills.Count) mori-* found" -ForegroundColor Green
    } else {
        Write-Host "WARN  No mori-* skills — run installer with -UpgradeSkills" -ForegroundColor Yellow
    }

    Write-Host "`nClient: $Client | Memory lives on the Mori server, not this PC.`n"
    if ($errors) { Write-Host "Doctor: $errors check(s) failed." -ForegroundColor Red; exit 1 }
    Write-Host "Doctor: all critical checks passed." -ForegroundColor Green
    exit 0
}

# ---- Doctor early exit ----
if ($Doctor) {
    if ([string]::IsNullOrWhiteSpace($ClientName)) { $ClientName = $env:COMPUTERNAME }
    if ($MoriUrl.EndsWith("/")) { $MoriUrl = $MoriUrl.TrimEnd("/") }
    Invoke-MoriDoctor -Url $MoriUrl -Client $ClientName
}

# ---- Interactive prompts (skip when all required args supplied) ----
$Headless = $PSBoundParameters.ContainsKey("MoriUrl") -and $PSBoundParameters.ContainsKey("ClientName")
if (-not $Headless) {
    Write-Host "--- Mori  -  Claude Code Bridge Setup Wizard ---" -ForegroundColor Cyan
    if (-not $PSBoundParameters.ContainsKey("MoriUrl")) {
        $p = Read-Host "Enter Mori Server URL [http://localhost:8968]"
        if ($p) { $MoriUrl = $p }
    }
    if (-not $PSBoundParameters.ContainsKey("ApiKey")) {
        $ApiKey = Read-Host "Enter Mori API Key (optional, press Enter to skip)"
    }
    if ([string]::IsNullOrWhiteSpace($ClientName)) {
        $ClientName = $env:COMPUTERNAME
        if (-not $PSBoundParameters.ContainsKey("ClientName")) {
            $p = Read-Host "Enter Client Name [$ClientName]"
            if ($p) { $ClientName = $p }
        }
    }
    if (-not $PSBoundParameters.ContainsKey("Target")) {
        Write-Host "`nInstall for:"
        Write-Host "  [C] CLI only (%USERPROFILE%\.claude\settings.json)"
        Write-Host "  [V] VS Code only (%APPDATA%\Code\User\settings.json)"
        Write-Host "  [B] Both"
        $tc = Read-Host "Choose [C/V/B] (default: C)"
        switch -Regex ($tc.ToLower()) {
            '^v' { $Target = "vscode" }
            '^b' { $Target = "both" }
            default { $Target = "cli" }
        }
    }
}

if ($MoriUrl.EndsWith("/")) { $MoriUrl = $MoriUrl.TrimEnd("/") }
if ($MoriUrl -notmatch "^https?://") { Write-Error "Invalid Mori URL. Must start with http:// or https://"; exit 1 }
if ([string]::IsNullOrWhiteSpace($ClientName)) { $ClientName = $env:COMPUTERNAME }
if ([string]::IsNullOrWhiteSpace($Target)) { $Target = "cli" }

Write-Host "`nValidating $MoriUrl..." -ForegroundColor Yellow
$Connected = $false
try {
    if ((Invoke-WebRequest -Uri "$MoriUrl/health" -UseBasicParsing -TimeoutSec 5).StatusCode -eq 200) {
        Write-Host "Connection successful!" -ForegroundColor Green
        $Connected = $true
    }
} catch { Write-Host "Warning: Could not connect: $_" -ForegroundColor Yellow }
if (-not $Connected -and -not $Force) {
    if ((Read-Host "Health check failed. Proceed anyway? (Y/N)") -notmatch "^[yY]") { exit 1 }
}

Write-Host "`nSetting up Mori  -  Claude Code Bridge..." -ForegroundColor Green
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$ClaudeDir = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { "$env:USERPROFILE\.claude" }
$ShipperSrc = Join-Path $PSScriptRoot "..\mori-ship-event.ps1"
$ShipperDst = Join-Path $ClaudeDir "mori-ship-event.ps1"
$BriefSrc   = Join-Path $PSScriptRoot "..\mori-post-compact-brief.ps1"
$BriefDst   = Join-Path $ClaudeDir "mori-post-compact-brief.ps1"

function Install-ForTarget {
    param([string]$ConfigPath, [string]$SkillsDir, [string]$Label)

    Write-Host "`n[$Label] Installing to $ConfigPath..." -ForegroundColor Yellow

    Write-Host "  [1/3] Deploying event shipper and hooks..." -ForegroundColor Yellow
    New-Item -ItemType Directory -Force -Path $ClaudeDir | Out-Null
    if (Test-Path $ShipperSrc) {
        Copy-Item $ShipperSrc $ShipperDst -Force
        Write-Host "    Deployed mori-ship-event.ps1 to $ClaudeDir" -ForegroundColor Cyan
    } else {
        Write-Host "    Warning: mori-ship-event.ps1 not found alongside installer — hooks will not work." -ForegroundColor Yellow
    }
    if (Test-Path $BriefSrc) {
        Copy-Item $BriefSrc $BriefDst -Force
        Write-Host "    Deployed mori-post-compact-brief.ps1 to $ClaudeDir" -ForegroundColor Cyan
    } else {
        Write-Host "    Warning: mori-post-compact-brief.ps1 not found — PostCompact hook will not work." -ForegroundColor Yellow
    }

    Write-Host "  [2/3] Merging MCP config, hooks, and permissions..." -ForegroundColor Yellow
    try {
        Merge-MoriSettings -Path $ConfigPath -ShipperPath $ShipperDst -BriefPath $BriefDst -Url $MoriUrl -Client $ClientName -Key $ApiKey
    } catch { Write-Host "    Error merging settings: $_" -ForegroundColor Red }

    Write-Host "  [3/3] Deploying skills..." -ForegroundColor Yellow
    Deploy-MoriSkills -SourceDir (Join-Path $RepoRoot "skills") -DestDir $SkillsDir -Upgrade:$UpgradeSkills

    Write-Host "[$Label] Done." -ForegroundColor Green
}

function Install-ForCli {
    $ConfigPath = Join-Path $ClaudeDir "settings.json"
    $SkillsDir = Join-Path $ClaudeDir "skills"
    Install-ForTarget -ConfigPath $ConfigPath -SkillsDir $SkillsDir -Label "CLI"
}

function Install-ForVscode {
    $VscodeBase = "$env:APPDATA\Code\User"
    $ConfigPath = Join-Path $VscodeBase "settings.json"
    $SkillsDir = Join-Path $VscodeBase "skills"

    $ProfilesDir = Join-Path $VscodeBase "profiles"
    if (Test-Path $ProfilesDir) {
        $Profiles = Get-ChildItem -Directory -Path $ProfilesDir
        if ($Profiles.Count -gt 0) {
            Write-Host "`n  VS Code profiles detected:" -ForegroundColor Yellow
            for ($i = 0; $i -lt $Profiles.Count; $i++) {
                $psettings = Join-Path $Profiles[$i].FullName "settings.json"
                $pdisplay = $Profiles[$i].Name
                if (Test-Path $psettings) {
                    $content = Get-Content $psettings -Raw -Encoding UTF8
                    if ($content -match '"name"[^"]*"([^"]*)"') { $pdisplay = "$($Matches[1]) ($($Profiles[$i].Name))" }
                }
                Write-Host "  [$($i+1)] Profile: $pdisplay" -ForegroundColor Yellow
            }
            $profileChoice = Read-Host "`n  Enter profile number, or press Enter for default user config"
            if ($profileChoice -match "^\d+$") {
                $idx = [int]$profileChoice - 1
                if ($idx -ge 0 -and $idx -lt $Profiles.Count) {
                    $ConfigPath = Join-Path $Profiles[$idx].FullName "settings.json"
                    $SkillsDir = Join-Path $Profiles[$idx].FullName "skills"
                }
            }
        }
    }

    Install-ForTarget -ConfigPath $ConfigPath -SkillsDir $SkillsDir -Label "VS Code"
}

switch ($Target.ToLower()) {
    "vscode" { Install-ForVscode }
    "both"   { Install-ForCli; Install-ForVscode }
    default  { Install-ForCli }
}

Write-Host @"

--- Post-Install Steps ---

1. Reload VS Code window: Command Palette -> Developer: Reload Window
2. Confirm MCP: Settings -> MCP -> mori connected
3. Verify: powershell -File scripts/legacy/install-mori-claude.ps1 -Doctor -MoriUrl "$MoriUrl"
4. In Agent chat: /brief  (memory comes from the server, not local disk)

Hook failures: $env:TEMP\mori-hook.log

"@
