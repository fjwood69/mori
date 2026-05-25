# Windows installer script for Mori Antigravity Bridge
# Run from the root of the mori repository.

param(
    [string]$MoriUrl,
    [string]$ApiKey,
    [string]$ClientName,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

# 1. Interactive setup prompt wizard (if parameters not explicitly passed)
Write-Host "--- Mori Antigravity Bridge Setup Wizard ---" -ForegroundColor Cyan

if (-not $PSBoundParameters.ContainsKey('MoriUrl')) {
    $PromptUrl = Read-Host "Enter Mori Server URL [http://localhost:8968] (e.g. http://192.168.0.100:8968)"
    if ([string]::IsNullOrWhiteSpace($PromptUrl)) {
        $MoriUrl = "http://localhost:8968"
    } else {
        $MoriUrl = $PromptUrl
    }
}

if (-not $PSBoundParameters.ContainsKey('ApiKey')) {
    $MoriApiKey = Read-Host "Enter Mori API Key (optional, press Enter to skip)"
    $ApiKey = $MoriApiKey
}

if (-not $PSBoundParameters.ContainsKey('ClientName')) {
    $DefaultClient = $env:COMPUTERNAME
    $PromptClient = Read-Host "Enter Client Name [$DefaultClient]"
    if ([string]::IsNullOrWhiteSpace($PromptClient)) {
        $ClientName = $DefaultClient
    } else {
        $ClientName = $PromptClient
    }
}

# Strip trailing slash from MoriUrl
if ($MoriUrl.EndsWith("/")) {
    $MoriUrl = $MoriUrl.Substring(0, $MoriUrl.Length - 1)
}

# 2. Validate URL format
if ($MoriUrl -notmatch "^https?://") {
    Write-Error "Invalid Mori URL. Must start with http:// or https://"
}

# 3. Check connection to Mori server
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
    $Choice = Read-Host "Mori server health check failed. Do you want to proceed with the installation anyway? (Y/N)"
    if ($Choice -notmatch "^[yY]") {
        Write-Host "Installation aborted." -ForegroundColor Red
        Exit
    }
}

Write-Host "`nSetting up Mori Antigravity Bridge..." -ForegroundColor Green

# Paths configuration
$MoriRepoRoot = Resolve-Path "$PSScriptRoot\.."
$AppDataDir = "$env:USERPROFILE\.gemini\antigravity-ide"
$ConfigDir = "$env:USERPROFILE\.gemini\config"
$PluginsDir = "$ConfigDir\plugins\mori-bridge"
$SkillsTargetDir = "$PluginsDir\skills"

# Ensure directories exist
New-Item -ItemType Directory -Force -Path $AppDataDir | Out-Null
New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
New-Item -ItemType Directory -Force -Path $PluginsDir | Out-Null
New-Item -ItemType Directory -Force -Path $SkillsTargetDir | Out-Null

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

# Write UTF-8 without BOM (required for JSON and MD files)
function Write-Utf8File {
    param([string]$Path, [string]$Content)
    [System.IO.File]::WriteAllText($Path, $Content, (New-Object System.Text.UTF8Encoding $false))
}

# 4. Deploy mcp_config.json
$McpConfigPath = "$AppDataDir\mcp_config.json"
$McpConfig = [PSCustomObject]@{
    mcpServers = [PSCustomObject]@{
        mori = [PSCustomObject]@{
            type = "http"
            url  = "$MoriUrl/mcp"
        }
    }
}

