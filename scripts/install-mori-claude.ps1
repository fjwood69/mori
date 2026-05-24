# Windows installer script for Mori — Claude Code bridge
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

Write-Host "--- Mori — Claude Code Bridge Setup Wizard ---" -ForegroundColor Cyan

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

Write-Host "`nSetting up Mori — Claude Code Bridge..." -ForegroundColor Green

$MoriRepoRoot = Resolve-Path "$PSScriptRoot\.."
$AuthHeader = if ($ApiKey) { "-H `"X-Api-Key: $ApiKey`" " } else { "" }

function Get-MoriConfigJson {
    $hooks = @"
{
  "mcpServers": {
    "mori": {
      "type": "http",
      "url": "${MoriUrl}/mcp"
    }
  },
  "hooks": {
    "PostToolUse": [
      { "type": "command", "command": "curl -sf -X POST `"${MoriUrl}/api/events/raw?client=${ClientName}`" $($AuthHeader)-H `"Content-Type: application/json`" -d @- >nul 2>&1; exit 0" }
    ],
    "PostToolUseFailure": [
      { "type": "command", "command": "curl -sf -X POST `"${MoriUrl}/api/events/raw?client=${ClientName}`" $($AuthHeader)-H `"Content-Type: application/json`" -d @- >nul 2>&1; exit 0" }
    ],
    "UserPromptSubmit": [
      { "type": "command", "command": "curl -sf -X POST `"${MoriUrl}/api/events/raw?client=${ClientName}`" $($AuthHeader)-H `"Content-Type: application/json`" -d @- >nul 2>&1; exit 0" }
    ],
    "Stop": [
      { "type": "command", "command": "curl -sf -X POST `"${MoriUrl}/api/events/raw?client=${ClientName}`" $($AuthHeader)-H `"Content-Type: application/json`" -d @- >nul 2>&1; exit 0" }
    ],
    "PreCompact": [
      { "type": "command", "command": "curl -sf -X POST `"${MoriUrl}/api/precompact?client=${ClientName}`" $($AuthHeader)-H `"Content-Type: application/json`" -d @- >nul 2>&1; exit 0" }
    ]
  }
}
"@
    return $hooks | ConvertFrom-Json
}

function Merge-JsonFile {
    param([string]$Path, [PSCustomObject]$NewConfig)

    $dir = Split-Path $Path -Parent
    New-Item -ItemType Directory -Force -Path $dir | Out-Null

    if (Test-Path $Path) {
        try {
            $RawContent = Get-Content $Path -Raw -Encoding UTF8
            if ([string]::IsNullOrWhiteSpace($RawContent)) {
                $NewConfig | ConvertTo-Json -Depth 10 | Set-Content -Path $Path -Encoding UTF8
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

                $Existing | ConvertTo-Json -Depth 10 | Set-Content -Path $Path -Encoding UTF8
                Write-Host "Updated $Path" -ForegroundColor Cyan
            }
        } catch {
            Write-Host "Warning: Failed to parse existing $Path. Overwriting..." -ForegroundColor Yellow
            $NewConfig | ConvertTo-Json -Depth 10 | Set-Content -Path $Path -Encoding UTF8
        }
    } else {
        $NewConfig | ConvertTo-Json -Depth 10 | Set-Content -Path $Path -Encoding UTF8
        Write-Host "Created $Path" -ForegroundColor Cyan
    }
}

function Deploy-Skills {
    param([string]$SkillsDir)

    $SourceSkillsDir = "$MoriRepoRoot\skills"
    if (-not (Test-Path $SourceSkillsDir)) {
        Write-Host "  Warning: Source skills folder not found at $SourceSkillsDir — skipping." -ForegroundColor Yellow
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
        Set-Content -Path "$SkillFolder\SKILL.md" -Value $SkillContent -Encoding UTF8
        Write-Host "  Deployed skill: mori-$Name → $SkillFolder" -ForegroundColor Cyan
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

Write-Host "`nMori — Claude Code Bridge installation complete!" -ForegroundColor Green