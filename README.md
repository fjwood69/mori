# Moku — Shared Memory & Dream Pipeline for Claude Code

Moku is an MCP server that gives Claude Code persistent shared memory, a
strategic advisor, and a "dream" pipeline that automatically distills
session activity into durable knowledge. Works with any OpenAI-compatible
LLM provider — no Bifrost, no homelab, no Anthropic account required.

**In development, we used this with [Bifrost](https://github.com/fjwood69/bifrost)
and open models. In production, it works with any provider (OpenAI, Anthropic,
Together, Groq, local Ollama, etc.)**

## What you get

| Tool | What it does |
|------|-------------|
| `moku-memory_search` / `moku-memory_write` | Persistent shared memory — survives sessions, survives restarts |
| `moku-consult_advisor` | Strategic guidance mid-task (configurable model) |
| `moku-dream_run` | Batch distills session events into structured memories |
| `moku-dream_status` | Shows dream pipeline state (watermark, backlog) |
| `moku-memory_export` / `moku-memory_import` | Portability — move memories between instances |

Plus four slash commands (`/brief`, `/consult`, `/dream`, `/pensieve`)
that wire these tools into Claude Code's workflow.

## Quickstart

### 1. Deploy the server

**Option A: Container (recommended)**

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

**Option B: Python directly**

```bash
pip install -r requirements.txt
MOKU_PROVIDER_MODE=direct \
  MOKU_API_KEY=sk-... \
  MOKU_BASE_URL=https://api.openai.com/v1 \
  python -m moku_advisor.main
```

**Option C: With Bifrost (advanced)**

If you run [Bifrost](https://github.com/fjwood69/bifrost) as your
LLM gateway, Moku can route through it using VK-based auth:

```bash
docker run -d --name moku --restart=unless-stopped -p 8968:8968 \
  -v moku-data:/data/moku \
  -e MOKU_BASE_URL=http://bifrost:8080 \
  ghcr.io/fjwood69/moku:latest
```

### 2. Verify it's running

```bash
curl http://localhost:8968/api/events/health
# {"status":"ok","total_events":0}
```

### 3. Connect Claude Code

Add this to your `~/.claude/settings.json` under `mcpServers`:

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

### 4. Add slash commands

Copy the skill files from `skills/` into your Claude Code profile's
`skills/` directory:

```bash
mkdir -p ~/.claude/skills
cp skills/*.skill.md ~/.claude/skills/
```

Each file becomes a slash command: `/brief`, `/consult`, `/dream`, `/pensieve`.

### 5. Enable event capture (optional, needed for dreams)

Add the hook configuration from `examples/settings.json` to your
`~/.claude/settings.json`. This captures session events that the
dream pipeline uses to produce memories.

## Configuration reference

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MOKU_PROVIDER_MODE` | `bifrost` | `direct` or `bifrost` — how the server connects to an LLM |
| `MOKU_API_KEY` | — | Provider API key (required in `direct` mode) |
| `MOKU_BASE_URL` | depends on mode | OpenAI-compatible base URL |
| `MOKU_MODEL` | `moonshotai/kimi-k2.6` | Model for consult_advisor (direct mode) |
| `MOKU_DREAM_MODEL` | falls back to `MOKU_MODEL` | Model for dream_run (direct mode) |
| `MOKU_MCP_SERVER_NAME` | `moku` | Prefix for MCP tool names |
| `MOKU_ADVISOR_DATA` | `/data/moku-advisor` | Where SQLite DB lives |
| `MOKU_ADVISOR_API_KEY` | — | Auth key for event capture endpoint (empty = no auth) |
| `MOKU_TRUSTED_DREAMERS` | — | Comma-separated hostnames that auto-approve protected memory writes |
| `MOKU_BIFROST_ADVISOR_VK` | `moku-advisor-local` | VK key for advisor (Bifrost mode) |
| `MOKU_BIFROST_DREAM_VK` | `moku-dream-local` | VK key for dream (Bifrost mode) |
| `MOKU_BIFROST_TIMEOUT` | `300` | API timeout in seconds |

### Ports

| Port | Service |
|------|---------|
| `8968` | MCP SSE server + event capture API |

## How it works

### Memory

Memories are stored in a single SQLite database (`memories.db`) with
versioning, attribution (session IDs + client hostnames), and
protection (trusted dreamers can write directly; others queue for
approval).

### Dream pipeline

Session events are captured via Claude Code lifecycle hooks
(PostToolUse, UserPromptSubmit, Stop). The dream pipeline reads events
since the last watermark, sends them to an LLM, and writes the
extracted memories back to the store. Run it manually via `/dream`
or schedule it:

```
/loop 4h /dream
```

**Recommended models for dream**: We tested several options. DeepSeek V4
Flash and V4 Pro struggled with reliable JSON output (returned content in
`reasoning` rather than `content` fields). Gemma 4 31B-it was adequate.
**Kimi K2.6** produced the most consistent structured output with good
judgment about what to keep and what to discard — it's our recommended
default. Set it via `MOKU_DREAM_MODEL=moonshotai/kimi-k2.6`.

### Consult advisor

A configurable LLM receives your question plus optional file context
and returns strategic guidance. Supports focus areas (architecture,
security, performance, style) and depth levels (quick, balanced, deep).

## For teams

Each team member runs their own Claude Code instance connected to the
same Moku server. Memories are shared across sessions and across team
members. The trusted dreamer mechanism prevents conflicting writes.

To deploy for a team:

1. Run Moku on a shared server (or as a cloud container)
2. Each team member points their `mcpServers` at the shared URL
3. Each team member installs the skills and hooks
4. Run `/dream` periodically on one instance to consolidate

## Building from source

```bash
git clone https://github.com/fjwood69/moku.git
cd moku
docker build -t ghcr.io/fjwood69/moku:latest .
```

Or with Podman:

```bash
podman build -t ghcr.io/fjwood69/moku:latest .
```

## LICENSE

MIT
