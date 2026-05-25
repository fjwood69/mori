# Windows installer script for Mori  -  Claude Code bridge
# Run from the root of the mori repository.
#
# Installs MCP config + hooks + skills for Claude Code CLI
# and/or VS Code extension.

param(
    [string]$MoriUrl,
    [string]$ApiKey,
    [string]$ClientName,
    [string]$Target,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

Write-Host "--- Mori  -  Claude Code Bridge Setup Wizard ---" -ForegroundColor Cyan

# URL
if (-not $PSBoundParameters.ContainsKey('MoriUrl')) {
    $PromptUrl = Read-Host "Enter Mori Server URL [http://localhost:8968] (e.g. http://192.168.0.100:8968)"
    if ([string]::IsNullOrWhiteSpace($PromptUrl)) {
        $MoriUrl = "http://localhost:8968"
    } else {
        $MoriUrl = $PromptUrl
    }
}

# API key
if (-not $PSBoundParameters.ContainsKey('ApiKey')) {
    $MoriApiKey = Read-Host "Enter Mori API Key (optional, press Enter to skip)"
    $ApiKey = $MoriApiKey
}

# Client name
if (-not $PSBoundParameters.ContainsKey('ClientName')) {
    $DefaultClient = $env:COMPUTERNAME
    $PromptClient = Read-Host "Enter Client Name [$DefaultClient]"
    if ([string]::IsNullOrWhiteSpace($PromptClient)) {
        $ClientName = $DefaultClient
    } else {
        $ClientName = $PromptClient
    }
}

# Target
if (-not $PSBoundParameters.ContainsKey('Target')) {
    Write-Host "`nInstall for:"
    Write-Host "  [C] CLI only (%USERPROFILE%\.claude\settings.json)"
    Write-Host "  [V] VS Code only (%APPDATA%\Code\User\settings.json)"
    Write-Host "  [B] Both"
    $targetChoice = Read-Host "Choose [C/V/B] (default: C)"
    switch -Regex ($targetChoice.ToLower()) {
        '^v' { $Target = "vscode" }
        '^b' { $Target = "both" }
        default { $Target = "cli" }
    }
}

# Strip trailing slash
if ($MoriUrl.EndsWith("/")) {
    $MoriUrl = $MoriUrl.Substring(0, $MoriUrl.Length - 1)
}

# Validate URL
if ($MoriUrl -notmatch "^https?://") {
    Write-Error "Invalid Mori URL. Must start with http:// or https://"
}

# Health check
Write-Host "`nValidating connection to Mori server at $MoriUrl..." -ForegroundColor Yellow
$HealthUrl = "$MoriUrl/health"
$Connected = $false
try {
    $Response = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 5
    if ($Response.StatusCode -eq 200) {
        Write-Host "Connection successful! Mori server health check: ok" -ForegroundColor Green
        $Connected = $true
    }
} catch {
    Write-Host "Warning: Could not connect to Mori server at ${MoriUrl}: $_" -ForegroundColor Yellow
}

if (-not $Connected -and -not $Force) {
    $Choice = Read-Host "Health check failed. Proceed anyway? (Y/N)"
    if ($Choice -notmatch "^[yY]") {
        Write-Host "Installation aborted." -ForegroundColor Red
        Exit
    }
}

Write-Host "`nSetting up Mori  -  Claude Code Bridge..." -ForegroundColor Green

$MoriRepoRoot = Resolve-Path "$PSScriptRoot\.."

# Deploy shipper script to the CLI config dir (~/.claude)
$ClaudeDir = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { "$env:USERPROFILE\.claude" }
$ShipperSrc = "$PSScriptRoot\mori-ship-event.ps1"
$ShipperDst = "$ClaudeDir\mori-ship-event.ps1"
New-Item -ItemType Directory -Force -Path $ClaudeDir | Out-Null
if (Test-Path $ShipperSrc) {
    Copy-Item -Path $ShipperSrc -Destination $ShipperDst -Force
    Write-Host "  Deployed mori-ship-event.ps1 to $ClaudeDir" -ForegroundColor Cyan
} else {
    Write-Host "  Warning: mori-ship-event.ps1 not found alongside installer - hooks will not work correctly." -ForegroundColor Yellow
}

# Write UTF-8 without BOM (required for JSON files)
function Write-Utf8File {
    param([string]$Path, [string]$Content)
    [System.IO.File]::WriteAllText($Path, $Content, (New-Object System.Text.UTF8Encoding $false))
}

function Get-MoriConfigJson {
    $shipperPath = "$ClaudeDir\mori-ship-event.ps1"
    $apiFlag = if ($ApiKey) { " -ApiKey `"$ApiKey`"" } else { "" }
    $base = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$shipperPath`" -MoriUrl `"$MoriUrl`" -Client `"$ClientName`"${apiFlag}"
    $rawCmd  = "$base -Mode raw"
    $compCmd = "$base -Mode precompact"

    $entry        = @([PSCustomObject]@{ type = "command"; command = $rawCmd })
    $compactEntry = @([PSCustomObject]@{ type = "command"; command = $compCmd })

    return [PSCustomObject]@{
        mcpServers = [PSCustomObject]@{
            mori = [PSCustomObject]@{
                type = "http"
                url  = "$MoriUrl/mcp"
            }
        }
        hooks = [PSCustomObject]@{
            PostToolUse        = $entry
            PostToolUseFailure = $entry
            UserPromptSubmit   = $entry
            Stop               = $entry
            PreCompact         = $compactEntry
        }
    }
}

function Merge-JsonFile {
    param([string]$Path, [PSCustomObject]$NewConfig)

    $dir = Split-Path $Path -Parent
    New-Item -ItemType Directory -Force -Path $dir | Out-Null

    if (Test-Path $Path) {
        try {
            $RawContent = Get-Content $Path -Raw -Encoding UTF8
            if ([string]::IsNullOrWhiteSpace($RawContent)) {
                Write-Utf8File $Path ($NewConfig | ConvertTo-Json -Depth 10)
                Write-Host "Created $Path (was empty)." -ForegroundColor Cyan
            } else {
                $Existing = $RawContent | ConvertFrom-Json
                if ($null -eq $Existing) { $Existing = [PSCustomObject]@{} }

                # Merge mcpServers.mori
                if ($null -eq $Existing.mcpServers) {
                    $Existing | Add-Member -MemberType NoteProperty -Name "mcpServers" -Value $NewConfig.mcpServers
                } else {
                    if ($Existing.mcpServers -is [System.Management.Automation.PSCustomObject]) {
                        $Existing.mcpServers | Add-Member -MemberType NoteProperty -Name "mori" -Value $NewConfig.mcpServers.mori -Force
                    } else {
                        $Existing.mcpServers = $NewConfig.mcpServers
                    }
                }

                # Merge hooks
                if ($null -eq $Existing.hooks) {
                    $Existing | Add-Member -MemberType NoteProperty -Name "hooks" -Value $NewConfig.hooks
                } else {
                    foreach ($hookEvent in $NewConfig.hooks.PSObject.Properties) {
                        $Existing.hooks | Add-Member -MemberType NoteProperty -Name $hookEvent.Name -Value $hookEvent.Value -Force
                    }
                }

                Write-Utf8File $Path ($Existing | ConvertTo-Json -Depth 10)
                Write-Host "Updated $Path" -ForegroundColor Cyan
            }
        } catch {
            Write-Host "Warning: Failed to parse existing $Path. Overwriting..." -ForegroundColor Yellow
            Write-Utf8File $Path ($NewConfig | ConvertTo-Json -Depth 10)
        }
    } else {
        Write-Utf8File $Path ($NewConfig | ConvertTo-Json -Depth 10)
        Write-Host "Created $Path" -ForegroundColor Cyan
    }
}

function Deploy-Skills {
    param([string]$SkillsDir)

    $SourceSkillsDir = "$MoriRepoRoot\skills"
    if (-not (Test-Path $SourceSkillsDir)) {
        Write-Host "  Warning: Source skills folder not found at $SourceSkillsDir  -  skipping." -ForegroundColor Yellow
        return
    }

    $SkillFiles = Get-ChildItem -Path $SourceSkillsDir -Filter *.skill.md
    foreach ($File in $SkillFiles) {
        $Lines = Get-Content -Path $File.FullName -Encoding UTF8
        $Name = ""
        $Desc = ""
        $RestOfLines = @()

        foreach ($Line in $Lines) {
            if ($Line -match "^-\s+name:\s*(.*)$") {
                $Name = $Matches[1].Trim()
            } elseif ($Line -match "^-\s+description:\s*(.*)$") {
                $Desc = $Matches[1].Trim()
            } elseif ($Line -match "^\s*$" -and $Name -eq "" -and $Desc -eq "") {
                # skip
            } else {
                $RestOfLines += $Line
            }
        }

        if ($Name -eq "") { $Name = $File.BaseName.Replace(".skill", "") }

        $EscapedDesc = $Desc.Replace('"', '\"')
        $SkillContent = @"
---
name: mori-$Name
description: "$EscapedDesc"
---
"@
        $SkillContent += ($RestOfLines -join "`r`n").Trim()

        $SkillFolder = "$SkillsDir\mori-$Name"
        New-Item -ItemType Directory -Force -Path $SkillFolder | Out-Null
        Write-Utf8File "$SkillFolder\SKILL.md" $SkillContent
        Write-Host "  Deployed skill: mori-$Name -> $SkillFolder" -ForegroundColor Cyan
    }
}

# ---- Install ----

$ConfigJson = Get-MoriConfigJson

function Install-ForCli {
    $ConfigDir = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { "$env:USERPROFILE\.claude" }
    $ConfigPath = "$ConfigDir\settings.json"
    $SkillsDir = "$ConfigDir\skills"

    Write-Host "`n[CLI] Installing to $ConfigPath..." -ForegroundColor Yellow
    Merge-JsonFile -Path $ConfigPath -NewConfig $ConfigJson

    Write-Host "[CLI] Deploying skills to $SkillsDir..." -ForegroundColor Yellow
    Deploy-Skills -SkillsDir $SkillsDir

    Write-Host "[CLI] Done." -ForegroundColor Green
}

function Install-ForVscode {
    $ConfigPath = "$env:APPDATA\Code\User\settings.json"
    $SkillsDir = "$env:APPDATA\Code\User\skills"

    # Check for VS Code profiles
    $ProfilesDir = "$env:APPDATA\Code\User\profiles"
    if (Test-Path $ProfilesDir) {
        $Profiles = Get-ChildItem -Directory -Path $ProfilesDir
        if ($Profiles.Count -gt 0) {
            Write-Host "`n  VS Code profiles detected:" -ForegroundColor Yellow
            for ($i = 0; $i -lt $Profiles.Count; $i++) {
                $psettings = Join-Path $Profiles[$i].FullName "settings.json"
                $pdisplay = $Profiles[$i].Name
                if (Test-Path $psettings) {
                    $content = Get-Content $psettings -Raw -Encoding UTF8
                    if ($content -match '"name"[^"]*"([^"]*)"') {
                        $pdisplay = "$($Matches[1]) ($($Profiles[$i].Name))"
                    }
                }
                Write-Host "  [$($i+1)] Profile: $pdisplay" -ForegroundColor Yellow
            }

            Write-Host "`n  Install to a profile or the default user config?"
            $profileChoice = Read-Host "  Enter profile number, or press Enter for default user config"
            if ($profileChoice -match "^\d+$") {
                $idx = [int]$profileChoice - 1
                if ($idx -ge 0 -and $idx -lt $Profiles.Count) {
                    $ConfigPath = Join-Path $Profiles[$idx].FullName "settings.json"
                    $SkillsDir = Join-Path $Profiles[$idx].FullName "skills"
                }
            }
        }
    }

    Write-Host "`n[VS Code] Installing to $ConfigPath..." -ForegroundColor Yellow
    Merge-JsonFile -Path $ConfigPath -NewConfig $ConfigJson

    Write-Host "[VS Code] Deploying skills to $SkillsDir..." -ForegroundColor Yellow
    Deploy-Skills -SkillsDir $SkillsDir

    Write-Host "[VS Code] Done." -ForegroundColor Green
}

switch ($Target.ToLower()) {
    "vscode" { Install-ForVscode }
    "both" {
        Install-ForCli
        Install-ForVscode
    }
    default { Install-ForCli }
}

Write-Host "`nMori  -  Claude Code Bridge installation complete!" -ForegroundColor Green