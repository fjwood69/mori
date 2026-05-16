# Moku — Shared Memory & Dream Pipeline for Claude Code

Moku is an MCP server that gives Claude Code persistent shared memory,
strategic advisory, session grounding, and a "dream" pipeline that
distills session activity into durable knowledge.

Works with any OpenAI-compatible provider. No homelab, no Anthropic
account, no Bifrost required — though those all work too.

---

## Core capabilities

### 1. Event Logging

Claude Code lifecycle hooks POST session events to `POST /api/events/raw`.
These events feed the dream pipeline.

```bash
# Minimal hook config — add to settings.json:
curl -sf -X POST 'http://localhost:8968/api/events/raw?client=my-hostname' \
  -H 'Content-Type: application/json' -d @-
```

Every Claude Code session emits lifecycle events — tool calls, prompts, errors,
stop reasons. Moku receives these via HTTP POST and stores them in an append-only
event log. This is the raw material everything else builds on.

**What it captures:**
- `PostToolUse` — tool name, input, output, errors
- `UserPromptSubmit` — the prompt text
- `Stop` / `SessionEnd` — stop reason, model used
- Session ID, client hostname, working directory, transcript path

**Components required:** Moku server only. No LLM provider needed.

**Config:** `MOKU_ADVISOR_API_KEY` for auth (empty = no auth, only reachable via
Tailscale LAN or localhost).

---

### 2. Persistent Memory

Memories live in a single SQLite database (`memories.db`) with:

- **Versioning** — every change creates a new version. View history, diff versions, rollback.
- **Attribution** — each memory tracks which session(s) and client(s) contributed.
- **Protection** — trusted dreamers write directly; others queue for approval.
- **Tagging** — memories are taggable (`security`, `architecture`, `decision`) for filtering.
- **Search** — keyword search across name, title, description, and body.

**Components required:** Moku server only. Memories persist in SQLite — no
external dependencies.

---

### 3. Dream Phase

Session events are captured via Claude Code lifecycle hooks (PostToolUse,
UserPromptSubmit, Stop). The dream pipeline reads events since the last
watermark, sends them to a configurable LLM, and writes extracted memories
back to the store.

```
Hook fires  →  POST /api/events/raw  →  SQLite events table
                                           ↓
                                    dream_run() reads since watermark
                                           ↓
                                    LLM distills events → structured memories
                                           ↓
                                    memories written to store (with attribution)
                                           ↓
                                    watermark advanced
```

Run it: `/dream` or `moku-dream_run`. Check state: `/dream --status`.

#### Stale knowledge & eviction

Dream produces three tiers of memory, each with a different lifecycle:

| Tier | Scope | Eviction |
|------|-------|----------|
| **Ephemeral** | Auto-generated session summaries | Auto-expire at session end unless explicitly saved |
| **Working** | Patterns, decisions, project context | Flagged for review after 30 days of no retrievals. Not deleted — surfaced via `pensieve --since 30d` for weekly triage |
| **Canonical** | Explicitly promoted by a trusted dreamer | Indefinite, but freshness-checked before injection via `/brief` |

**The freshness check** runs during `/brief` (session bootstrap). For each
canonical memory about dependencies, infrastructure, or tooling, a lightweight
validation prompt asks: *"Based on current project state, is this still accurate?
Answer YES, NO, or STALE."* STALE responses suppress injection and queue
the memory for review.

**Orphan scan** — `dream_run` also tracks retrieval recency. Memories not
retrieved in 30 days are flagged but preserved. Human reviews the queue,
not a batch delete.

This avoids the classic "persistent memory" failure mode where a patched
cluster's stale workaround poisons sessions for months.

**Components required:** Moku server + LLM provider (for the distillation model).
Config: `MOKU_DREAM_MODEL` (defaults to `MOKU_MODEL`, then `deepseek/deepseek-v4-flash`).

---

### 4. Session Context (`/brief`)

