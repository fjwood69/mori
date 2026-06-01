# Windows installer script for Mori — Cursor bridge
# Pure PowerShell — no Python required.

param(
    [string]$MoriUrl = "http://localhost:8968",
    [string]$ApiKey = "",
    [string]$ClientName = "",
    [switch]$Force,
    [switch]$Doctor,
    [switch]$UpgradeSkills
)

$ErrorActionPreference = "Stop"

$Script:MoriMcpAllow = @(
    # Core session tools
    "mcp__mori__brief", "mcp__mori__pensieve", "mcp__mori__consult_advisor",
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
    return ($Command -like "*mori-ship-event*" -or $Command -like "*/api/events/raw*" -or $Command -like "*/api/precompact*")
}

function Test-MoriHookEntry {
    param($Entry)
    if ($null -eq $Entry) { return $false }
    if ($Entry.PSObject.Properties.Name -contains '_mori_managed' -and $Entry._mori_managed -eq $true) {
        return $true
    }
    if ($Entry.PSObject.Properties.Name -contains 'command') {
        return (Test-MoriHookCommand $Entry.command)
    }
    return $false
}

function New-MoriHookEntry {
    param([string]$Command)
    return [PSCustomObject]@{
        type          = "command"
        command       = $Command
        _mori_managed = $true
    }
}

function Set-MoriHookEntryFields {
    param($Entry, [string]$NewCommand)
    $Entry | Add-Member -NotePropertyName command -NotePropertyValue $NewCommand -Force
    $Entry | Add-Member -NotePropertyName _mori_managed -NotePropertyValue $true -Force
}

