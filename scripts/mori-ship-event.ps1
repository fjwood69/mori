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

# ---- Stop-event enrichment ---------------------------------------------------
# On Stop, attach a bounded, base64-encoded tail of the session transcript so the
# server can extract the turn's assistant reasoning. PowerShell-native JSON + IO
# (no python/jq). Any failure falls through to shipping the original body.
if ($Mode -eq "raw") {
    try {
        $obj = $body | ConvertFrom-Json
        if ($obj.hook_event_name -eq "Stop" -and $obj.transcript_path -and (Test-Path -LiteralPath $obj.transcript_path)) {
            $tailLines = Get-Content -LiteralPath $obj.transcript_path -Tail 120 -Encoding utf8 -ErrorAction Stop
            $tailText = ($tailLines -join "`n")
            if ($tailText.Length -gt 65536) { $tailText = $tailText.Substring($tailText.Length - 65536) }
            $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($tailText))
            $obj | Add-Member -NotePropertyName "transcript_tail_b64" -NotePropertyValue $b64 -Force
            $body = $obj | ConvertTo-Json -Compress -Depth 20
        }
    } catch { }  # never block the hook
}

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
