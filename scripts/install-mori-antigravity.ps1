# Windows installer script for Mori Antigravity Bridge
# Run from the root of the mori repository.

param(
    [string]$MoriUrl = "http://localhost:8968",
    [string]$ApiKey = "",
    [string]$ClientName = "",
    [string]$Target = "prompt",
    [switch]$Force,
    [switch]$Doctor,
    [switch]$UpgradeSkills
)

$ErrorActionPreference = "Stop"

function Write-Utf8File {
    param([string]$Path, [string]$Content)
    $dir = Split-Path $Path -Parent
    if ($dir) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    [System.IO.File]::WriteAllText($Path, $Content, (New-Object System.Text.UTF8Encoding $false))
}

function Test-MoriHookEntry {
    param($Entry)
    if ($null -eq $Entry) { return $false }
    try {
        if ($Entry._mori_managed -eq $true) { return $true }
    } catch {}
    try {
        if ($null -ne $Entry.command) {
            $cmd = $Entry.command
            return ($cmd -like "*mori-ship-event*" -or $cmd -like "*/api/events/raw*" -or $cmd -like "*/api/precompact*" -or $cmd -like "*mori-post-compact-brief*")
        }
    } catch {}
    return $false
}

function Get-MoriShipperCommands {
    param([string]$ShipperPath, [string]$Url, [string]$Client, [string]$Key)
    $apiFlag = if ($Key) { " -ApiKey `"$Key`"" } else { "" }
    $base = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$ShipperPath`" -MoriUrl `"$Url`" -Client `"$Client`"${apiFlag}"
    
    $briefPath = Join-Path (Split-Path $ShipperPath -Parent) "mori-post-compact-brief.ps1"
    $postcompact = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$briefPath`""
    
    return @{ 
        raw = "$base -Mode raw"; 
        precompact = "$base -Mode precompact";
        postcompact = $postcompact
    }
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
        $found = $false
        foreach ($h in @($Entry.hooks)) {
            if ($h.type -eq "command" -and (Test-MoriHookEntry -Entry $h)) {
                $h | Add-Member -NotePropertyName command -NotePropertyValue $NewCommand -Force
                $h | Add-Member -NotePropertyName _mori_managed -NotePropertyValue $true -Force
                $found = $true
            }
        }
        if ($found) { return $true }
        $newHook = [PSCustomObject]@{ type = "command"; command = $NewCommand; _mori_managed = $true }
        $Entry.hooks = @($newHook) + @($Entry.hooks)
        return $true
    }
    if ($Entry.type -eq "command" -and (Test-MoriHookEntry -Entry $Entry)) {
        $Entry | Add-Member -NotePropertyName command -NotePropertyValue $NewCommand -Force
        $Entry | Add-Member -NotePropertyName _mori_managed -NotePropertyValue $true -Force
        return $true
    }
    return $false
}

function Merge-MoriHooks {
    param([string]$Path, [string]$ShipperPath, [string]$Url, [string]$Client, [string]$Key)
    $cmds = Get-MoriShipperCommands -ShipperPath $ShipperPath -Url $Url -Client $Client -Key $Key
    $events = @{
        PostToolUse = $cmds.raw; PostToolUseFailure = $cmds.raw
        UserPromptSubmit = $cmds.raw; Stop = $cmds.raw; 
        PreCompact = $cmds.precompact; PostCompact = $cmds.postcompact
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
            $list = @([PSCustomObject]@{ type = "command"; command = $cmd; _mori_managed = $true }) + $list
        }
        $existing.hooks | Add-Member -NotePropertyName $name -NotePropertyValue $list -Force
    }

    Write-Utf8File $Path ($existing | ConvertTo-Json -Depth 20)
    Write-Host "  Merged Mori hooks into $Path" -ForegroundColor Cyan
}

function Deploy-MoriSkills {
    param([string]$SourceDir, [string]$DestDir, [switch]$Upgrade)
    if (-not (Test-Path $SourceDir)) {
        Write-Host "  Warning: skills source not found: $SourceDir" -ForegroundColor Yellow
        return
    }
    New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
    $count = 0
    $files = Get-ChildItem -Path $SourceDir -Filter "SKILL.md" -Recurse
    $files += Get-ChildItem -Path $SourceDir -Filter "*.skill.md" -Recurse
    $seenPaths = @{}
    foreach ($File in $files) {
        $resolvedPath = $File.FullName
        if ($seenPaths.ContainsKey($resolvedPath)) { continue }
        $seenPaths[$resolvedPath] = $true
        
        $Content = Get-Content -Path $resolvedPath -Raw -Encoding UTF8
        $Lines = $Content -split '\r?\n'
        $Name = ""; $Desc = ""; $Rest = @()
        foreach ($Line in $Lines) {
            if ($Line -match "^(?:-\s+)?name:\s*(.*)$") { $Name = $Matches[1].Trim() }
            elseif ($Line -match "^(?:-\s+)?description:\s*(.*)$") { $Desc = $Matches[1].Trim() -replace '^"|"$','' }
        }
        
        if ($Lines.Count -gt 0 -and $Lines[0].Trim() -eq "---") {
            $endIdx = -1
            for ($i = 1; $i -lt $Lines.Count; $i++) {
                if ($Lines[$i].Trim() -eq "---") {
                    $endIdx = $i
                    break
                }
            }
            if ($endIdx -ne -1) {
                for ($i = $endIdx + 1; $i -lt $Lines.Count; $i++) {
                    $Rest += $Lines[$i]
                }
            } else {
                $Rest = $Lines
            }
        } else {
            $inBody = $false
            foreach ($Line in $Lines) {
                if (-not $inBody) {
                    if ($Line -match "^(?:-\s+)?(?:name|description):\s*" -or $Line.Trim() -eq "---") {
                        continue
                    }
                    if ([string]::IsNullOrWhiteSpace($Line)) {
                        continue
                    }
                    $inBody = $true
                }
                $Rest += $Line
            }
        }
        
        if ($Name -eq "") {
            if ($File.BaseName -eq "SKILL") {
                $Name = $File.Directory.Name
            } else {
                $Name = $File.BaseName.Replace(".skill", "")
            }
        }
        $Folder = Join-Path $DestDir "mori-$Name"
        $Out = Join-Path $Folder "SKILL.md"
        $Exists = Test-Path $Out
        if ($Exists -and -not $Upgrade) {
            Write-Host "  Skipped existing skill: mori-$Name (use -UpgradeSkills to overwrite)" -ForegroundColor Yellow
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
        Write-Host "  No new skills deployed (all present skills skipped; use -UpgradeSkills to overwrite/update existing skills)" -ForegroundColor Cyan
    }
}

function Invoke-MoriDoctor {
    param([string]$Url, [string]$Client, [string]$TargetProfile)
    $errors = 0
    if ($TargetProfile -eq "cli") {
        $AppDataDir = "$env:USERPROFILE\.gemini\antigravity"
        $ConfigDir = "$env:USERPROFILE\.gemini\antigravity"
    } else {
        $AppDataDir = "$env:USERPROFILE\.gemini\antigravity-ide"
        $ConfigDir = "$env:USERPROFILE\.gemini\antigravity-ide"
    }
    $PluginsDir = "$ConfigDir\plugins\mori-bridge"
    $mcpPath = "$AppDataDir\mcp_config.json"
    $hooksPath = "$ConfigDir\hooks.json"
    $skillsTargetDir = "$PluginsDir\skills"

    Write-Host "--- Mori Antigravity IDE doctor ---`n" -ForegroundColor Cyan
    Write-Host "Target profile: $($TargetProfile.ToUpper())" -ForegroundColor Cyan

    $configSymlink = "$env:USERPROFILE\.gemini\config"
    if (Test-Path $configSymlink) {
        $item = Get-Item $configSymlink
        if ($item.Attributes -match "ReparsePoint") {
            $targetPath = $item.Target
            Write-Host "INFO  ~/.gemini/config symlink points to: $targetPath" -ForegroundColor Cyan
            if ($targetPath -notlike "*antigravity-ide*" -and (Test-Path "$env:USERPROFILE\.gemini\antigravity-ide")) {
                Write-Host "WARN  ~/.gemini/config symlink points to CLI configuration, but Antigravity IDE folder exists." -ForegroundColor Yellow
                Write-Host "      To redirect config to the IDE, run:" -ForegroundColor Yellow
                Write-Host "      cmd /c rmdir `"$configSymlink`" && mklink /d `"$configSymlink`" `"$env:USERPROFILE\.gemini\antigravity-ide`"" -ForegroundColor Yellow
            }
        }
    }

    if (Test-Path $mcpPath) {
        Write-Host "OK  MCP config: $mcpPath" -ForegroundColor Green
        try {
            $mcp = Get-Content $mcpPath -Raw | ConvertFrom-Json
            $moriUrlField = $mcp.mcpServers.mori.serverUrl
            if ($null -eq $moriUrlField) { $moriUrlField = $mcp.mcpServers.mori.url }
            if ($moriUrlField) {
                Write-Host "    mori URL: $moriUrlField"
                if ($moriUrlField -match "^(https?://[^/]+)") { $Url = $Matches[1] }
            } else { Write-Host "FAIL  mcp_config.json missing mori URL" -ForegroundColor Red; $errors++ }
        } catch { Write-Host "FAIL  Could not parse mcp_config.json" -ForegroundColor Red; $errors++ }
    } else {
        Write-Host "FAIL  MCP config missing: $mcpPath" -ForegroundColor Red; $errors++
    }

    if ($Url) {
        try {
            $r = Invoke-WebRequest -Uri "$Url/health" -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -eq 200) {
                Write-Host "OK  Server health: $Url/health" -ForegroundColor Green
            } else {
                Write-Host "FAIL  Server health: status code $($r.StatusCode)" -ForegroundColor Red; $errors++
            }
        } catch { Write-Host "FAIL  Server health: $_" -ForegroundColor Red; $errors++ }
        try {
            $r = Invoke-WebRequest -Uri "$Url/api/events/health" -UseBasicParsing -TimeoutSec 5
            Write-Host "OK  Events: $($r.Content.Trim())" -ForegroundColor Green
        } catch { Write-Host "WARN  Events health: $_" -ForegroundColor Yellow }
    }

    if (Test-Path $hooksPath) {
        $text = Get-Content $hooksPath -Raw
        if ($text -like "*_mori_managed*" -or $text -like "*mori-ship-event*") { Write-Host "OK  Event hooks present" -ForegroundColor Green }
        else { Write-Host "WARN  No Mori hooks in hooks.json" -ForegroundColor Yellow; $errors++ }
        if ($text -like "*mori-post-compact-brief*") { Write-Host "OK  PostCompact brief hook present" -ForegroundColor Green }
        else { Write-Host "WARN  No PostCompact hook in hooks.json" -ForegroundColor Yellow }
    } else { Write-Host "FAIL  hooks.json missing" -ForegroundColor Red; $errors++ }

    $skills = Get-ChildItem $skillsTargetDir -Directory -Filter "mori-*" -ErrorAction SilentlyContinue
    if ($skills) { Write-Host "OK  Skills: $($skills.Count) mori-*" -ForegroundColor Green }
    else { Write-Host "WARN  No mori-* skills under $skillsTargetDir" -ForegroundColor Yellow }

    Write-Host "`nClient: $Client | Memory lives on the Mori server, not this PC.`n"
    if ($errors) { Write-Host "Doctor: $errors check(s) failed." -ForegroundColor Red; exit 1 }
    Write-Host "Doctor: all critical checks passed. Reload or restart your IDE if MCP was just installed." -ForegroundColor Green
    exit 0
}