Moku uses **session grounding** rather than per-query RAG. `/brief` loads
shared memories, team standards, and dream pipeline state into context at
session start. From turn one, the model knows your security baseline,
coding conventions, and current project state — no retrieval needed.

Unresolved `/req` items also surface via `/brief` — a sticky note, not a project board.
No sync, no drift from JIRA or GH Projects.

```
# Starting a refactor — add a checklist:
/req add "Extract auth middleware" --project bifrost --pri high
/req add "Add rate limiting" --project bifrost --pri medium
/req add "Write migration guide" --project bifrost --pri low

# Check progress mid-session:
/req --project bifrost
→ 3 requirements, 1 in-progress, 2 pending

# Mark done as you go:
/req done req-bifrost-extract-auth-middleware

# Next session, /brief shows what's still open
```

When the standards corpus grows beyond one context window, run separate
Moku instances per namespace rather than adding a vector store:

```bash
# Retail team
docker run ... -e MOKU_STANDARDS_DIR=/standards/retail -p 8970:8968
# Energy team
docker run ... -e MOKU_STANDARDS_DIR=/standards/energy -p 8971:8968
```

#### Standards ingestion

Set `MOKU_STANDARDS_DIR` to a directory of `.md` files:

```
/path/to/standards/
  ethos/
    values-and-ethical-principles.md
  security/
    security-baseline.md
    pii-handling.md
  coding/
    python-style-guide.md
```

On startup, every `.md` file is imported as a protected memory with `type: standard`
and tags from its subdirectory. Standards are read-only to non-trusted dreamers.

Update without restarting: `moku-standards_reload` (trusted dreamers only).

**Components required:** Moku server + `/brief` skill. Config: `MOKU_STANDARDS_DIR`.

---

### 5. Strategic Code Review (`/consult`)

A configurable LLM receives your question plus optional file context and
returns strategic guidance. Supports focus areas (`general`, `architecture`,
`security`, `performance`, `style`) and depth levels (`quick`, `balanced`, `deep`).

When a specific focus is given (`--focus security`), relevant team standards
are auto-injected from memory — the advisor checks against your own baseline,
not generic advice.

Chain tool output into the advisor:
```
/consult "review this auth handler" --focus security --file src/auth.py --file snyk-report.json
```

**Components required:** Moku server + LLM provider. Config: `MOKU_MODEL`
(default `moonshotai/kimi-k2.6`).

---

### 6. Agent Delegation + NATS (`/nats`, `/update`)

#### Cross-device messaging (NATS)

Optional NATS JetStream integration for cross-device state-of-play messages.
Each device publishes session summaries; any device can replay the last 7 days.
Useful for awareness across a team or fleet of Claude Code instances.

#### Skill deployment (`/update`)

The `moku-update` tool generates install commands for skills and slash commands
across devices. It knows each device's profile layout and produces the right
shell commands — no manual copy-paste across machines:

| Device | Profiles |
|--------|----------|
| Linux | `.claude`, `.claude-sr`, `.claude-sub`, `.claude-api` |
| Windows | Same paths via `$env:USERPROFILE` |
| NUC | `.claude`, `.claude-jr`, `.claude-sub`, `.claude-api` |

Command output is base64-encoded to avoid shell quoting issues:

```
/update --twiggy --nats
→ compact PowerShell block that deploys to all 4 profiles
→ ask approval then execute — no copy-paste needed
```

This means pushing an updated skill to every Claude Code instance is a single
`/update` command away.

**Components required:** Moku server. NATS server for cross-device messaging
(optional).

---

### 7. Governance — Memory Quality & Validity

Memories accumulate over time. Without safeguards, they drift, conflict, or
accumulate noise. Moku has several mechanisms to maintain quality:

#### Trusted Dreamers

Certain client hostnames are designated as **trusted dreamers**. Only they can
directly modify protected memories. Writes from other instances are queued as
pending writes.

