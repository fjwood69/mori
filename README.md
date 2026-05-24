![Mori — A shared memory layer for AI coding agents](https://raw.githubusercontent.com/fjwood69/mori/f76f7622984f72be1c4d4d28870d3972a320cb2e/docs/assets/header-dark-v0_1_2.svg)


Mori (森) is a shared memory layer for AI coding agents — one that compounds.
Sessions feed a dream pipeline that distils activity into durable knowledge,
so every instance starts informed rather than cold. One Mori, many agents —
every session benefits from what every other session learned.

Works with any OpenAI-compatible provider. No homelab, no Anthropic
account, no LLM Gateway required — though those all work too.

---

## Multi-Instance Coherence
![One Forest, Many Agents](https://raw.githubusercontent.com/fjwood69/mori/5d55d248dc91fc7c8292c3deaf6d2a2aa40192ce/docs/assets/figure-5-one-forest.svg)


If you run Claude Code across multiple machines or profiles — one focused on the
API layer, another on the frontend, a third on infrastructure — you already know
the problem: each instance is brilliant in isolation, but none of them know what
the others decided.

Instance B doesn't know that Instance A just changed the auth contract. Instance C
doesn't know that Instance B's deployment assumptions shifted. They find out the
hard way, mid-task, when something breaks.

Mori solves this.

Every CC instance sends its session events — prompts, tool calls, errors, decisions
— to the shared Mori server. The dream pipeline distils those events from **all
instances** into a unified memory store. At the start of any session, `/brief`
surfaces what the other instances have been doing: the cross-cutting decisions,
the architectural tensions, the gotchas one instance hit that another is about
to repeat.

From turn one, each instance knows what the others know.


### Real-time awareness via NATS

The dream pipeline runs on a schedule. For decisions that
can't wait, NATS provides real-time cross-instance messaging:

```bash
# Instance A just changed the auth contract:
/nats pub "Auth contract changed — JWT now includes org_id claim. See memory: api-auth-contract"

# Instance B picks it up immediately:
/nats sub
→ [Instance A] Auth contract changed — JWT now includes org_id claim.
```

Any instance can publish, any instance can subscribe. Messages replay for 7 days
so instances that were offline don't miss decisions.

### What gets shared

The dream pipeline captures and synthesises across instances:

- **Architecture decisions** — "Instance A moved to event-driven auth; Instance B
  should update its session handling assumptions"
- **Cross-cutting gotchas** — "This provider 429s under load; all instances should
  use the fallback routing"
- **Deferred decisions** — "Instance C flagged a migration risk; nobody has resolved it yet"
- **Conventions** — patterns that emerge across sessions become shared standards

What doesn't get shared: one-off bugs, noise, anything recoverable from docs or git.
The dream pipeline filters aggressively. You get signal, not a transcript.

### Setup for multi-instance use

Point every instance at the same Mori server. That's it.

```json
{
  "mcpServers": {
    "mori": {
      "type": "http",
      "url": "http://<your-mori-server>:8968/mcp"
    }
  }
}
```

Add the lifecycle hooks to each instance's `settings.json` so events flow in.
Each instance gets a `?client=<hostname>` tag so the dream pipeline knows who
contributed what. Attribution is preserved — you can always trace a memory back
to the session and device that produced it.

### Recommended dream cadence for multi-instance setups

| Instances | Recommended interval |
|-----------|---------------------|
| 1–2 | 1 hour |
| 3–5 | 30 minutes |
| 5+ | 30 minutes + manual `/dream` after significant decisions |

The `PreCompact` hook triggers an immediate dream run before any instance's
context is compressed — ensuring nothing is lost at the moment it matters most.

---

## Core capabilities

### 1. Event Logging

Claude Code lifecycle hooks POST session events to `POST /api/events/raw`.
These events feed the dream pipeline. The `PreCompact` hook posts to
`POST /api/precompact` and triggers an immediate synchronous dream.

```bash
# Minimal hook config — add to settings.json:
curl -sf -X POST 'http://localhost:8968/api/events/raw?client=my-hostname' \
  -H 'Content-Type: application/json' -d @-
```

Every Claude Code session emits lifecycle events — tool calls, prompts, errors,
stop reasons. Mori receives these via HTTP POST and stores them in an append-only
event log. This is the raw material everything else builds on.

**What it captures:**
- `PostToolUse` — tool name, input, output, errors
- `PostToolUseFailure` — tool call errors (high-value for dream distillation)
- `PreCompact` — session snapshot before context compression (triggers synchronous dream)
- `UserPromptSubmit` — the prompt text
- `Stop` / `SessionEnd` — stop reason, model used
- Session ID, client hostname, working directory, transcript path

**Components required:** Mori server only. No LLM provider needed.

**Config:** `MORI_ADVISOR_API_KEY` for auth (empty = no auth, only reachable via
Tailscale LAN or localhost).

---

### 2. Persistent Memory

Memories live in a single SQLite database (`memories.db`) with:

- **Versioning** — every change creates a new version. View history, diff versions, rollback.
- **Attribution** — each memory tracks which session(s) and client(s) contributed.
- **Protection** — trusted dreamers write directly; others queue for approval.
- **Tagging** — memories are taggable (`security`, `architecture`, `decision`) for filtering.
- **Search** — keyword search across name, title, description, and body.

![The Forest Remembers](https://raw.githubusercontent.com/fjwood69/mori/5d55d248dc91fc7c8292c3deaf6d2a2aa40192ce/docs/assets/figure-2-the-forest-remembers.svg)

**Components required:** Mori server only. Memories persist in SQLite — no
external dependencies.

---

### 3. Dream Phase

Session events are captured via Claude Code lifecycle hooks (PostToolUse,
PostToolUseFailure, UserPromptSubmit, Stop, PreCompact). The dream pipeline
reads events since the last watermark, sends them to a configurable LLM, and
writes extracted memories back to the store.

```
Hook fires  →  POST /api/events/raw  →  SQLite events table
                                             ↓
PreCompact  →  POST /api/precompact  →  dream_run() reads since watermark
                                             ↓
                                      LLM distills events → structured memories
                                             ↓
                                      memories written to store (with attribution)
                                             ↓
                                      watermark advanced
```


![Dream Pipeline](https://raw.githubusercontent.com/fjwood69/mori/5d55d248dc91fc7c8292c3deaf6d2a2aa40192ce/docs/assets/figure-1-dream-pipeline.svg)


Run it: `/dream` or `mori-dream_run`. Check state: `/dream --status`.

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

**Components required:** Mori server + LLM provider (for the distillation model).
Config: `MORI_DREAM_MODEL` (defaults to `MORI_MODEL`, then `deepseek/deepseek-v4-flash`).

---

### 4. Session Context (`/brief`)

Mori uses **session grounding** rather than per-query RAG. `/brief` loads
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
Mori instances per namespace rather than adding a vector store:

```bash
# Retail team
docker run ... -e MORI_STANDARDS_DIR=/standards/retail -p 8970:8968
# Energy team
docker run ... -e MORI_STANDARDS_DIR=/standards/energy -p 8971:8968
```

#### Standards ingestion

Set `MORI_STANDARDS_DIR` to a directory of `.md` files:

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

The mori repo ships built-in standards in `standards/`. To enable them,
set `MORI_STANDARDS_DIR=/app/standards` (container) or point at the
repo's `standards/` directory (native).

On startup, every `.md` file is imported as a protected memory with `type: standard`
and tags from its subdirectory. Standards are read-only to non-trusted dreamers.

Update without restarting: `mori-standards_reload` (trusted dreamers only).

**Components required:** Mori server + `/brief` skill. Config: `MORI_STANDARDS_DIR`.

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

**Components required:** Mori server + LLM provider. Config: `MORI_MODEL`
(default `moonshotai/kimi-k2.6`).

---

### 6. Agent Delegation + NATS (`/nats`, `/update`)

#### Cross-device messaging (NATS)

Optional NATS JetStream integration for cross-device state-of-play messages.
Each device publishes session summaries; any device can replay the last 7 days.
Useful for awareness across a team or fleet of Claude Code instances.

#### Skill deployment (`/update`)

The `mori-update` tool generates install commands for skills and slash commands
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

**Components required:** Mori server. NATS server for cross-device messaging
(optional).

---

### 7. Governance — Memory Quality & Validity

Memories accumulate over time. Without safeguards, they drift, conflict, or
accumulate noise. Mori has several mechanisms to maintain quality:

#### Trusted Dreamers

Certain client hostnames are designated as **trusted dreamers**. Only they can
directly modify protected memories. Writes from other instances are queued as
pending writes.

Configured via `MORI_TRUSTED_DREAMERS` env var (comma-separated hostnames)
or in the `dreamer_config` table.

#### Protection

Any memory can be toggled protected via `mori-memory_protect`. When protected:
- Trusted dreamers write directly (no change in behaviour)
- Other instances' writes go to `pending_writes` for review
- `mori-memory_pending_list`, `mori-memory_approve`, `mori-memory_reject` manage the queue

#### Versioning & Rollback

Every write snapshots the previous state. You can:
- View history: `mori-memory_history(name)`
- Compare versions: `mori-memory_diff(name, from, to)`
- Roll back: `mori-memory_rollback(name, version_id)` — rollbacks are themselves
  versioned, so they can be reversed

#### Attribution

Every memory tracks its origin:
- `origin_session_ids` — which sessions contributed
- `origin_clients` — which hostnames contributed
- `mori-memory_session_summary(session_id)` — audit what a session produced

This means you can trace any memory back to the session and device that created it.

#### Export / Import

Memories can be exported to standard `.md` files and imported elsewhere. This
serves as both backup and review — you can inspect the full corpus as flat
files, edit them, and re-import.

---

## Quickstart

### 1. Pick your platform

See [docs/deployment/quickstart.md](docs/deployment/quickstart.md) for platform-specific instructions (Docker Compose, Podman, macOS native, Windows, GCP).

### 2. Verify it's running

```bash
curl http://localhost:8968/health
# {"status":"ok","service":"mori-advisor"}

curl http://localhost:8968/api/events/health
# {"status":"ok","total_events":0}

curl http://localhost:8968/metrics
# Prometheus-formatted metrics
```

### 3. Connect Claude Code

The fastest way is the automated installer — it configures the MCP server, event hooks, and slash commands in one step:

```bash
./scripts/install-mori-claude.sh
```

Or on Windows (PowerShell):
```powershell
powershell -File scripts/install-mori-claude.ps1
```

See [docs/getting-started/claude-code.md](docs/getting-started/claude-code.md) for full details.

---

## Setup guides

| Platform | Quick start | Full guide |
|----------|-------------|------------|
| Claude Code CLI | `./scripts/install-mori-claude.sh` | [docs/getting-started/claude-code.md](docs/getting-started/claude-code.md) |
| Google Antigravity IDE | `./scripts/install-mori-antigravity.sh` | [docs/getting-started/antigravity.md](docs/getting-started/antigravity.md) |
| Cline | `./scripts/install-mori-cline.sh` | [docs/getting-started/cline.md](docs/getting-started/cline.md) |

---

## What you get

| Tool | What it does |
|------|-------------|
| `mori-memory_search/write/read/list/delete` | Full CRUD on shared memories |
| `mori-memory_export/import/export_all` | Portability between instances |
| `mori-memory_history/diff/rollback` | Versioning — track changes over time |
| `mori-memory_session_summary` | Attribution — see what a session produced |
| `mori-memory_pending_list/approve/reject/protect` | Governance — trusted dreamer workflow |
| `mori-consult_advisor` | Strategic guidance mid-task (configurable model + focus) |
| `mori-dream_run / dream_status` | Batch distills session events → memories |
| `mori-standards_reload` | Re-import team standards from disk |
| `mori-brief` | Session bootstrap — loads memories + standards + dream state |
| `mori-pensieve` | Search/browse the shared memory store |
| `mori-update` | Deploy slash command skills to devices |
| `mori-nats_pub/sub/ping` | Cross-device message bus (NATS optional) |
| `mori-memory_req` | Requirements tracking dashboard with status workflow |
| `mori-event_log` | HTTP event capture endpoint for dream pipeline |

**Slash commands**: `/brief`, `/wrap`, `/consult`, `/dream`, `/pensieve`, `/update`, `/nats`, `/req`

### Quick Reference

| Command | Usage | What it does |
|---------|-------|-------------|
| `/brief` | `/brief` | Load shared memories + standards + dream state into context |
| `/wrap` | `/wrap` | Session wrap — writes summary to cc-share, publishes to NATS, runs dream |
| `/consult` | `/consult "question" [--focus security] [--depth quick] [--file path]` | Get strategic guidance from the advisor model |
| `/dream` | `/dream` | Distill undreamed events into memories |
| | `/dream --status` | Show dream pipeline state (watermark, event counts) |
| | `/dream --dry-run` | Preview what would be produced without writing |
| `/pensieve` | `/pensieve <query>` | Search memories by keyword |
| | `/pensieve read <name>` | Read a specific memory by its kebab-case name |
| | `/pensieve --type decision --since 30d` | Filter by type and recency |
| | `/pensieve --tag security` | Filter by tag |
| `/req` | `/req` | Show requirements dashboard grouped by project |
| | `/req --project bifrost` | Filter by project |
| | `/req --project bifrost --status pending` | Filter by project and status |
| | `/req add "Title" --project bifrost --pri high` | Create a new requirement |
| | `/req done req-bifrost-<name>` | Mark a requirement complete |
| `/nats` | `/nats ping` | Check NATS connection status |
| | `/nats sub` | Show recent cross-device messages |
| | `/nats pub "message"` | Publish a message to other devices |
| `/update` | `/update --device twiggy --skill nats` | Generate install commands for a skill on a device |
Full reference: [docs/reference/slash-commands.md](docs/reference/slash-commands.md).

---

## Architecture

![Mori Architecture](https://raw.githubusercontent.com/fjwood69/mori/5d55d248dc91fc7c8292c3deaf6d2a2aa40192ce/docs/assets/figure-4-architecture.svg)


**Configuration** → [docs/reference/configuration.md](docs/reference/configuration.md)
**Deployment guides** → [docs/deployment/quickstart.md](docs/deployment/quickstart.md)
**Recommended models** → [docs/reference/models.md](docs/reference/models.md)
**For teams** → [docs/for-teams.md](docs/for-teams.md)

---

## Building

```bash
git clone https://github.com/fjwood69/mori.git
cd mori
podman build -t localhost/mori-advisor:latest .
# Or with Docker: docker build -t mori-advisor:latest .
```

## License

MIT

---

[![Support me on Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/moriapp)