# ---- Main Execution ----

if ($Doctor) {
    if ([string]::IsNullOrWhiteSpace($ClientName)) { $ClientName = $env:COMPUTERNAME }
    if ($MoriUrl.EndsWith("/")) { $MoriUrl = $MoriUrl.TrimEnd("/") }
    $docTarget = if ($Target -eq "prompt") { "ide" } else { $Target }
    Invoke-MoriDoctor -Url $MoriUrl -Client $ClientName -TargetProfile $docTarget
}

$TargetSpecified = $PSBoundParameters.ContainsKey("Target")
$Headless = $PSBoundParameters.ContainsKey("MoriUrl") -and $PSBoundParameters.ContainsKey("ClientName")
if (-not $Headless) {
    Write-Host "--- Mori Antigravity Bridge Setup Wizard ---" -ForegroundColor Cyan
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
    if (-not $TargetSpecified) {
        Write-Host ""
        Write-Host "Install for:"
        Write-Host "  [C] CLI only (~/.gemini/antigravity)"
        Write-Host "  [I] IDE only (~/.gemini/antigravity-ide)"
        Write-Host "  [B] Both"
        $choice = Read-Host "Choose [C/I/B] (default: I)"
        if ($choice -match "^[cC]") { $Target = "cli" }
        elseif ($choice -match "^[bB]") { $Target = "both" }
        else { $Target = "ide" }
    }
}

