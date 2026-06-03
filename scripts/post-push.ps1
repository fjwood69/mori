# Mori post-push hook — Windows equivalent of post-push.sh
# Install: Copy-Item scripts\post-push.ps1 .git\hooks\post-push.ps1
# Or use:  .\scripts\install-git-hooks.ps1

$MoriUrl = $env:MORI_URL ?? "http://localhost:8968"
$ApiKey  = $env:MORI_API_KEY ?? ""
$Client  = $env:MORI_CLIENT ?? $env:COMPUTERNAME ?? "unknown"

$Repo    = Split-Path (git rev-parse --show-toplevel 2>$null) -Leaf
$Branch  = git branch --show-current 2>$null
$Sha     = git rev-parse --short HEAD 2>$null
$Message = git log -1 --pretty=%s 2>$null
$Remote  = if ($args[0]) { $args[0] } else { "origin" }

$Payload = @{
    hook_event_name = "GitPush"
    session_id      = $Sha
    repo            = $Repo
    branch          = $Branch
    sha             = $Sha
    message         = $Message
    remote          = $Remote
    client          = $Client
} | ConvertTo-Json -Compress

$Headers = @{ "Content-Type" = "application/json" }
if ($ApiKey) { $Headers["X-Api-Key"] = $ApiKey }

try {
    Invoke-RestMethod -Uri "$MoriUrl/api/events/raw?client=$([Uri]::EscapeDataString($Client))" `
        -Method POST -Headers $Headers -Body $Payload -TimeoutSec 5 | Out-Null
} catch { }  # Never block a push

# ── Git commit ingestion ──────────────────────────────────────────────────────

if ($ApiKey) {
    try {
        # Fetch the git watermark (last ingested commit SHA for this repo)
        $WatermarkResponse = Invoke-RestMethod `
            -Uri "$MoriUrl/api/dream/state?key=git_watermark_$Repo" `
            -Headers @{ "X-Api-Key" = $ApiKey } `
            -TimeoutSec 5 `
            -ErrorAction SilentlyContinue
        $Watermark = $WatermarkResponse.value
    } catch { $Watermark = $null }

    $Range = if ($Watermark) { "${Watermark}..HEAD" } else { "HEAD~20..HEAD" }

    # Collect commits oldest-first using field separator 0x1f
    $GitLines = git log --reverse $Range --format="%H%x1f%h%x1f%s%x1f%an%x1f%aI" 2>$null

    $Commits = @()
    foreach ($Line in ($GitLines -split "`n")) {
        $Line = $Line.Trim()
        if (-not $Line) { continue }
        $Parts = $Line -split [char]0x1f
        if ($Parts.Count -lt 5) { continue }
        $Commits += @{
            sha       = $Parts[0].Trim()
            short_sha = $Parts[1].Trim()
            subject   = $Parts[2].Trim()
            author    = $Parts[3].Trim()
            timestamp = $Parts[4].Trim()
        }
    }

    if ($Commits.Count -gt 0) {
        $IngestPayload = @{
            repo    = $Repo
            branch  = $Branch
            commits = $Commits
            pusher  = $Client
        } | ConvertTo-Json -Compress -Depth 4

        try {
            $Result = Invoke-RestMethod `
                -Uri "$MoriUrl/api/ingest/git" `
                -Method POST `
                -Headers @{ "X-Api-Key" = $ApiKey; "Content-Type" = "application/json" } `
                -Body $IngestPayload `
                -TimeoutSec 10
            if ($Result.ingested -gt 0) {
                Write-Host "[mori] ingested $($Result.ingested) commit(s) from $Repo"
            }
        } catch { }  # Never block a push
    }
}

exit 0
