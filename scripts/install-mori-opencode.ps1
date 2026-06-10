<#
.SYNOPSIS
    Windows installer script for Mori — OpenCode bridge (PowerShell 5.1+)

.DESCRIPTION
    Installs the Mori TypeScript plugin for OpenCode, wires the MCP server
    config, and deploys Mori slash-command skills.

    The API key is the BARE secret from MORI_API_KEYS — never "name:secret".

.PARAMETER MoriUrl
    Mori server base URL (default: http://localhost:8968)

.PARAMETER ApiKey
    Bare API key from MORI_API_KEYS (not "name:secret")

.PARAMETER ClientName
    Client name for event tagging (default: $env:COMPUTERNAME)

.PARAMETER ProjectScoped
    Install to .opencode\plugins\mori\ in the current directory instead of global

.PARAMETER Force
    Proceed even if the server health check fails

.PARAMETER Doctor
    Run connectivity/config checks only (no changes)

.PARAMETER UpgradeSkills
    Refresh mori skills from repo skills\ directory

.EXAMPLE
    .\scripts\install-mori-opencode.ps1 -MoriUrl http://10.0.0.10:8968 -ApiKey abcdef1234...

.EXAMPLE
    .\scripts\install-mori-opencode.ps1 -Doctor

.EXAMPLE
    .\scripts\install-mori-opencode.ps1 -MoriUrl http://server:8968 -ApiKey abc -Force
#>

param(
    [string]$MoriUrl     = "http://localhost:8968",
    [string]$ApiKey      = "",
    [string]$ClientName  = "",
    [switch]$ProjectScoped,
    [switch]$Force,
    [switch]$Doctor,
    [switch]$UpgradeSkills
)

$ErrorActionPreference = "Stop"
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot   = Split-Path -Parent $ScriptDir
$PluginSrc  = Join-Path $RepoRoot "plugins\mori\opencode"

function Write-Utf8File {
    param([string]$Path, [string]$Content)
    $dir = Split-Path $Path -Parent
    if ($dir) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    [System.IO.File]::WriteAllText($Path, $Content, (New-Object System.Text.UTF8Encoding $false))
}

function Get-OpenCodeConfigDir {
    $xdg = $env:XDG_CONFIG_HOME
    if ($xdg) { return Join-Path $xdg "opencode" }
    if ($env:APPDATA) { return Join-Path $env:APPDATA "opencode" }
    return Join-Path $env:USERPROFILE ".config\opencode"
}

function Merge-OpenCodeMcp {
    param([string]$ConfigPath, [string]$Url, [string]$Key)
    $moriEntry = [ordered]@{
        type    = "remote"
        url     = "$Url/mcp"
        headers = [ordered]@{ "x-api-key" = if ($Key) { $Key } else { "YOUR-64-CHAR-BARE-SECRET" } }
    }
    $config = @{}
    if (Test-Path $ConfigPath) {
        try { $config = Get-Content $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json -AsHashtable }
        catch { $config = @{} }
    }
    if (-not $config.ContainsKey("mcpServers")) { $config["mcpServers"] = @{} }
    $config["mcpServers"]["mori"] = $moriEntry

    $dir = Split-Path $ConfigPath -Parent
    if ($dir) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    Write-Utf8File $ConfigPath ($config | ConvertTo-Json -Depth 10)
    Write-Host "  Updated $ConfigPath" -ForegroundColor Cyan
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
        $Dest = Join-Path $DestDir $SkillDir.Name
        $DestFile = Join-Path $Dest "SKILL.md"
        if ((Test-Path $DestFile) -and -not $Upgrade) {
            Write-Host "  Skipped existing skill: $($SkillDir.Name) (use -UpgradeSkills to refresh)" -ForegroundColor Yellow
            continue
        }
        New-Item -ItemType Directory -Force -Path $Dest | Out-Null
        Copy-Item $SkillFile $DestFile -Force
        $action = if (Test-Path $DestFile) { "Updated" } else { "Deployed" }
        Write-Host "  $action skill: $($SkillDir.Name)" -ForegroundColor Cyan
        $count++
    }
    if ($count -eq 0 -and -not $Upgrade) {
        Write-Host "  No new skills deployed (use -UpgradeSkills to refresh)" -ForegroundColor Cyan
    }
}

