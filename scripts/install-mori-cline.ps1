# Windows installer script for Mori  -  Cline bridge
# Run from the root of the mori repository.
#
# Installs env vars, plugin registration, MCP config, and skills
# for the Cline AI coding assistant.

param(
    [string]$MoriUrl,
    [string]$ApiKey,
    [string]$ClientName,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

Write-Host "--- Mori  -  Cline Bridge Setup Wizard ---" -ForegroundColor Cyan

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

Write-Host "`nSetting up Mori  -  Cline Bridge..." -ForegroundColor Green

$MoriRepoRoot = Resolve-Path "$PSScriptRoot\.."
$PluginPath = "$MoriRepoRoot\extensions\mori-cline-plugin"

# Deploy shipper script to the CLI config dir (~/.claude)
$ClaudeDir = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { "$env:USERPROFILE\.claude" }
$ShipperSrc = "$PSScriptRoot\mori-ship-event.ps1"
$ShipperDst = "$ClaudeDir\mori-ship-event.ps1"
$BriefSrc = "$PSScriptRoot\mori-post-compact-brief.ps1"
$BriefDst = "$ClaudeDir\mori-post-compact-brief.ps1"
New-Item -ItemType Directory -Force -Path $ClaudeDir | Out-Null
if (Test-Path $ShipperSrc) {
    Copy-Item -Path $ShipperSrc -Destination $ShipperDst -Force
    Write-Host "  Deployed mori-ship-event.ps1 to $ClaudeDir" -ForegroundColor Cyan
} else {
    Write-Host "  Warning: mori-ship-event.ps1 not found alongside installer - hooks will not work correctly." -ForegroundColor Yellow
}
if (Test-Path $BriefSrc) {
    Copy-Item -Path $BriefSrc -Destination $BriefDst -Force
    Write-Host "  Deployed mori-post-compact-brief.ps1 to $ClaudeDir" -ForegroundColor Cyan
} else {
    Write-Host "  Warning: mori-post-compact-brief.ps1 not found alongside installer - PostCompact hook will not work." -ForegroundColor Yellow
}

# Write UTF-8 without BOM (required for JSON and MD files)
function Write-Utf8File {
    param([string]$Path, [string]$Content)
    [System.IO.File]::WriteAllText($Path, $Content, (New-Object System.Text.UTF8Encoding $false))
}

# 1. Set persistent environment variables
Write-Host "[1/4] Setting environment variables..." -ForegroundColor Yellow

# Remove existing Mori vars then re-add
$existingVars = [Environment]::GetEnvironmentVariables("User")
$toRemove = @("MORI_API_URL", "MORI_API_KEY", "MORI_CLIENT")
foreach ($var in $toRemove) {
    try {
        [Environment]::SetEnvironmentVariable($var, $null, "User")
    } catch {
        # may not exist
    }
}

[Environment]::SetEnvironmentVariable("MORI_API_URL", $MoriUrl, "User")
if ($ApiKey) {
    [Environment]::SetEnvironmentVariable("MORI_API_KEY", $ApiKey, "User")
}
[Environment]::SetEnvironmentVariable("MORI_CLIENT", $ClientName, "User")

# Set for current process too
$env:MORI_API_URL = $MoriUrl
if ($ApiKey) { $env:MORI_API_KEY = $ApiKey }
$env:MORI_CLIENT = $ClientName

Write-Host "  MORI_API_URL=$MoriUrl" -ForegroundColor Cyan
Write-Host "  MORI_CLIENT=$ClientName" -ForegroundColor Cyan

# 2. Register the plugin
Write-Host "`n[2/4] Registering Cline plugin..." -ForegroundColor Yellow

if (Get-Command "cline" -ErrorAction SilentlyContinue) {
    if (Test-Path $PluginPath) {
        try {
            cline plugin install "$PluginPath"
            Write-Host "  Plugin registered via Cline CLI." -ForegroundColor Cyan
        } catch {
            Write-Host "  Warning: cline plugin install failed  -  you may need to register manually." -ForegroundColor Yellow
        }
    } else {
        Write-Host "  Warning: Plugin directory not found at $PluginPath" -ForegroundColor Yellow
    }
} else {
    Write-Host "  Cline CLI not found. Skipping plugin registration." -ForegroundColor Yellow
    Write-Host "  To register manually, add to VS Code settings.json:" -ForegroundColor Yellow
    Write-Host "  `"cline.agentRuntimePlugins`": [`"$PluginPath\dist\mori-plugin.js`"]" -ForegroundColor Yellow
}

