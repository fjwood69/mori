# Mori post-push hook — Windows equivalent of post-push.sh
# Install: Copy-Item scripts\post-push.ps1 .git\hooks\post-push.ps1
# Or use:  .\scripts\install-git-hooks.ps1

$MoriUrl = $env:MORI_URL ?? "http://localhost:8968"
$Client  = $env:MORI_CLIENT ?? $env:COMPUTERNAME ?? "unknown"

# Resolve MORI_API_KEY: env var takes precedence; fall back to ~/.claude/.secrets
$ApiKey = $env:MORI_API_KEY
if (-not $ApiKey) {
    $SecretsFile = Join-Path $env:USERPROFILE ".claude\.secrets"
    if (-not (Test-Path $SecretsFile)) {
        $SecretsFile = Join-Path $env:USERPROFILE ".claude/.secrets"
    }
    if (Test-Path $SecretsFile) {
        # Derive key name from COMPUTERNAME (e.g. MY-PC → MORI_API_KEY_PC)
        $HostUpper = ($env:COMPUTERNAME -replace '^[^-]+-[^-]+-', '' -replace '-','_').ToUpper()
        $KeyName = "MORI_API_KEY_$HostUpper"
        $Line = Get-Content $SecretsFile | Where-Object { $_ -match "^$KeyName=" } | Select-Object -First 1
        if ($Line) {
            $ApiKey = $Line.Substring($KeyName.Length + 1)
        } else {
            # Fallback: first MORI_API_KEY_ line
            $Line = Get-Content $SecretsFile | Where-Object { $_ -match "^MORI_API_KEY_" } | Select-Object -First 1
            if ($Line) { $ApiKey = ($Line -split '=', 2)[1] }
        }
    }
}

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
        # Fetch per-ref watermark
        $WmResponse = Invoke-RestMethod `
            -Uri "$MoriUrl/api/git/watermark?repo=$([Uri]::EscapeDataString($Repo))&ref=$([Uri]::EscapeDataString($Branch))" `
            -Headers @{ "X-Api-Key" = $ApiKey } `
            -TimeoutSec 5 `
            -ErrorAction SilentlyContinue
        $Watermark = $WmResponse.watermark
    } catch { $Watermark = $null }

    # Use the watermark only if it resolves to a real commit (guards against
    # force-push, rebase, fresh clone, or a SHA from another machine).
    $WmValid = $false
    if ($Watermark) {
        git rev-parse --verify --quiet "$Watermark^{commit}" *>$null
        $WmValid = ($LASTEXITCODE -eq 0)
    }
    $Range = if ($WmValid) { "${Watermark}..HEAD" } else { "HEAD~20..HEAD" }

    # Collect commits with body (record separator \x1e, field separator \x1f)
    $GitOut = git log --reverse $Range --format="%H%x1f%h%x1f%s%x1f%b%x1e" 2>$null

    $Commits = @()
    foreach ($Entry in ($GitOut -split [char]0x1e)) {
        $Entry = $Entry.Trim()
        if (-not $Entry) { continue }
        $Parts = $Entry -split [char]0x1f, 4
        if ($Parts.Count -lt 3) { continue }
        $Commits += @{
            sha       = $Parts[0].Trim()
            short_sha = $Parts[1].Trim()
            subject   = $Parts[2].Trim()
            body      = if ($Parts.Count -gt 3) { $Parts[3].Trim() } else { "" }
        }
    }

    # Add author + timestamp
    if ($Commits.Count -gt 0) {
        $MetaLines = git log --reverse $Range --format="%H%x1f%an%x1f%aI" 2>$null
        $Meta = @{}
        foreach ($Line in ($MetaLines -split "`n")) {
            $P = $Line -split [char]0x1f, 3
            if ($P.Count -eq 3) { $Meta[$P[0].Trim()] = @{ author = $P[1].Trim(); timestamp = $P[2].Trim() } }
        }
        for ($i = 0; $i -lt $Commits.Count; $i++) {
            $m = $Meta[$Commits[$i].sha]
            $Commits[$i].author    = if ($m) { $m.author } else { "" }
            $Commits[$i].timestamp = if ($m) { $m.timestamp } else { "" }
        }

        $IngestPayload = @{
            repo    = $Repo
            ref     = $Branch
            commits = $Commits
            pusher  = $Client
        } | ConvertTo-Json -Compress -Depth 4

        try {
            $Result = Invoke-RestMethod `
                -Uri "$MoriUrl/api/git/ingest" `
                -Method POST `
                -Headers @{ "X-Api-Key" = $ApiKey; "Content-Type" = "application/json" } `
                -Body $IngestPayload `
                -TimeoutSec 10
            if ($Result.ingested -gt 0) {
                Write-Host "[mori] ingested $($Result.ingested) commit(s) from ${Repo}/${Branch}"
            }
        } catch { }  # Never block a push
    }
}

exit 0