function Invoke-MoriDoctor {
    param([string]$Url, [string]$Key)
    $errors = 0
    $ConfigDir = Get-OpenCodeConfigDir
    $GlobalPlugin = Join-Path $ConfigDir "plugins\mori"
    $ProjectPlugin = ".opencode\plugins\mori"

    Write-Host "Mori OpenCode doctor" -ForegroundColor Cyan
    Write-Host ("─" * 40) -ForegroundColor Cyan

    if (Test-Path $GlobalPlugin) {
        Write-Host "  OK   Global plugin: $GlobalPlugin" -ForegroundColor Green
    } elseif (Test-Path $ProjectPlugin) {
        Write-Host "  OK   Project plugin: $ProjectPlugin" -ForegroundColor Green
    } else {
        Write-Host "  FAIL Plugin not found in $GlobalPlugin or $ProjectPlugin" -ForegroundColor Red
        $errors++
    }

    $ConfigPath = Join-Path $ConfigDir "opencode.json"
    if (Test-Path $ConfigPath) {
        try {
            $cfg = Get-Content $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($cfg.mcpServers.mori) {
                Write-Host "  OK   MCP entry in $ConfigPath" -ForegroundColor Green
                $moriUrl = $cfg.mcpServers.mori.url
                if ($moriUrl) {
                    Write-Host "       mori URL: $moriUrl"
                    if ($moriUrl -match "^(https?://[^/]+)") { $Url = $Matches[1] }
                }
            } else {
                Write-Host "  FAIL No 'mori' in mcpServers in $ConfigPath" -ForegroundColor Red
                $errors++
            }
        } catch {
            Write-Host "  FAIL Could not parse $ConfigPath" -ForegroundColor Red
            $errors++
        }
    } else {
        Write-Host "  WARN opencode.json not found at $ConfigPath (may be project-scoped)" -ForegroundColor Yellow
    }

    if ($env:MORI_SERVER_URL) {
        Write-Host "  OK   MORI_SERVER_URL = $env:MORI_SERVER_URL" -ForegroundColor Green
    } else {
        Write-Host "  WARN MORI_SERVER_URL not set as user env var" -ForegroundColor Yellow
    }
    if ($env:MORI_API_KEY) {
        Write-Host "  OK   MORI_API_KEY set" -ForegroundColor Green
    } else {
        Write-Host "  WARN MORI_API_KEY not set as user env var" -ForegroundColor Yellow
    }

    if ($Url) {
        Write-Host "  →    health check $Url/health ... " -NoNewline
        try {
            $headers = @{}
            if ($Key) { $headers["x-api-key"] = $Key }
            $resp = Invoke-WebRequest -Uri "$Url/health" -Headers $headers -UseBasicParsing -TimeoutSec 5
            if ($resp.StatusCode -eq 200) {
                Write-Host "OK" -ForegroundColor Green
            } else {
                Write-Host "FAIL (HTTP $($resp.StatusCode))" -ForegroundColor Red
                $errors++
            }
        } catch {
            Write-Host "FAIL ($_)" -ForegroundColor Red
            $errors++
        }
    }

    $SkillsDir = Join-Path $ConfigDir "skills"
    $SkillNames = @("brief", "dream", "pensieve", "consult", "wrap")
    if (Test-Path $SkillsDir) {
        $found = $SkillNames | Where-Object { Test-Path (Join-Path $SkillsDir "$_\SKILL.md") }
        if ($found) {
            Write-Host "  OK   Skills ($($found.Count)/$($SkillNames.Count)): $($found -join ', ')" -ForegroundColor Green
        } else {
            Write-Host "  WARN No mori skills in $SkillsDir" -ForegroundColor Yellow
        }
    } else {
        $claudeSkills = Join-Path $env:USERPROFILE ".claude\skills"
        $found = $SkillNames | Where-Object { Test-Path (Join-Path $claudeSkills "$_\SKILL.md") }
        if ($found) {
            Write-Host "  OK   Skills via .claude\skills\ ($($found.Count)/$($SkillNames.Count)): $($found -join ', ')" -ForegroundColor Green
        } else {
            Write-Host "  WARN No mori skills found (run with -UpgradeSkills to deploy)" -ForegroundColor Yellow
        }
    }

    Write-Host ""
    if ($errors) {
        Write-Host "Doctor: $errors error(s) — see above." -ForegroundColor Red
        exit 1
    }
    Write-Host "Doctor: all critical checks passed. Restart OpenCode if MCP was just installed." -ForegroundColor Green
    exit 0
}

