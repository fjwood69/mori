# Getting Started — Git Push Hooks

When you push to a git repo, Mori does two things automatically:

1. **Publishes a `GitPush` event** to the NATS message bus so every other active instance knows commits are available.
2. **Ingests commit messages** into the memory store as working-tier project memories — so the dream pipeline can extract decisions, patterns, and context from what was actually built.

---

## How it works

```
git push
  └─▶ .git/hooks/post-push
        ├─▶ POST /api/events/raw        (fire-and-forget NATS event)
        │     └─▶ NATS cc.<device>      (surfaces in /brief + /nats sub)
        │
        └─▶ GET  /api/git/watermark?repo=X&ref=Y   (per-branch watermark)
              └─▶ POST /api/git/ingest                     (batch commit ingest)
                    └─▶ working-tier memories tagged project:<repo>
                          └─▶ dream pipeline promotes to canonical over time
```

The hook never blocks a push — if Mori is unreachable both paths silently drop.

---

## Prerequisites

- Mori server running and reachable (default: `http://localhost:8968`)
- `curl` + `python3` (Linux/macOS) or PowerShell 7+ (Windows)
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

`MORI_URL` and `MORI_CLIENT` should go in `~/.bashrc` / `~/.zshrc` / `$PROFILE`. `MORI_API_KEY` is sourced directly from `~/.claude/.secrets` at push time — no need to export it to your shell.

| Variable | Default | Description |
|----------|---------|-------------|
| `MORI_URL` | `http://localhost:8968` | Mori server URL |
| `MORI_CLIENT` | `$(hostname)` | Client identifier shown in NATS messages and memory tags |

### API key — `~/.claude/.secrets`

The hook reads `MORI_API_KEY` from `~/.claude/.secrets` automatically. Add a line in this format:

```
MORI_API_KEY_<HOSTNAME_UPPER>=your-secret-key
```

For example, on a host named `dev-laptop`:

```
MORI_API_KEY_LAPTOP=<your-secret-key>
```

The hook derives the key name from `hostname`, stripping any leading location-style
prefix (e.g. `office-`, `home-`). If no hostname-specific key is found it falls back to
the first `MORI_API_KEY_*` line. If `~/.claude/.secrets` is absent, the push event is
still sent (NATS path), but commit ingestion is skipped.

You can also set `MORI_API_KEY` directly in the environment to override the `.secrets` lookup.

---

## What gets ingested

Each commit since the last ingested SHA (per repo + branch) is written as a memory:

```
title:  fix: Postgres sequence reset in start-uat.sh
body:   [mori/main] fix: Postgres sequence reset in start-uat.sh

        After pg_dump restore, primary key sequences must be reset.

        Commit abc1234 by Alex Dev at 2026-06-03T09:15:00Z
tags:   ["commit", "project:mori", "pusher:dev-laptop"]
tier:   working
```

The dream pipeline reviews these on its next run. Significant commits (architectural decisions, major fixes) are promoted to `canonical`; routine maintenance ages out naturally.

**Watermarks are per `(repo, ref)`** — pushing to `main` and `feature/x` maintains independent ingestion state. Re-pushing the same SHAs is idempotent.

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

Install in each repo where cross-device push awareness and memory capture matters:

- `mori`
- `bifrost`
- `dotfiles`
- `ai-stack`

---

## Verification

```bash
# 1. Make a commit and push
git commit --allow-empty -m "test: git hook smoke test"
git push

# Expected terminal output:
# [mori] ingested 1 commit(s) from mori/main

# 2. Check NATS on the same or another device
/nats sub
# → [your-hostname] GitPush: mori/main abc1234 — test: git hook smoke test

# 3. Verify the memory was written
/pensieve "git hook smoke test"
# → Memory: commit-mori-abc1234

# 4. Check the watermark advanced
curl http://localhost:8968/api/git/watermark?repo=mori\&ref=main \
  -H "X-Api-Key: $MORI_API_KEY"
# → {"repo":"mori","ref":"main","watermark":"<sha>"}
```

---

## What other instances see

The push surfaces in two places:

- **`/nats sub`** — immediate one-liner from the pushing device
- **`/brief`** — NATS replay at session start picks up recent pushes from the last 7 days
- **`/pensieve <topic>`** — commit memories appear once the dream pipeline has run
