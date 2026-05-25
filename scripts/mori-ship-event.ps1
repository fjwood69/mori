param(
    [Parameter(Mandatory)][string]$MoriUrl,
    [string]$Client = $env:COMPUTERNAME,
    [string]$ApiKey = "",
    [ValidateSet("raw", "precompact")][string]$Mode = "raw"
)

# Read event JSON from stdin
$body = $null
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

# POST - suppress only the REST call errors
$ErrorActionPreference = "Stop"
try {
    $null = Invoke-RestMethod -Uri $uri -Method POST -Body $body -Headers $headers -TimeoutSec 10
} catch {
    $logPath = "$env:TEMP\mori-hook.log"
    try {
        # Rotate log if > 100 KB
        if ((Test-Path $logPath) -and (Get-Item $logPath).Length -gt 102400) {
            Move-Item $logPath "$logPath.old" -Force
        }
        $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        Add-Content -Path $logPath -Value "$timestamp [mori-ship] $Mode $uri : $_"
    } catch { }
}

exit 0
