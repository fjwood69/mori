# Windows installer script for Mori — Cursor bridge
# Run from the root of the mori repository.
#
# Installs MCP config for Cursor 2.4+, event capture hooks, and
# Mori slash commands. Works whether or not Claude Code is installed
# — Cursor loads hooks from ~/.claude/settings.json and skills from
# ~/.claude/skills/ natively.

param(
    [string]$MoriUrl,
    [string]$ApiKey,
    [string]$ClientName,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

Write-Host "--- Mori — Cursor Bridge Setup Wizard ---" -ForegroundColor Cyan

# URL
if (-not $PSBoundParameters.ContainsKey('MoriUrl')) {
    $PromptUrl = Read-Host -Prompt "Enter Mori Server URL, default http://localhost:8968"
    if ([string]::IsNullOrWhiteSpace($PromptUrl)) {
        $MoriUrl = "http://localhost:8968"
    } else {
        $MoriUrl = $PromptUrl
    }
}

# API key
if (-not $PSBoundParameters.ContainsKey('ApiKey')) {
    $PromptKey = Read-Host -Prompt "Enter Mori API Key, press Enter to skip"
    $ApiKey = $PromptKey
}

# Client name
if (-not $PSBoundParameters.ContainsKey('ClientName')) {
    $DefaultClient = $env:COMPUTERNAME
    $PromptClient = Read-Host -Prompt "Enter Client Name, default $DefaultClient"
    if ([string]::IsNullOrWhiteSpace($PromptClient)) {
        $ClientName = $DefaultClient
    } else {
        $ClientName = $PromptClient
    }
}

# Strip trailing slash
if ($MoriUrl.EndsWith("/")) {
    $MoriUrl = $MoriUrl.Substring(0, $MoriUrl.Length - 1)
}

# Validate URL
if ($MoriUrl -notmatch "^https?://") {
    Write-Error "Invalid Mori URL. Must start with http:// or https://"
    exit 1
}

# Check Cursor is installed (check for global config path)
$CursorGlobalDir = "$env:APPDATA\Cursor\User\globalStorage\cursor.mcp"
$CursorProjectDir = "$env:USERPROFILE\.cursor"
$CursorInstalled = (Test-Path "$env:APPDATA\Cursor") -or (Test-Path $CursorProjectDir)
if (-not $CursorInstalled) {
    Write-Host "Warning: Cursor does not appear to be installed." -ForegroundColor Yellow
    if (-not $Force) {
        $Choice = Read-Host -Prompt "Proceed anyway"
        if ($Choice -notmatch "^[yY]") {
            Write-Host "Installation aborted." -ForegroundColor Red
            exit
        }
    }
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
    $Choice = Read-Host -Prompt "Health check failed. Proceed anyway"
    if ($Choice -notmatch "^[yY]") {
        Write-Host "Installation aborted." -ForegroundColor Red
        exit
    }
}

Write-Host "`nSetting up Mori — Cursor Bridge..." -ForegroundColor Green

$MoriRepoRoot = Resolve-Path "$PSScriptRoot\.."
$AuthHeader = if ($ApiKey) { "-H `"X-Api-Key: $ApiKey`" " } else { "" }

function Get-MoriMcpConfig {
    $config = @"
{
  "mcpServers": {
    "mori": {
      "type": "http",
      "url": "${MoriUrl}/mcp"
    }
  }
}
"@
    return $config | ConvertFrom-Json
}

function Get-MoriHooksConfig {
    $config = @"
{
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
    return $config | ConvertFrom-Json
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
                Write-Host "  Created $Path (was empty)." -ForegroundColor Cyan
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

                # Merge hooks (if present in the new config)
                if ($NewConfig.PSObject.Properties.Name -contains "hooks") {
                    if ($null -eq $Existing.hooks) {
                        $Existing | Add-Member -MemberType NoteProperty -Name "hooks" -Value $NewConfig.hooks
                    } else {
                        foreach ($hookEvent in $NewConfig.hooks.PSObject.Properties) {
                            $Existing.hooks | Add-Member -MemberType NoteProperty -Name $hookEvent.Name -Value $hookEvent.Value -Force
                        }
                    }
                }

                $Existing | ConvertTo-Json -Depth 10 | Set-Content -Path $Path -Encoding UTF8
                Write-Host "  Updated $Path" -ForegroundColor Cyan
            }
        } catch {
            Write-Host "  Warning: Failed to parse existing $Path. Overwriting..." -ForegroundColor Yellow
            $NewConfig | ConvertTo-Json -Depth 10 | Set-Content -Path $Path -Encoding UTF8
        }
    } else {
        $NewConfig | ConvertTo-Json -Depth 10 | Set-Content -Path $Path -Encoding UTF8
        Write-Host "  Created $Path" -ForegroundColor Cyan
    }
}

function Deploy-Skills {
    param([string]$SkillsDir)

    $SourceSkillsDir = "$MoriRepoRoot\skills"
    if (-not (Test-Path $SourceSkillsDir)) {
        Write-Host "  Warning: Source skills folder not found at $SourceSkillsDir — skipping." -ForegroundColor Yellow
        return
    }

    # Check if skills already exist
    if (Test-Path $SkillsDir) {
        $existingItems = Get-ChildItem -Path $SkillsDir
        if ($existingItems.Count -gt 0) {
            Write-Host "  Skipped — $SkillsDir already has skills" -ForegroundColor Cyan
            return
        }
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
        Write-Host "  Deployed skill: mori-$Name" -ForegroundColor Cyan
    }
}

# ---- Step 1: MCP config for Cursor ----
Write-Host "[1/3] Configuring MCP server..." -ForegroundColor Yellow

# Prefer global config, fall back to project-level
$CursorMcpPath = "$CursorGlobalDir\mcp.json"
if (-not (Test-Path $CursorGlobalDir)) {
    $CursorMcpPath = "$CursorProjectDir\mcp.json"
}
$McpConfig = Get-MoriMcpConfig
Merge-JsonFile -Path $CursorMcpPath -NewConfig $McpConfig

# ---- Step 2: Event capture hooks ----
Write-Host "[2/3] Setting up event capture hooks..." -ForegroundColor Yellow

$ClaudeDir = "$env:USERPROFILE\.claude"
$HooksFile = "$ClaudeDir\settings.json"
$HooksConfig = Get-MoriHooksConfig

if (-not (Test-Path $HooksFile)) {
    $HooksConfig | ConvertTo-Json -Depth 10 | Set-Content -Path $HooksFile -Encoding UTF8
    Write-Host "  Created $HooksFile with Mori event capture hooks" -ForegroundColor Cyan
} else {
    $rawContent = Get-Content $HooksFile -Raw -Encoding UTF8
    if ($rawContent -like "*mori*" -or $rawContent -like "*8968*") {
        Write-Host "  Skipped — $HooksFile already has Mori hooks" -ForegroundColor Cyan
    } else {
        # File exists but no Mori hooks — merge
        Merge-JsonFile -Path $HooksFile -NewConfig $HooksConfig
    }
}

# ---- Step 3: Deploy skills ----
Write-Host "[3/3] Deploying skills..." -ForegroundColor Yellow

$SkillsDir = "$ClaudeDir\skills"
Deploy-Skills -SkillsDir $SkillsDir

Write-Host "`nMori — Cursor Bridge installation complete!" -ForegroundColor Green
Write-Host ""
Write-Host "--- Post-Install Steps ---"
Write-Host ""
Write-Host "1. Enable Third-party skills in Cursor:"
Write-Host "   Settings -> Features -> Third-party skills -> Enable"
Write-Host ""
Write-Host "2. Restart Cursor for changes to take effect."
Write-Host ""
Write-Host "3. Verify:"
Write-Host "   - Open Cursor Agent, type /brief -- shared memories should load"
Write-Host "   - Run: curl $MoriUrl/health"
Write-Host ""
Write-Host "No Claude Code required - Mori creates ~/.claude/settings.json and"
Write-Host "~/.claude/skills/ for you if they do not already exist."
