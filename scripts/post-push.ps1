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

exit 0
