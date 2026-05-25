param(
    [Parameter(Mandatory)][string]$MoriUrl,
    [string]$Client = $env:COMPUTERNAME,
    [string]$ApiKey = "",
    [ValidateSet("raw", "precompact")][string]$Mode = "raw"
)

$ErrorActionPreference = "SilentlyContinue"

# Read event JSON from stdin
try {
    $body = [Console]::In.ReadToEnd()
} catch {
    exit 0
}

if ([string]::IsNullOrWhiteSpace($body)) { exit 0 }

# Build endpoint URL
$endpoint = if ($Mode -eq "precompact") { "precompact" } else { "events/raw" }
$uri = "$($MoriUrl.TrimEnd('/'))/api/${endpoint}?client=$([Uri]::EscapeDataString($Client))"

# Build headers
$headers = @{ "Content-Type" = "application/json" }
if ($ApiKey) { $headers["X-Api-Key"] = $ApiKey }

try {
    $null = Invoke-RestMethod -Uri $uri -Method POST -Body $body -Headers $headers -TimeoutSec 10
} catch {
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    try {
        Add-Content -Path "$env:TEMP\mori-hook.log" -Value "$timestamp [mori-ship] $Mode $uri : $_"
    } catch { }
}

exit 0
