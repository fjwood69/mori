# Mori smoke test — Windows equivalent of smoke-test.sh
#
# Usage:
#   .\scripts\smoke-test.ps1 [-Strict] [-Url http://<mori-host>:8968]
#   $env:MORI_URL="http://<mori-host>:8968"; $env:MORI_API_KEY="xxx"; .\scripts\smoke-test.ps1
#
# -Strict: treat 'degraded' as failure (use for GCE post-deploy confirmation)

param(
    [switch]$Strict,
    [string]$Url = ($env:MORI_URL ?? "http://localhost:8968")
)

$ApiKey = $env:MORI_API_KEY ?? ""

Write-Host ""
Write-Host "Mori smoke test → $Url" -ForegroundColor White
Write-Host "──────────────────────────────────────────"

$Headers = @{ "Accept" = "application/json" }
if ($ApiKey) { $Headers["X-Api-Key"] = $ApiKey }

try {
    $Response = Invoke-RestMethod -Uri "$Url/api/smoke" -Headers $Headers -TimeoutSec 30
} catch {
    Write-Host "✗ Could not reach $Url/api/smoke" -ForegroundColor Red
    Write-Host "  Is Mori running? Is MORI_API_KEY set?"
    exit 1
}

$Detail = @{
    db_read         = { param($c) "$($c.memory_count) memories" }
    event_log       = { param($c) "$($c.total_events) events" }
    event_roundtrip = { param($c) "$($c.before) → $($c.after)" }
    dream_watermark = { param($c) "watermark=$($c.watermark), undreamed=$($c.undreamed)" }
    msg_daemon      = { param($c) "$($c.msg_count) messages" }
}

$Checks = $Response.checks.PSObject.Properties
foreach ($prop in $Checks) {
    $key    = $prop.Name
    $check  = $prop.Value
    $status = $check.status
    $extra  = if ($Detail.ContainsKey($key)) { & $Detail[$key] $check } else { "" }
    $err    = $check.error ?? ""

    if ($status -eq "ok") {
        Write-Host ("✓ {0,-20} {1}" -f $key, $extra) -ForegroundColor Green
    } else {
        Write-Host ("✗ {0,-20} {1}" -f $key, ($err ? $err : "failed")) -ForegroundColor Red
    }
}

Write-Host "──────────────────────────────────────────"

$Overall = $Response.status
switch ($Overall) {
    "ok" {
        Write-Host "Status: OK — mori is healthy" -ForegroundColor Green
        exit 0
    }
    "degraded" {
        Write-Host "Status: DEGRADED — NATS/ingestion failed (non-critical)" -ForegroundColor Yellow
        exit ($Strict ? 1 : 0)
    }
    default {
        Write-Host "Status: FAILED — critical checks failed" -ForegroundColor Red
        exit 1
    }
}