# ── Doctor mode ───────────────────────────────────────────────────────────────

if ($Doctor) {
    if ([string]::IsNullOrWhiteSpace($ClientName)) { $ClientName = $env:COMPUTERNAME }
    if ($MoriUrl.EndsWith("/")) { $MoriUrl = $MoriUrl.TrimEnd("/") }
    Invoke-MoriDoctor -Url $MoriUrl -Key $ApiKey
}

# ── Interactive wizard ────────────────────────────────────────────────────────

$Headless = $PSBoundParameters.ContainsKey("MoriUrl") -and $PSBoundParameters.ContainsKey("ApiKey")
if (-not $Headless) {
    Write-Host "--- Mori — OpenCode Bridge Setup Wizard ---" -ForegroundColor Cyan
    Write-Host ""

    if (-not $PSBoundParameters.ContainsKey("MoriUrl")) {
        $p = Read-Host "Enter Mori Server URL [http://localhost:8968]"
        if ($p) { $MoriUrl = $p }
    }
    if (-not $PSBoundParameters.ContainsKey("ApiKey")) {
        $ApiKey = Read-Host "Enter Mori API Key (bare secret, Enter to skip)"
    }
    if ([string]::IsNullOrWhiteSpace($ClientName)) {
        $ClientName = $env:COMPUTERNAME
        $p = Read-Host "Enter Client Name [$ClientName]"
        if ($p) { $ClientName = $p }
    }
    if (-not $PSBoundParameters.ContainsKey("ProjectScoped")) {
        Write-Host ""
        $scope = Read-Host "Install globally or project-scoped? [G/p]"
        if ($scope -match "^[pP]") { $ProjectScoped = $true }
    }
}

if ($MoriUrl.EndsWith("/")) { $MoriUrl = $MoriUrl.TrimEnd("/") }
if ($MoriUrl -notmatch "^https?://") { Write-Error "Invalid Mori URL"; exit 1 }
if ([string]::IsNullOrWhiteSpace($ClientName)) { $ClientName = $env:COMPUTERNAME }

# ── Resolve install directories ───────────────────────────────────────────────

$ConfigDir = Get-OpenCodeConfigDir
if ($ProjectScoped) {
    $PluginDest = ".opencode\plugins\mori"
    $OpenCodeJson = "opencode.json"
} else {
    $PluginDest = Join-Path $ConfigDir "plugins\mori"
    $OpenCodeJson = Join-Path $ConfigDir "opencode.json"
}
$SkillsDest = Join-Path $ConfigDir "skills"

# ── Health check ──────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "Validating connection to Mori server at $MoriUrl..." -ForegroundColor Yellow
$Connected = $false
try {
    $headers = @{}
    if ($ApiKey) { $headers["x-api-key"] = $ApiKey }
    $resp = Invoke-WebRequest -Uri "$MoriUrl/health" -Headers $headers -UseBasicParsing -TimeoutSec 5
    if ($resp.StatusCode -eq 200) {
        Write-Host "Connection successful! Mori server health check: ok" -ForegroundColor Green
        $Connected = $true
    }
} catch {
    Write-Host "Warning: Could not connect to Mori server at $MoriUrl" -ForegroundColor Yellow
}

if (-not $Connected -and -not $Force) {
    $confirm = Read-Host "Health check failed. Proceed anyway? (y/N)"
    if ($confirm -notmatch "^[yY]") {
        Write-Host "Installation aborted."
        exit 1
    }
}

