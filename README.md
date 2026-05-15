# Moku — Shared Memory & Dream Pipeline for Claude Code

Moku is an MCP server that gives Claude Code persistent shared memory,
strategic advisory, session grounding, and a "dream" pipeline that
distills session activity into durable knowledge.

Works with any OpenAI-compatible provider. No homelab, no Anthropic
account, no Bifrost required — though those all work too.

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
| `moku-event_log` | HTTP event capture endpoint for dream pipeline |

**Slash commands**: `/brief`, `/consult`, `/dream`, `/pensieve`, `/update`, `/nats`

## Quickstart

### 1. Deploy the server

**Container (recommended):**

```bash
# With a direct API provider:
docker run -d --name moku --restart=unless-stopped -p 8968:8968 \
  -v moku-data:/data/moku \
  -e MOKU_PROVIDER_MODE=direct \
  -e MOKU_API_KEY=sk-your-provider-key \
  -e MOKU_BASE_URL=https://api.openai.com/v1 \
  -e MOKU_MODEL=gpt-4o \
  ghcr.io/fjwood69/moku:latest
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
curl http://localhost:8968/api/events/health
# {"status":"ok","total_events":0}
```

### 3. Connect Claude Code

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

Each skill becomes a `/command`: `/brief`, `/consult`, `/dream`, `/pensieve`, `/update`, `/nats`.

### 5. Enable event capture (required for dreams)

Add the hooks from `examples/settings.json` to your `~/.claude/settings.json`.
See [Event Capture](#event-capture) below.

## How it works

### Memory store

Memories live in a single SQLite database (`memories.db`) with:

- **Versioning** — every change creates a new version. View history, diff versions, rollback.
- **Attribution** — each memory tracks which session(s) and client(s) contributed.
- **Protection** — trusted dreamers write directly; others queue for approval.
- **Tagging** — memories are taggable (`security`, `architecture`, `decision`) for filtering.
- **Search** — keyword search across name, title, description, and body.

### Dream pipeline

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

### Session grounding

Moku uses **session grounding** rather than per-query RAG. `/brief` loads
shared memories, team standards, and dream pipeline state into context at
session start. From turn one, the model knows your security baseline,
coding conventions, and current project state — no retrieval needed.

When the standards corpus grows beyond one context window, run separate
Moku instances per namespace rather than adding a vector store:

```bash
# Retail team
docker run ... -e MOKU_STANDARDS_DIR=/standards/retail -p 8970:8968
# Energy team
docker run ... -e MOKU_STANDARDS_DIR=/standards/energy -p 8971:8968
```

### Consult advisor

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

### Standards ingestion

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

### Device deployment

The `moku-update` tool generates install commands for multiple profiles
across devices. It knows each device's profile layout:

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

### Cross-device messaging (NATS)

Optional NATS JetStream integration for cross-device state-of-play messages.
Each device publishes session summaries; any device can replay the last 7 days.
Useful for awareness across a team or fleet of Claude Code instances.

### Event capture

Claude Code lifecycle hooks POST session events to `POST /api/events/raw`.
These events feed the dream pipeline.

```bash
# Minimal hook config — add to settings.json:
curl -sf -X POST 'http://localhost:8968/api/events/raw?client=my-hostname' \
  -H 'Content-Type: application/json' -d @-
```

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

## For teams

Each team member runs their own Claude Code connected to the same Moku.
Memories are shared. Trusted dreamers approve writes.

1. Run Moku on a shared server or as a cloud container
2. Each member points `mcpServers` at the shared URL
3. Each member installs the skills and hooks
4. Run `/dream` periodically on one instance to consolidate

## Building

```bash
git clone https://github.com/fjwood69/moku.git
cd moku
docker build -t ghcr.io/fjwood69/moku:latest .
```

## License

MIT