# 3. Add MCP server to Cline config
Write-Host "`n[3/4] Configuring Cline settings..." -ForegroundColor Yellow

$ClineConfigDir = "$env:USERPROFILE\.cline"
$ClineSettingsPath = "$ClineConfigDir\settings.json"
New-Item -ItemType Directory -Force -Path $ClineConfigDir | Out-Null

function Get-ClineConfigJson {
    $apiFlag = if ($ApiKey) { " -ApiKey `"$ApiKey`"" } else { "" }
    $base = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$ShipperDst`" -MoriUrl `"$MoriUrl`" -Client `"$ClientName`"${apiFlag}"
    $rawCmd  = "$base -Mode raw"
    $compCmd = "$base -Mode precompact"
    $briefCmd = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$BriefDst`""

    $entry        = @([PSCustomObject]@{ type = "command"; command = $rawCmd })
    $compactEntry = @([PSCustomObject]@{ type = "command"; command = $compCmd })
    $briefEntry   = @([PSCustomObject]@{ type = "command"; command = $briefCmd })

    return [PSCustomObject]@{
        "cline.mcpServers" = [PSCustomObject]@{
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
            PostCompact        = $briefEntry
        }
    }
}

$NewConfig = Get-ClineConfigJson

if (Test-Path $ClineSettingsPath) {
    try {
        $RawContent = Get-Content $ClineSettingsPath -Raw -Encoding UTF8
        if ([string]::IsNullOrWhiteSpace($RawContent)) {
            Write-Utf8File $ClineSettingsPath ($NewConfig | ConvertTo-Json -Depth 10)
            Write-Host "  Created $ClineSettingsPath (was empty)." -ForegroundColor Cyan
        } else {
            $Existing = $RawContent | ConvertFrom-Json
            if ($null -eq $Existing) { $Existing = [PSCustomObject]@{} }

            # Merge cline.mcpServers.mori
            $mcpServers = $Existing.'cline.mcpServers'
            if ($null -eq $mcpServers) {
                $Existing | Add-Member -MemberType NoteProperty -Name "cline.mcpServers" -Value $NewConfig.'cline.mcpServers'
            } else {
                if ($mcpServers -is [System.Management.Automation.PSCustomObject]) {
                    $mcpServers | Add-Member -MemberType NoteProperty -Name "mori" -Value $NewConfig.'cline.mcpServers'.mori -Force
                } else {
                    $Existing.'cline.mcpServers' = $NewConfig.'cline.mcpServers'
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

            Write-Utf8File $ClineSettingsPath ($Existing | ConvertTo-Json -Depth 10)
            Write-Host "  Updated $ClineSettingsPath" -ForegroundColor Cyan
        }
    } catch {
        Write-Host "  Warning: Failed to parse existing config. Overwriting..." -ForegroundColor Yellow
        Write-Utf8File $ClineSettingsPath ($NewConfig | ConvertTo-Json -Depth 10)
    }
} else {
    Write-Utf8File $ClineSettingsPath ($NewConfig | ConvertTo-Json -Depth 10)
    Write-Host "  Created $ClineSettingsPath" -ForegroundColor Cyan
}

# 4. Deploy skills
Write-Host "`n[4/4] Deploying skills..." -ForegroundColor Yellow

$SkillsDir = "$ClineConfigDir\skills"
$SourceSkillsDir = "$MoriRepoRoot\skills"

if (Test-Path $SourceSkillsDir) {
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
        Write-Host "  Deployed skill: mori-$Name" -ForegroundColor Cyan
    }
} else {
    Write-Host "  Warning: Source skills folder not found at $SourceSkillsDir  -  skipping." -ForegroundColor Yellow
}

Write-Host "`nMori  -  Cline Bridge installation complete!" -ForegroundColor Green
Write-Host "Restart Cline / VS Code for the changes to take effect." -ForegroundColor Yellow