Write-Host ""
Write-Host "Setting up Mori — OpenCode Bridge..." -ForegroundColor Green

$PluginOk = $false
$McpOk = $false

# ── Step 1: Copy plugin files ─────────────────────────────────────────────────

Write-Host "[1/3] Installing plugin..." -ForegroundColor Yellow
if (-not (Test-Path $PluginSrc)) {
    Write-Host "  Error: plugin source not found at $PluginSrc" -ForegroundColor Red
    Write-Host "  Run this script from the mori repo root." -ForegroundColor Red
    exit 1
}

New-Item -ItemType Directory -Force -Path $PluginDest | Out-Null
Copy-Item -Path "$PluginSrc\*" -Destination $PluginDest -Recurse -Force

# Write mcp.json with actual values
$keyOrPlaceholder = if ($ApiKey) { $ApiKey } else { "YOUR-64-CHAR-BARE-SECRET" }
$mcpContent = @"
{
  "mcpServers": {
    "mori": {
      "type": "remote",
      "url": "$MoriUrl/mcp",
      "headers": {
        "x-api-key": "$keyOrPlaceholder"
      }
    }
  }
}
"@
Write-Utf8File (Join-Path $PluginDest "mcp.json") $mcpContent
Write-Host "  Plugin installed to $PluginDest" -ForegroundColor Cyan
$PluginOk = $true

# ── Step 2: Merge MCP server into opencode.json ───────────────────────────────

Write-Host "[2/3] Configuring MCP server..." -ForegroundColor Yellow
try {
    Merge-OpenCodeMcp -ConfigPath $OpenCodeJson -Url $MoriUrl -Key $ApiKey
    $McpOk = $true
} catch {
    Write-Host "  Error: $_" -ForegroundColor Red
}

# ── Step 3: Deploy skills ─────────────────────────────────────────────────────

Write-Host "[3/3] Deploying skills..." -ForegroundColor Yellow
try {
    Deploy-MoriSkills -SourceDir (Join-Path $RepoRoot "skills") -DestDir $SkillsDest -Upgrade:$UpgradeSkills
} catch {
    Write-Host "  Warning: skill deploy had issues (skills may still work via .claude\skills\)" -ForegroundColor Yellow
}

# ── Set user environment variables ────────────────────────────────────────────

if ($ApiKey) {
    $existingUrl = [System.Environment]::GetEnvironmentVariable("MORI_SERVER_URL", "User")
    if ($existingUrl) {
        Write-Host "  Note: MORI_SERVER_URL already set as user env var — skipping" -ForegroundColor Yellow
    } else {
        [System.Environment]::SetEnvironmentVariable("MORI_SERVER_URL", $MoriUrl,  "User")
        [System.Environment]::SetEnvironmentVariable("MORI_API_KEY",    $ApiKey,   "User")
        Write-Host "  User environment variables set (MORI_SERVER_URL, MORI_API_KEY)" -ForegroundColor Cyan
        Write-Host "  Restart your terminal or OpenCode to pick these up" -ForegroundColor Cyan
    }
} else {
    Write-Host "  Note: no API key provided — set MORI_SERVER_URL and MORI_API_KEY as user env vars manually" -ForegroundColor Yellow
}

# ── Summary ───────────────────────────────────────────────────────────────────

Write-Host ""
if ($PluginOk -and $McpOk) {
    Write-Host "Mori — OpenCode Bridge installation complete!" -ForegroundColor Green
} else {
    Write-Host "Installation FAILED — see errors above." -ForegroundColor Red
}

Write-Host @"

--- Post-Install Steps ---

1. Restart OpenCode to activate the plugin
2. Confirm MCP: opencode mcp list  (mori should appear)
3. Verify: powershell -File scripts\install-mori-opencode.ps1 -Doctor -MoriUrl "$MoriUrl"
4. In a session: /brief  (memory comes from the server, not local disk)

Hook failures: $env:TEMP\mori-hook.log
Shared memory lives on the Mori server, not your local disk.

"@

if (-not $PluginOk -or -not $McpOk) { exit 1 }
