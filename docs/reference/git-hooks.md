# Getting Started — Git Push Notifications

When you push to a git repo, Mori publishes a `GitPush` event to the NATS message bus so every other active instance knows commits are available. This prevents stale-code work, duplicate effort, and surprise merge conflicts across devices.

---

## How it works

```
git push
  └─▶ .git/hooks/post-push
        └─▶ POST /api/events/raw  (fire-and-forget)
              └─▶ Mori event log
                    └─▶ NATS cc.<device>  (immediate)
                          └─▶ /brief → NATS replay surfaces it on every other device
```

The hook never blocks a push — if Mori is unreachable the push completes normally and the notification is silently dropped.

---

## Prerequisites

- Mori server running and reachable (default: `http://localhost:8968`)
- `curl` (Linux/macOS) or PowerShell 7+ (Windows)
- Hook installed per-repo (git hooks are not global)

---

## Installation

### Linux / macOS

```bash
# From the mori repo root, install into the current repo:
./scripts/install-git-hooks.sh

# Install into a different repo:
./scripts/install-git-hooks.sh --repo ~/bifrost
./scripts/install-git-hooks.sh --repo ~/dotfiles
./scripts/install-git-hooks.sh --repo ~/ai-stack
```

### Windows (PowerShell)

```powershell
# From the mori repo root:
.\scripts\install-git-hooks.ps1

# Install into a different repo:
.\scripts\install-git-hooks.ps1 -RepoDir C:\My Code\bifrost
.\scripts\install-git-hooks.ps1 -RepoDir C:\My Code\dotfiles
```

> **Windows note:** Git for Windows runs bash hooks automatically. The `.ps1` hook is installed alongside and requires a thin bash wrapper if your git uses bash hooks only. See [Manual install](#manual-install) below if needed.

---

## Environment variables

Set these in `~/.bashrc`, `~/.zshrc`, or `$PROFILE` (PowerShell):

| Variable | Default | Description |
|----------|---------|-------------|
| `MORI_URL` | `http://localhost:8968` | Mori server URL |
| `MORI_API_KEY` | *(empty)* | API key — only needed if auth is enabled |
| `MORI_CLIENT` | `$(hostname)` | Client identifier shown in NATS messages |

---

## Manual install

If you prefer not to use the installer:

**Linux/macOS:**
```bash
cp scripts/post-push.sh /path/to/repo/.git/hooks/post-push
chmod +x /path/to/repo/.git/hooks/post-push
```

**Windows:**
```powershell
Copy-Item scripts\post-push.ps1 C:\path\to\repo\.git\hooks\post-push.ps1
```

---

## Recommended repos

Install in each repo where cross-device push awareness matters:

- `mori`
- `bifrost`
- `dotfiles`
- `ai-stack`

---

## Verification

```bash
# 1. Make a commit and push
git commit --allow-empty -m "test: NATS push notification"
git push

# 2. Check NATS on the same or another device
/nats sub
# → [your-hostname] GitPush: mori/main abc1234 — test: NATS push notification

# 3. Confirm event was logged
curl http://localhost:8968/api/events/health
# → {"status":"ok","total_events":<incremented>}
```

---

## What other instances see

The push surfaces in two places:

- **`/nats sub`** — immediate, shows the one-liner from the pushing device
- **`/brief`** — NATS replay at session start picks up recent pushes from the last 7 days
