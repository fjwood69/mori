# Install Mori git hooks into a repository.
# Usage: .\scripts\install-git-hooks.ps1 [-RepoDir C:\path\to\repo]

param(
    [string]$RepoDir = "."
)

$ScriptDir = Split-Path $MyInvocation.MyCommand.Path
$HooksDir  = Join-Path $RepoDir ".git\hooks"

if (-not (Test-Path $HooksDir)) {
    Write-Error "Not a git repository: $HooksDir not found. Pass -RepoDir <path> if needed."
    exit 1
}

Copy-Item "$ScriptDir\post-push.ps1" "$HooksDir\post-push.ps1" -Force
Write-Host "Installed post-push hook to $HooksDir\post-push.ps1"
Write-Host ""
Write-Host "Set these environment variables (e.g. in `$PROFILE):"
Write-Host "  `$env:MORI_URL      = 'http://localhost:8968'  # default"
Write-Host "  `$env:MORI_API_KEY  = 'your-key'               # if auth is enabled"
Write-Host "  `$env:MORI_CLIENT   = `$env:COMPUTERNAME        # default — override if needed"