Configured via `MOKU_TRUSTED_DREAMERS` env var (comma-separated hostnames)
or in the `dreamer_config` table.

#### Protection

Any memory can be toggled protected via `moku-memory_protect`. When protected:
- Trusted dreamers write directly (no change in behaviour)
- Other instances' writes go to `pending_writes` for review
- `moku-memory_pending_list`, `moku-memory_approve`, `moku-memory_reject` manage the queue

#### Versioning & Rollback

Every write snapshots the previous state. You can:
- View history: `moku-memory_history(name)`
- Compare versions: `moku-memory_diff(name, from, to)`
- Roll back: `moku-memory_rollback(name, version_id)` — rollbacks are themselves
  versioned, so they can be reversed

#### Attribution

Every memory tracks its origin:
- `origin_session_ids` — which sessions contributed
- `origin_clients` — which hostnames contributed
- `moku-memory_session_summary(session_id)` — audit what a session produced

This means you can trace any memory back to the session and device that created it.

#### Export / Import

Memories can be exported to standard `.md` files and imported elsewhere. This
serves as both backup and review — you can inspect the full corpus as flat
files, edit them, and re-import.

---

## Quickstart

### 1. Deploy the server

**Container (recommended) — homelab:**

```bash
podman build -t localhost/moku-advisor:latest .
podman run -d --name moku --restart=unless-stopped --network=host \
  -v /data/moku-advisor:/data/moku-advisor:Z \
  -e MOKU_PROVIDER_MODE=direct \
  -e MOKU_API_KEY=sk-your-provider-key \
  -e MOKU_BASE_URL=https://api.openai.com/v1 \
  -e MOKU_MODEL=gpt-4o \
  localhost/moku-advisor:latest
```

**Container — GCP (via Terraform):**

See [deploy/gcp/](deploy/gcp/) for Terraform configs. Creates a GCE e2-small VM
with Podman rootless, persistent disk, Tailscale, and GCP Secret Manager.

```bash
cd deploy/gcp
terraform init
terraform plan
terraform apply
```

**Python directly:**

```bash
pip install -r requirements.txt
MOKU_PROVIDER_MODE=direct \
  MOKU_API_KEY=sk-... \
  MOKU_BASE_URL=https://api.openai.com/v1 \
  python -m moku_advisor.main
```

### 2. Verify it's running

```bash
curl http://localhost:8968/health
# {"status":"ok","service":"moku-advisor"}

curl http://localhost:8968/api/events/health
# {"status":"ok","total_events":0}

curl http://localhost:8968/metrics
# Prometheus-formatted metrics
```

### 3. Connect Claude Code

**Option A: `.mcp.json` (project root, most reliable)**

Create a `.mcp.json` file in your project root:

```json
{
  "mcpServers": {
    "moku": {
      "type": "sse",
      "url": "http://localhost:8968/sse"
    }
  }
}
```

Claude Code picks this up automatically when working in that project directory
— no global config needed. For a remote Moku server (e.g. on another machine
on the same Tailscale tailnet), use the Tailscale IP:

```json
{
  "mcpServers": {
    "moku": {
      "type": "sse",
      "url": "http://100.84.128.79:8968/sse"
    }
  }
}
```

**Option B: `settings.json` (global)**

Add to `~/.claude/settings.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "moku": {
      "type": "sse",
      "url": "http://localhost:8968/sse"
    }
  }
}
```

For user-global scope (works in VS Code extension):

```bash
claude mcp add moku --scope user --type sse http://localhost:8968/sse
```

### 4. Install slash commands

Copy the skill files from the `skills/` directory:

```bash
# One-shot for all profiles:
SKILLS_DIRS=(".claude" ".claude-sr" ".claude-sub" ".claude-api")
for d in "${SKILLS_DIRS[@]}"; do
  cp -r skills/* ~/$d/skills/
done
```

