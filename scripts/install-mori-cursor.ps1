# Windows installer script for Mori  -  Cursor bridge
# Run from the root of the mori repository.
#
# Installs MCP config for Cursor 2.4+, event capture hooks, and
# Mori slash commands. Works whether or not Claude Code is installed
#  -  Cursor loads hooks from ~/.claude/settings.json and skills from
# ~/.claude/skills/ natively.

param(
    [string]$MoriUrl,
    [string]$ApiKey,
    [string]$ClientName,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

Write-Host "--- Mori  -  Cursor Bridge Setup Wizard ---" -ForegroundColor Cyan

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

# Check Cursor is installed
$CursorDir = "$env:USERPROFILE\.cursor"
$CursorInstalled = (Test-Path "$env:APPDATA\Cursor") -or (Test-Path $CursorDir)
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
$Connected = $false
try {
    $Response = Invoke-WebRequest -Uri "$MoriUrl/health" -UseBasicParsing -TimeoutSec 5
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

Write-Host "`nSetting up Mori  -  Cursor Bridge..." -ForegroundColor Green

$MoriRepoRoot = Resolve-Path "$PSScriptRoot\.."

# Write UTF-8 without BOM (safe for JSON consumers on both PS 5.1 and PS 7)
function Write-Utf8File {
    param([string]$Path, [string]$Content)
    [System.IO.File]::WriteAllText($Path, $Content, (New-Object System.Text.UTF8Encoding $false))
}

# Build MCP config as a PSCustomObject (no JSON string parsing needed)
function Get-MoriMcpConfig {
    return [PSCustomObject]@{
        mcpServers = [PSCustomObject]@{
            mori = [PSCustomObject]@{
                type = "http"
                url  = "$MoriUrl/mcp"
            }
        }
    }
}

# Build hooks config using the installed mori-ship-event.ps1 shipper.
# This avoids curl, -d @-, /dev/null and other Unix-isms in hook commands.
function Get-MoriHooksConfig {
    $shipperPath = "$env:USERPROFILE\.claude\mori-ship-event.ps1"
    $apiFlag = if ($ApiKey) { " -ApiKey `"$ApiKey`"" } else { "" }
    $base = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$shipperPath`" -MoriUrl `"$MoriUrl`" -Client `"$ClientName`"${apiFlag}"
    $rawCmd  = "$base -Mode raw"
    $compCmd = "$base -Mode precompact"

    $entry        = @([PSCustomObject]@{ type = "command"; command = $rawCmd })
    $compactEntry = @([PSCustomObject]@{ type = "command"; command = $compCmd })

    return [PSCustomObject]@{
        hooks = [PSCustomObject]@{
            PostToolUse        = $entry
            PostToolUseFailure = $entry
            UserPromptSubmit   = $entry
            Stop               = $entry
            PreCompact         = $compactEntry
        }
    }
}

# Merge mcpServers.mori into a JSON file, creating the file if absent.
function Merge-McpFile {
    param([string]$Path, [PSCustomObject]$MoriServer)

    $dir = Split-Path $Path -Parent
    New-Item -ItemType Directory -Force -Path $dir | Out-Null

    if (Test-Path $Path) {
        try {
            $raw = Get-Content $Path -Raw -Encoding UTF8
            if ([string]::IsNullOrWhiteSpace($raw)) {
                $fresh = [PSCustomObject]@{ mcpServers = [PSCustomObject]@{ mori = $MoriServer } }
                Write-Utf8File $Path ($fresh | ConvertTo-Json -Depth 10)
                Write-Host "  Created $Path (was empty)." -ForegroundColor Cyan
            } else {
                $existing = $raw | ConvertFrom-Json
                if ($null -eq $existing) { $existing = [PSCustomObject]@{} }

                if ($null -eq $existing.mcpServers) {
                    $existing | Add-Member -MemberType NoteProperty -Name "mcpServers" -Value ([PSCustomObject]@{ mori = $MoriServer })
                } elseif ($existing.mcpServers -is [System.Management.Automation.PSCustomObject]) {
                    $existing.mcpServers | Add-Member -MemberType NoteProperty -Name "mori" -Value $MoriServer -Force
                } else {
                    $existing.mcpServers = [PSCustomObject]@{ mori = $MoriServer }
                }

                Write-Utf8File $Path ($existing | ConvertTo-Json -Depth 10)
                Write-Host "  Updated $Path" -ForegroundColor Cyan
            }
        } catch {
            Write-Host "  Warning: Failed to parse existing $Path. Overwriting..." -ForegroundColor Yellow
            $fresh = [PSCustomObject]@{ mcpServers = [PSCustomObject]@{ mori = $MoriServer } }
            Write-Utf8File $Path ($fresh | ConvertTo-Json -Depth 10)
        }
    } else {
        $fresh = [PSCustomObject]@{ mcpServers = [PSCustomObject]@{ mori = $MoriServer } }
        Write-Utf8File $Path ($fresh | ConvertTo-Json -Depth 10)
        Write-Host "  Created $Path" -ForegroundColor Cyan
    }
}

# Merge hooks into settings.json, creating the file if absent.
function Merge-HooksFile {
    param([string]$Path, [PSCustomObject]$HooksConfig)

    $dir = Split-Path $Path -Parent
    New-Item -ItemType Directory -Force -Path $dir | Out-Null

    if (Test-Path $Path) {
        try {
            $raw = Get-Content $Path -Raw -Encoding UTF8
            if ([string]::IsNullOrWhiteSpace($raw)) {
                Write-Utf8File $Path ($HooksConfig | ConvertTo-Json -Depth 10)
                Write-Host "  Created $Path (was empty)." -ForegroundColor Cyan
            } else {
                $existing = $raw | ConvertFrom-Json
                if ($null -eq $existing) { $existing = [PSCustomObject]@{} }

                if ($null -eq $existing.hooks) {
                    $existing | Add-Member -MemberType NoteProperty -Name "hooks" -Value $HooksConfig.hooks
                } else {
                    foreach ($hookEvent in $HooksConfig.hooks.PSObject.Properties) {
                        $existing.hooks | Add-Member -MemberType NoteProperty -Name $hookEvent.Name -Value $hookEvent.Value -Force
                    }
                }

                Write-Utf8File $Path ($existing | ConvertTo-Json -Depth 10)
                Write-Host "  Updated $Path" -ForegroundColor Cyan
            }
        } catch {
            Write-Host "  Warning: Failed to parse existing $Path. Overwriting..." -ForegroundColor Yellow
            Write-Utf8File $Path ($HooksConfig | ConvertTo-Json -Depth 10)
        }
    } else {
        Write-Utf8File $Path ($HooksConfig | ConvertTo-Json -Depth 10)
        Write-Host "  Created $Path" -ForegroundColor Cyan
    }
}

function Deploy-Skills {
    param([string]$SkillsDir)

    $SourceSkillsDir = "$MoriRepoRoot\skills"
    if (-not (Test-Path $SourceSkillsDir)) {
        Write-Host "  Warning: Source skills folder not found at $SourceSkillsDir  -  skipping." -ForegroundColor Yellow
        return
    }

    if (Test-Path $SkillsDir) {
        $existingItems = Get-ChildItem -Path $SkillsDir
        if ($existingItems.Count -gt 0) {
            Write-Host "  Skipped  -  $SkillsDir already has skills" -ForegroundColor Cyan
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
                # skip blank lines before metadata
            } else {
                $RestOfLines += $Line
            }
        }

        if ($Name -eq "") { $Name = $File.BaseName.Replace(".skill", "") }

        $EscapedDesc = $Desc.Replace('"', '\"')
        $SkillContent = "---`nname: mori-$Name`ndescription: `"$EscapedDesc`"`n---`n`n"
        $SkillContent += ($RestOfLines -join "`n").Trim()

        $SkillFolder = "$SkillsDir\mori-$Name"
        New-Item -ItemType Directory -Force -Path $SkillFolder | Out-Null
        Write-Utf8File "$SkillFolder\SKILL.md" $SkillContent
        Write-Host "  Deployed skill: mori-$Name" -ForegroundColor Cyan
    }
}

# ---- Step 1: MCP config for Cursor ----
Write-Host "[1/3] Configuring MCP server..." -ForegroundColor Yellow
$CursorMcpPath = "$CursorDir\mcp.json"
$McpConfig = Get-MoriMcpConfig
Merge-McpFile -Path $CursorMcpPath -MoriServer $McpConfig.mcpServers.mori

# ---- Step 2: Event capture hooks ----
Write-Host "[2/3] Setting up event capture hooks..." -ForegroundColor Yellow
$ClaudeDir = "$env:USERPROFILE\.claude"
$HooksFile = "$ClaudeDir\settings.json"

# Deploy the shipper script
$ShipperSrc = "$PSScriptRoot\mori-ship-event.ps1"
$ShipperDst = "$ClaudeDir\mori-ship-event.ps1"
New-Item -ItemType Directory -Force -Path $ClaudeDir | Out-Null
if (Test-Path $ShipperSrc) {
    Copy-Item -Path $ShipperSrc -Destination $ShipperDst -Force
    Write-Host "  Deployed mori-ship-event.ps1 to $ClaudeDir" -ForegroundColor Cyan
} else {
    Write-Host "  Warning: mori-ship-event.ps1 not found alongside installer - hooks will not work correctly." -ForegroundColor Yellow
}

$HooksConfig = Get-MoriHooksConfig

if (-not (Test-Path $HooksFile)) {
    Write-Utf8File $HooksFile ($HooksConfig | ConvertTo-Json -Depth 10)
    Write-Host "  Created $HooksFile with Mori event capture hooks" -ForegroundColor Cyan
} else {
    $rawContent = Get-Content $HooksFile -Raw -Encoding UTF8
    if ($rawContent -like "*mori-ship-event.ps1*") {
        Write-Host "  Skipped  -  $HooksFile already has Mori shipper hooks" -ForegroundColor Cyan
    } else {
        Merge-HooksFile -Path $HooksFile -HooksConfig $HooksConfig
        Write-Host "  Updated $HooksFile (replaced legacy or missing Mori hooks)" -ForegroundColor Cyan
    }
}

# ---- Step 3: Deploy skills ----
Write-Host "[3/3] Deploying skills..." -ForegroundColor Yellow
$SkillsDir = "$ClaudeDir\skills"
Deploy-Skills -SkillsDir $SkillsDir

Write-Host "`nMori  -  Cursor Bridge installation complete!" -ForegroundColor Green
Write-Host ""
Write-Host "--- Post-Install Steps ---"
Write-Host ""
Write-Host "1. Enable Third-party skills in Cursor:"
Write-Host "   Settings -> Rules, Skills, Subagents -> Enable third-party skills"
Write-Host ""
Write-Host "2. Restart Cursor for changes to take effect."
Write-Host ""
Write-Host "3. Verify:"
Write-Host "   - Open Cursor Agent, type /brief -- shared memories should load"
Write-Host "   - Run: curl $MoriUrl/health"
Write-Host ""
Write-Host "No Claude Code required - Mori creates ~/.claude/settings.json and"
Write-Host "~/.claude/skills/ for you if they do not already exist."
Write-Host ""
Write-Host "Hook failures are logged to: $env:TEMP\mori-hook.log"