# If config exists and is not empty, merge, otherwise write fresh
if (Test-Path $McpConfigPath) {
    try {
        $RawContent = Get-Content $McpConfigPath -Raw -Encoding UTF8
        if ([string]::IsNullOrWhiteSpace($RawContent)) {
            Write-Utf8File $McpConfigPath ($McpConfig | ConvertTo-Json -Depth 10)
            Write-Host "Created mcp_config.json (was empty)." -ForegroundColor Cyan
        } else {
            $Existing = $RawContent | ConvertFrom-Json
            if ($null -eq $Existing) {
                $Existing = [PSCustomObject]@{}
            }
            if ($null -eq $Existing.mcpServers) {
                $Existing | Add-Member -MemberType NoteProperty -Name "mcpServers" -Value $McpConfig.mcpServers
            } else {
                if ($Existing.mcpServers -is [System.Management.Automation.PSCustomObject]) {
                    $Existing.mcpServers | Add-Member -MemberType NoteProperty -Name "mori" -Value $McpConfig.mcpServers.mori -Force
                } else {
                    $Existing.mcpServers = $McpConfig.mcpServers
                }
            }
            Write-Utf8File $McpConfigPath ($Existing | ConvertTo-Json -Depth 10)
            Write-Host "Updated existing mcp_config.json." -ForegroundColor Cyan
        }
    } catch {
        Write-Host "Warning: Failed to parse existing mcp_config.json. Overwriting..." -ForegroundColor Yellow
        Write-Utf8File $McpConfigPath ($McpConfig | ConvertTo-Json -Depth 10)
    }
} else {
    Write-Utf8File $McpConfigPath ($McpConfig | ConvertTo-Json -Depth 10)
    Write-Host "Created mcp_config.json." -ForegroundColor Cyan
}

# 5. Deploy hooks.json
$HooksPath = "$ConfigDir\hooks.json"
$apiFlag = if ($ApiKey) { " -ApiKey `"$ApiKey`"" } else { "" }
$base = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$ShipperDst`" -MoriUrl `"$MoriUrl`" -Client `"$ClientName`"${apiFlag}"
$rawCmd  = "$base -Mode raw"
$compCmd = "$base -Mode precompact"

$entry        = @([PSCustomObject]@{ type = "command"; command = $rawCmd })
$compactEntry = @([PSCustomObject]@{ type = "command"; command = $compCmd })

$HooksConfig = [PSCustomObject]@{
    hooks = [PSCustomObject]@{
        PostToolUse        = $entry
        PostToolUseFailure = $entry
        UserPromptSubmit   = $entry
        Stop               = $entry
        PreCompact         = $compactEntry
    }
}

# Remove existing hooks.json to force a clean write
if (Test-Path $HooksPath) {
    Remove-Item $HooksPath -Force
}

Write-Utf8File $HooksPath ($HooksConfig | ConvertTo-Json -Depth 10)
Write-Host "Created hooks.json." -ForegroundColor Cyan

# 6. Deploy plugin.json
$PluginJsonPath = "$PluginsDir\plugin.json"
$PluginJson = [PSCustomObject]@{
    name        = "mori-bridge"
    version     = "1.0.0"
    description = "Antigravity plugin providing Mori shared memory skills."
    author      = "fjwood69"
}
Write-Utf8File $PluginJsonPath ($PluginJson | ConvertTo-Json -Depth 10)
Write-Host "Created plugin.json." -ForegroundColor Cyan

# 7. Translate and Deploy Skills
$SourceSkillsDir = "$MoriRepoRoot\skills"
if (-not (Test-Path $SourceSkillsDir)) {
    throw "Source skills folder not found at: $SourceSkillsDir"
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
            # Skip initial blank lines before name/desc
        } else {
            $RestOfLines += $Line
        }
    }

    if ($Name -eq "") {
        # Fallback to filename
        $Name = $File.BaseName.Replace(".skill", "")
    }

    # Format description to escape double quotes
    $EscapedDesc = $Desc.Replace('"', '\"')

    # Construct YAML Frontmatter
    $SkillContent = @"
---
name: mori-$Name
description: "$EscapedDesc"
---

"@
    $SkillContent += ($RestOfLines -join "`r`n").Trim()

    # Target folder: mori-bridge/skills/mori-<name>/SKILL.md
    $SkillFolder = "$SkillsTargetDir\mori-$Name"
    New-Item -ItemType Directory -Force -Path $SkillFolder | Out-Null
    Write-Utf8File "$SkillFolder\SKILL.md" $SkillContent
    Write-Host "Translated and deployed skill: mori-$Name" -ForegroundColor Cyan
}

Write-Host "Mori Antigravity Bridge installation complete!" -ForegroundColor Green