Each skill becomes a `/command`: `/brief`, `/consult`, `/dream`, `/pensieve`, `/update`, `/nats`, `/req`.

### 5. Enable event capture (required for dreams)

Add the hooks from `examples/settings.json` to your `~/.claude/settings.json`.
See [Event Logging](#event-logging) below.

## What you get

| Tool | What it does |
|------|-------------|
| `moku-memory_search/write/read/list/delete` | Full CRUD on shared memories |
| `moku-memory_export/import/export_all` | Portability between instances |
| `moku-memory_history/diff/rollback` | Versioning — track changes over time |
| `moku-memory_session_summary` | Attribution — see what a session produced |
| `moku-memory_pending_list/approve/reject/protect` | Governance — trusted dreamer workflow |
| `moku-consult_advisor` | Strategic guidance mid-task (configurable model + focus) |
| `moku-dream_run / dream_status` | Batch distills session events → memories |
| `moku-standards_reload` | Re-import team standards from disk |
| `moku-brief` | Session bootstrap — loads memories + standards + dream state |
| `moku-pensieve` | Search/browse the shared memory store |
| `moku-update` | Deploy slash command skills to devices |
| `moku-nats_pub/sub/ping` | Cross-device message bus (NATS optional) |
| `moku-memory_req` | Requirements tracking dashboard with status workflow |
| `moku-event_log` | HTTP event capture endpoint for dream pipeline |

**Slash commands**: `/brief`, `/consult`, `/dream`, `/pensieve`, `/update`, `/nats`, `/req`

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Claude Code (N instances)                │
│                                                             │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐      │
│  │ Instance│  │ Instance│  │ Instance│  │ Instance│      │
│  │ Ubuntu  │  │ Windows │  │ ChromeOS│  │ Win11   │      │
│  └────┬────┘  └────┬────┘  └────┬────┘  └────┬────┘      │
│       │            │            │            │            │
│       └────────────┴──────┬─────┴────────────┘            │
│                           │                                │
│                    SSE / MCP tools                         │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                       Moku Server                            │
│                                                              │
│  ┌──────────────┐  ┌──────────┐  ┌──────────────────────┐   │
│  │ Memory Store │  │ Dream    │  │ Consult Advisor      │   │
│  │ (SQLite)     │  │ Pipeline │  │ (focus + depth)      │   │
│  │ + versioning │  │ + events │  │ + standards inject   │   │
│  │ + protection │  │ + evict  │  └──────────────────────┘   │
│  └──────────────┘  └──────────┘                              │
│                                                              │
│  ┌──────────────┐  ┌──────────┐  ┌──────────────────────┐   │
│  │ NATS Bridge  │  │ Device   │  │ Standards            │   │
│  │ (optional)   │  │ Deployer │  │ Ingestion            │   │
│  └──────────────┘  └──────────┘  └──────────────────────┘   │
│                                                              │
│  LLM backend: direct (OpenAI API) or via Bifrost gateway     │
└─────────────────────────────────────────────────────────────┘
```

---

## Configuration

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MOKU_PROVIDER_MODE` | `bifrost` | `direct` or `bifrost` |
| `MOKU_API_KEY` | — | Provider key (required in `direct` mode) |
| `MOKU_BASE_URL` | depends | OpenAI-compatible base URL |
| `MOKU_MODEL` | `moonshotai/kimi-k2.6` | Advisor model |
| `MOKU_DREAM_MODEL` | falls back | Dream pipeline model |
| `MOKU_MCP_SERVER_NAME` | `moku` | MCP tool prefix |
| `MOKU_ADVISOR_DATA` | `/data/moku-advisor` | SQLite DB location |
| `MOKU_ADVISOR_API_KEY` | — | Event capture auth (empty = no auth) |
| `MOKU_TRUSTED_DREAMERS` | — | Comma-separated hostnames for write approval bypass |
| `MOKU_STANDARDS_DIR` | — | Path to team standards .md directory |
| `MOKU_SKILLS_DIR` | — | Path to slash command skill files (for /update) |
| `MOKU_BIFROST_TIMEOUT` | `300` | API timeout in seconds |

### Ports

| Port | Service |
|------|---------|
| `8968` | MCP SSE server + event capture API |

---

## Deployment

### Homelab (Podman rootless)

The NUC setup uses docker-compose with Podman. Systemd user services
for the dream timer and backup timer are in [deploy/homelab/](deploy/homelab/):

```bash
# Install systemd timers (user-level)
cp deploy/homelab/moku-dream.*   ~/.config/systemd/user/
cp deploy/homelab/moku-backup.*  ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now moku-dream.timer
systemctl --user enable --now moku-backup.timer
```

The compose file is at `compose/moku-advisor.yml` in the ai-stack repo.

### GCP (GCE VM)

See [deploy/gcp/](deploy/gcp/) for Terraform configs. Creates:

- GCE e2-small VM (2 vCPU, 2GB RAM, 20GB persistent disk) — ~$12/month
- Ubuntu 24.04 LTS with Podman rootless
- GCS bucket for SQLite backups (daily backup, 90-day archive lifecycle)
- GCP Secret Manager for all secrets
- Tailscale join for access (no public ports)
- Systemd timers for dream and backup

```bash
cd deploy/gcp
terraform init
terraform apply
# Then migrate secrets from the NUC:
bash scripts/migrate-secrets.sh
# SSH in and verify:
gcloud compute ssh moku-advisor
curl http://localhost:8968/health
```

### Dual deployment (migration period)

During migration, both homelab and GCP instances can run in parallel pointing
at separate databases. Claude Code points at either one via `.mcp.json`.

To copy memories from an existing instance:
1. On the old instance: `moku-memory_export_all` → flat `.md` files
2. On the new instance: `moku-memory_import` → loads into new DB
3. Verify with `moku-memory_list`

No downtime — both instances serve during the cutover.

### Observability endpoints

| Endpoint | Purpose | Response |
|----------|---------|----------|
| `/health` | Liveness probe | 200 if process is alive |
| `/ready` | Readiness probe | 200 if DB accessible, 503 otherwise |
| `/metrics` | Prometheus exposition format | Counts for memories, events, pending writes, eviction queue |
| `/api/events/health` | Legacy event endpoint | Event count |

---

## Provider Policy

Moku routes all LLM inference through **US and EU sovereign endpoints only**. While
Moku can use open-weight models created in the PRC (e.g. DeepSeek, Kimi, GLM,
Qwen), inference runs entirely outside the PRC via US-based provider infrastructure:

| Model | Origin | Provider Route |
|-------|--------|----------------|
| Kimi K2.6 | Moonshot AI (PRC) | DeepInfra / Novita / Parasail (US) |
| DeepSeek V4 | DeepSeek (PRC) | DeepInfra / Novita (US) |
| GLM-5 | Zhipu AI (PRC) | DeepInfra / Novita / Parasail / Vertex (US) |
| Qwen | Alibaba (PRC) | Nebius / DeepInfra (US/EU) |

This is explicitly documented in the README because the model names alone could
mislead colleagues into thinking direct Moonshot/DeepSeek API usage is involved.
**It is not.** All inference goes through US-based providers that happen to host
open-weight models.

## For teams

Each team member runs their own Claude Code connected to the same Moku.
Memories are shared. Trusted dreamers approve writes.

1. Run Moku on a shared server or as a cloud container
2. Each member points `mcpServers` at the shared URL
3. Each member installs the skills and hooks
4. Run `/dream` periodically on one instance to consolidate

---

## Building

```bash
git clone https://github.com/fjwood69/moku.git
cd moku
podman build -t localhost/moku-advisor:latest .
# Or with Docker: docker build -t moku-advisor:latest .
```

## License

MIT