function Get-MoriShipperCommands {
    param([string]$ShipperPath, [string]$Url, [string]$Client, [string]$Key)
    $apiFlag = if ($Key) { " -ApiKey `"$Key`"" } else { "" }
    $base = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$ShipperPath`" -MoriUrl `"$Url`" -Client `"$Client`"${apiFlag}"
    return @{ raw = "$base -Mode raw"; precompact = "$base -Mode precompact" }
}

function Merge-McpFile {
    param([string]$Path, [PSCustomObject]$MoriServer)
    New-Item -ItemType Directory -Force -Path (Split-Path $Path -Parent) | Out-Null
    if (Test-Path $Path) {
        $raw = Get-Content $Path -Raw -Encoding UTF8
        if ([string]::IsNullOrWhiteSpace($raw)) {
            $existing = [PSCustomObject]@{ mcpServers = [PSCustomObject]@{ mori = $MoriServer } }
        } else {
            $existing = $raw | ConvertFrom-Json
            if ($null -eq $existing.mcpServers) {
                $existing | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([PSCustomObject]@{})
            }
            $existing.mcpServers | Add-Member -NotePropertyName mori -NotePropertyValue $MoriServer -Force
        }
        Write-Utf8File $Path ($existing | ConvertTo-Json -Depth 10)
        Write-Host "  Updated $Path" -ForegroundColor Cyan
    } else {
        $fresh = [PSCustomObject]@{ mcpServers = [PSCustomObject]@{ mori = $MoriServer } }
        Write-Utf8File $Path ($fresh | ConvertTo-Json -Depth 10)
        Write-Host "  Created $Path" -ForegroundColor Cyan
    }
}

function Update-HookEntry {
    param($Entry, [string]$NewCommand)
    if ($null -eq $Entry) { return $false }
    if ($Entry.PSObject.Properties.Name -contains "hooks") {
        foreach ($h in @($Entry.hooks)) {
            if ($h.type -eq "command" -and (Test-MoriHookEntry $h)) {
                Set-MoriHookEntryFields -Entry $h -NewCommand $NewCommand
                return $true
            }
        }
        $Entry.hooks = @(New-MoriHookEntry $NewCommand) + @($Entry.hooks)
        return $true
    }
    if ($Entry.type -eq "command" -and (Test-MoriHookEntry $Entry)) {
        Set-MoriHookEntryFields -Entry $Entry -NewCommand $NewCommand
        return $true
    }
    return $false
}

function Merge-MoriSettings {
    param([string]$Path, [string]$ShipperPath, [string]$Url, [string]$Client, [string]$Key)
    $cmds = Get-MoriShipperCommands -ShipperPath $ShipperPath -Url $Url -Client $Client -Key $Key
    $events = @{
        PostToolUse = $cmds.raw; PostToolUseFailure = $cmds.raw
        UserPromptSubmit = $cmds.raw; Stop = $cmds.raw; PreCompact = $cmds.precompact
    }

    if (Test-Path $Path) {
        $existing = Get-Content $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    } else {
        $existing = [PSCustomObject]@{}
    }
    if ($null -eq $existing.hooks) {
        $existing | Add-Member -NotePropertyName hooks -NotePropertyValue ([PSCustomObject]@{})
    }

    foreach ($name in $events.Keys) {
        $cmd = $events[$name]
        $list = @()
        if ($existing.hooks.PSObject.Properties.Name -contains $name) {
            $list = @($existing.hooks.$name)
        }
        $updated = $false
        foreach ($entry in $list) {
            if (Update-HookEntry -Entry $entry -NewCommand $cmd) { $updated = $true }
        }
        if (-not $updated) {
            $list = @(New-MoriHookEntry $cmd) + $list
        }
        $existing.hooks | Add-Member -NotePropertyName $name -NotePropertyValue $list -Force
    }

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

    Write-Utf8File $Path ($existing | ConvertTo-Json -Depth 20)
    Write-Host "  Merged Mori hooks + MCP permissions into $Path" -ForegroundColor Cyan
}

function Deploy-MoriSkills {
    param([string]$SourceDir, [string]$DestDir, [switch]$Upgrade)
    if (-not (Test-Path $SourceDir)) {
        Write-Host "  Warning: skills source not found: $SourceDir" -ForegroundColor Yellow
        return
    }
    New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
    $count = 0
    foreach ($SkillDir in Get-ChildItem -Path $SourceDir -Directory) {
        $SkillFile = Join-Path $SkillDir.FullName "SKILL.md"
        if (-not (Test-Path $SkillFile)) { continue }
        $Lines = Get-Content -Path $SkillFile -Encoding UTF8
        $Name = ""; $Desc = ""; $Rest = @()
        foreach ($Line in $Lines) {
            if ($Line -match "^-\s+name:\s*(.*)$") { $Name = $Matches[1].Trim() }
            elseif ($Line -match "^-\s+description:\s*(.*)$") { $Desc = $Matches[1].Trim() }
            elseif ($Name -or $Desc -or $Line.Trim()) { $Rest += $Line }
        }
        if ($Name -eq "") { $Name = $SkillDir.Name }
        $Folder = Join-Path $DestDir "$Name"
        $Out = Join-Path $Folder "SKILL.md"
        if ((Test-Path $Out) -and -not $Upgrade) { continue }
        New-Item -ItemType Directory -Force -Path $Folder | Out-Null
        $body = ($Rest -join "`n").Trim()
        Write-Utf8File $Out "---`nname: $Name`ndescription: `"$($Desc.Replace('"','\"'))`"`n---`n`n$body`n"
        Write-Host "  Deployed skill: $Name" -ForegroundColor Cyan
        $count++
    }
    if ($count -eq 0) {
        Write-Host "  No skills deployed (use -UpgradeSkills to refresh)" -ForegroundColor Cyan
    }
}

function Invoke-MoriDoctor {
    param([string]$Url, [string]$Client)
    $errors = 0
    $CursorDir = Join-Path $env:USERPROFILE ".cursor"
    $ClaudeDir = Join-Path $env:USERPROFILE ".claude"
    $mcpPath = Join-Path $CursorDir "mcp.json"

    Write-Host "--- Mori Cursor doctor ---`n" -ForegroundColor Cyan

    if (Test-Path $mcpPath) {
        Write-Host "OK  MCP config: $mcpPath" -ForegroundColor Green
        try {
            $mcp = Get-Content $mcpPath -Raw | ConvertFrom-Json
            if ($mcp.mcpServers.mori.url) {
                Write-Host "    mori URL: $($mcp.mcpServers.mori.url)"
                if ($mcp.mcpServers.mori.url -match "^(https?://[^/]+)") { $Url = $Matches[1] }
            } else { Write-Host "FAIL  mcp.json missing mori URL" -ForegroundColor Red; $errors++ }
        } catch { Write-Host "FAIL  Could not parse mcp.json" -ForegroundColor Red; $errors++ }
    } else {
        Write-Host "FAIL  MCP config missing: $mcpPath" -ForegroundColor Red; $errors++
    }

    if ($Url) {
        try {
            $r = Invoke-WebRequest -Uri "$Url/health" -UseBasicParsing -TimeoutSec 5
            Write-Host "OK  Server health: $Url/health" -ForegroundColor Green
        } catch { Write-Host "FAIL  Server health: $_" -ForegroundColor Red; $errors++ }
        try {
            $r = Invoke-WebRequest -Uri "$Url/api/events/health" -UseBasicParsing -TimeoutSec 5
            Write-Host "OK  Events: $($r.Content.Trim())" -ForegroundColor Green
        } catch { Write-Host "WARN  Events health: $_" -ForegroundColor Yellow }
    }

    $settings = Join-Path $ClaudeDir "settings.json"
    if (Test-Path $settings) {
        $hooksOk = $false
        try {
            $cfg = Get-Content $settings -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($null -ne $cfg.hooks) {
                foreach ($prop in $cfg.hooks.PSObject.Properties) {
                    foreach ($entry in @($prop.Value)) {
                        if ((Test-MoriHookEntry $entry) -or (
                            $entry.PSObject.Properties.Name -contains 'hooks' -and
                            (@($entry.hooks) | Where-Object { Test-MoriHookEntry $_ }).Count -gt 0
                        )) {
                            $hooksOk = $true
                            break
                        }
                    }
                    if ($hooksOk) { break }
                }
            }
        } catch { }
        if (-not $hooksOk) {
            $text = Get-Content $settings -Raw
            $hooksOk = (Test-MoriHookCommand $text)
        }
        if ($hooksOk) { Write-Host "OK  Event hooks present (_mori_managed or legacy)" -ForegroundColor Green }
        else { Write-Host "WARN  No Mori hooks" -ForegroundColor Yellow; $errors++ }
        $text = Get-Content $settings -Raw
        if ($text -match "mcp__mori__brief") { Write-Host "OK  MCP permissions seeded" -ForegroundColor Green }
        else { Write-Host "WARN  permissions.allow may be missing Mori tools" -ForegroundColor Yellow }
    } else { Write-Host "FAIL  settings.json missing" -ForegroundColor Red; $errors++ }

    $skills = Get-ChildItem (Join-Path $ClaudeDir "skills") -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -in @("brief","consult","dream","ingest","msg","nats","pensieve","req","wrap") }
    if ($skills) { Write-Host "OK  Skills: $($skills.Count) mori skills deployed" -ForegroundColor Green }
    else { Write-Host "WARN  No mori skills deployed" -ForegroundColor Yellow }

    Write-Host "`nClient: $Client | Memory lives on the Mori server, not this PC.`n"
    if ($errors) { Write-Host "Doctor: $errors check(s) failed." -ForegroundColor Red; exit 1 }
    Write-Host "Doctor: all critical checks passed. Reload Cursor window if MCP was just installed." -ForegroundColor Green
    exit 0
}

# ---- Doctor mode ----
if ($Doctor) {
    if ([string]::IsNullOrWhiteSpace($ClientName)) { $ClientName = $env:COMPUTERNAME }
    if ($MoriUrl.EndsWith("/")) { $MoriUrl = $MoriUrl.TrimEnd("/") }
    Invoke-MoriDoctor -Url $MoriUrl -Client $ClientName
}

# ---- Interactive prompts (skip when all args passed on command line) ----
$Headless = $PSBoundParameters.ContainsKey("MoriUrl") -and $PSBoundParameters.ContainsKey("ClientName")
if (-not $Headless) {
    Write-Host "--- Mori — Cursor Bridge Setup Wizard ---" -ForegroundColor Cyan
    if (-not $PSBoundParameters.ContainsKey("MoriUrl")) {
        $p = Read-Host "Enter Mori Server URL [http://localhost:8968]"
        if ($p) { $MoriUrl = $p }
    }
    if (-not $PSBoundParameters.ContainsKey("ApiKey")) {
        $ApiKey = Read-Host "Enter Mori API Key (optional, Enter to skip)"
    }
    if ([string]::IsNullOrWhiteSpace($ClientName)) {
        $ClientName = $env:COMPUTERNAME
        if (-not $PSBoundParameters.ContainsKey("ClientName")) {
            $p = Read-Host "Enter Client Name [$ClientName]"
            if ($p) { $ClientName = $p }
        }
    }
}

if ($MoriUrl.EndsWith("/")) { $MoriUrl = $MoriUrl.TrimEnd("/") }
if ($MoriUrl -notmatch "^https?://") { Write-Error "Invalid Mori URL"; exit 1 }
if ([string]::IsNullOrWhiteSpace($ClientName)) { $ClientName = $env:COMPUTERNAME }

$CursorDir = Join-Path $env:USERPROFILE ".cursor"
$ClaudeDir = Join-Path $env:USERPROFILE ".claude"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")

if (-not (Test-Path "$env:APPDATA\Cursor") -and -not (Test-Path $CursorDir) -and -not $Force) {
    Write-Host "Warning: Cursor not detected." -ForegroundColor Yellow
    if ((Read-Host "Proceed anyway") -notmatch "^[yY]") { exit 1 }
}

Write-Host "`nValidating $MoriUrl..." -ForegroundColor Yellow
$Connected = $false
try {
    if ((Invoke-WebRequest -Uri "$MoriUrl/health" -UseBasicParsing -TimeoutSec 5).StatusCode -eq 200) {
        Write-Host "Connection successful!" -ForegroundColor Green
        $Connected = $true
    }
} catch { Write-Host "Warning: Could not connect: $_" -ForegroundColor Yellow }
if (-not $Connected -and -not $Force) {
    if ((Read-Host "Health check failed. Proceed anyway") -notmatch "^[yY]") { exit 1 }
}

Write-Host "`nSetting up Mori — Cursor Bridge..." -ForegroundColor Green
$mcpOk = $false

Write-Host "[1/3] Configuring MCP server..." -ForegroundColor Yellow
try {
    Merge-McpFile -Path (Join-Path $CursorDir "mcp.json") -MoriServer ([PSCustomObject]@{ type = "http"; url = "$MoriUrl/mcp" })
    $mcpOk = $true
} catch { Write-Host "  Error: $_" -ForegroundColor Red }

Write-Host "[2/3] Setting up event capture hooks..." -ForegroundColor Yellow
New-Item -ItemType Directory -Force -Path $ClaudeDir | Out-Null
$ShipperDst = Join-Path $ClaudeDir "mori-ship-event.ps1"
$ShipperSrc = Join-Path $PSScriptRoot "mori-ship-event.ps1"
if (Test-Path $ShipperSrc) {
    Copy-Item $ShipperSrc $ShipperDst -Force
    Write-Host "  Deployed mori-ship-event.ps1" -ForegroundColor Cyan
} else {
    Write-Host "  Warning: mori-ship-event.ps1 not found" -ForegroundColor Yellow
}
try {
    Merge-MoriSettings -Path (Join-Path $ClaudeDir "settings.json") -ShipperPath $ShipperDst -Url $MoriUrl -Client $ClientName -Key $ApiKey
} catch { Write-Host "  Error merging settings: $_" -ForegroundColor Red }

Write-Host "[3/3] Deploying skills..." -ForegroundColor Yellow
Deploy-MoriSkills -SourceDir (Join-Path $RepoRoot "skills") -DestDir (Join-Path $ClaudeDir "skills") -Upgrade:$UpgradeSkills

Write-Host ""
if ($mcpOk) { Write-Host "Mori — Cursor Bridge installation complete!" -ForegroundColor Green }
else { Write-Host "Installation FAILED (MCP config not written)." -ForegroundColor Red }

Write-Host @"

--- Post-Install Steps ---

1. Reload Cursor window: Command Palette -> Developer: Reload Window
2. Enable third-party skills: Settings -> Rules, Skills, Subagents
3. Confirm MCP: Settings -> MCP -> mori connected
4. Verify: powershell -File scripts/install-mori-cursor.ps1 -Doctor -MoriUrl "$MoriUrl"
5. In Agent chat: /brief (memory comes from the server, not local disk)

Hook failures: $env:TEMP\mori-hook.log

"@

if (-not $mcpOk) { exit 1 }