if ($MoriUrl.EndsWith("/")) { $MoriUrl = $MoriUrl.TrimEnd("/") }
if ($MoriUrl -notmatch "^https?://") { Write-Error "Invalid Mori URL"; exit 1 }
if ([string]::IsNullOrWhiteSpace($ClientName)) { $ClientName = $env:COMPUTERNAME }
if ($Target -eq "prompt") { $Target = "ide" }

$Targets = @()
if ($Target -eq "cli" -or $Target -eq "both") { $Targets += "cli" }
if ($Target -eq "ide" -or $Target -eq "both") { $Targets += "ide" }

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

Write-Host "`nSetting up Mori Antigravity Bridge..." -ForegroundColor Green
$mcpOk = $false
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")

foreach ($t in $Targets) {
    Write-Host "`nInstalling to $t profile..." -ForegroundColor Green
    if ($t -eq "cli") {
        $AppDataDir = "$env:USERPROFILE\.gemini\antigravity"
        $ConfigDir = "$env:USERPROFILE\.gemini\antigravity"
    } else {
        $AppDataDir = "$env:USERPROFILE\.gemini\antigravity-ide"
        $ConfigDir = "$env:USERPROFILE\.gemini\antigravity-ide"
    }
    $PluginsDir = "$ConfigDir\plugins\mori-bridge"
    $SkillsTargetDir = "$PluginsDir\skills"

    Write-Host "[1/3] Configuring MCP server..." -ForegroundColor Yellow
    try {
        $MoriServerObj = [ordered]@{
            type = "http"
            serverUrl = "$MoriUrl/mcp"
        }
        if ($ApiKey) {
            $MoriServerObj["headers"] = @{ "X-Api-Key" = $ApiKey }
        }
        Merge-McpFile -Path "$AppDataDir\mcp_config.json" -MoriServer ([PSCustomObject]$MoriServerObj)
        $mcpOk = $true
    } catch { Write-Host "  Error: $_" -ForegroundColor Red }

    Write-Host "[2/3] Setting up event capture hooks..." -ForegroundColor Yellow
    $ShipperDst = "$PluginsDir\mori-ship-event.ps1"
    $ShipperSrc = "$PSScriptRoot\mori-ship-event.ps1"
    if (Test-Path $ShipperSrc) {
        Copy-Item $ShipperSrc $ShipperDst -Force
        Write-Host "  Deployed mori-ship-event.ps1 to $PluginsDir" -ForegroundColor Cyan
    } else {
        Write-Host "  Warning: mori-ship-event.ps1 not found alongside installer" -ForegroundColor Yellow
    }

    $BriefDst = "$PluginsDir\mori-post-compact-brief.ps1"
    $BriefSrc = "$PSScriptRoot\mori-post-compact-brief.ps1"
    if (Test-Path $BriefSrc) {
        Copy-Item $BriefSrc $BriefDst -Force
        Write-Host "  Deployed mori-post-compact-brief.ps1 to $PluginsDir" -ForegroundColor Cyan
    } else {
        Write-Host "  Warning: mori-post-compact-brief.ps1 not found alongside installer" -ForegroundColor Yellow
    }

    try {
        Merge-MoriHooks -Path "$ConfigDir\hooks.json" -ShipperPath $ShipperDst -Url $MoriUrl -Client $ClientName -Key $ApiKey
    } catch { Write-Host "  Error merging settings: $_" -ForegroundColor Red }

    Write-Host "[3/3] Deploying skills..." -ForegroundColor Yellow
    try {
        Deploy-MoriSkills -SourceDir (Join-Path $RepoRoot "skills") -DestDir $SkillsTargetDir -Upgrade:$UpgradeSkills
    } catch { Write-Host "  Error deploying skills: $_" -ForegroundColor Red }
}

Write-Host ""
if ($mcpOk) {
    Write-Host "Mori Antigravity Bridge installation complete!" -ForegroundColor Green
} else {
    Write-Host "Installation FAILED (MCP config not written)." -ForegroundColor Red
}

Write-Host @"

--- Post-Install Steps ---

1. Confirm MCP: Settings -> MCP -> 'mori' connected
2. Verify: powershell -File scripts/install-mori-antigravity.ps1 -Doctor -MoriUrl "$MoriUrl"
3. In Agent chat: /brief (memory comes from the server, not local disk)

Hook failures: $env:TEMP\mori-hook.log

"@

if (-not $mcpOk) { exit 1 }
exit 0
