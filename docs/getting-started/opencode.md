# Getting Started — Mori + OpenCode

Connect OpenCode to your Mori shared memory server: `/brief`, `/consult`, `/dream`,
event capture, and cross-device messaging. OpenCode is Tier 1 integration — the
`experimental.session.compacting` hook lets Mori inject curated canon directly into
the compaction summary, so key decisions survive context compression.

---

## Prerequisites

- **OpenCode** (latest) with Node.js 18+ available in `PATH`.
- **Mori server** reachable (homelab, GCE, Tailscale, Railway, etc.).
- Your **bare API key** — the 64-char hex secret from `MORI_API_KEYS` (not `name:secret`).

---

## Install

### Option A — Installer script (recommended)

From the mori repo root:

```bash
./scripts/install-mori-opencode.sh http://YOUR-SERVER:8968 YOUR-64-CHAR-SECRET
```

Windows (PowerShell 5.1+):

```powershell
.\scripts\install-mori-opencode.ps1 -MoriUrl http://YOUR-SERVER:8968 -MoriKey YOUR-64-CHAR-SECRET
```

The installer:
- Copies the plugin to `~/.config/opencode/plugins/mori/`
- Adds `MORI_SERVER_URL` and `MORI_API_KEY` to your shell profile
- Writes `mcp.json` to your global OpenCode config

### Option B — Manual install

**1. Copy the plugin package:**

```bash
# Global (all projects)
mkdir -p ~/.config/opencode/plugins/mori
cp -r plugins/mori/opencode/. ~/.config/opencode/plugins/mori/

# Project-scoped
mkdir -p .opencode/plugins/mori
cp -r plugins/mori/opencode/. .opencode/plugins/mori/
```

**2. Set environment variables:**

```bash
export MORI_SERVER_URL=http://YOUR-SERVER:8968
export MORI_API_KEY=YOUR-64-CHAR-BARE-SECRET
```

Add to `~/.bashrc`, `~/.zshrc`, or `~/.profile` to persist. On Windows, set as
user environment variables in System Properties.

**3. Add the MCP server to your OpenCode config** (`~/.config/opencode/opencode.json`
or project-level `opencode.json`):

```json
{
  "mcpServers": {
    "mori": {
      "type": "remote",
      "url": "http://YOUR-SERVER:8968/mcp",
      "headers": {
        "x-api-key": "YOUR-64-CHAR-BARE-SECRET"
      }
    }
  }
}
```

> **The `x-api-key` is the bare secret** — the 64-char hex string alone, **not**
> `name:secret`. The `name:` prefix only labels the key in the server's
> `MORI_API_KEYS`; clients send the secret alone, or the server returns `401`.

---

## What the plugin wires

| Component | What it does |
|-----------|-------------|
| **MCP server** | HTTP connection to your Mori server — all 30+ MCP tools available immediately |
| **Skills** | `/brief`, `/dream`, `/consult`, `/pensieve`, `/wrap`, `/ingest`, `/req`, `/nats`, `/msg` |
| **session.created hook** | Ships SessionStart event; grounding nudge at session open |
| **session.compacted hook** | Ships PostCompact event; nudges `/brief --post-compact` after compaction |
| **session.idle hook** | Ships Stop event for dream ingestion |
| **tool.execute.after hook** | Ships tool call events (capped at 2KB) to the dream pipeline |
| **experimental.session.compacting hook** | Runs dream pipeline before compression; injects grounding text into the compaction summary so curated canon survives the context window reset |
| **shell.env hook** | Propagates Mori env vars to all sub-shells spawned by OpenCode |

### Skills are already there

OpenCode reads skills from `.claude/skills/<name>/SKILL.md` and
`.agents/skills/<name>/SKILL.md` — the same paths Mori already uses. No extra
configuration is needed; skills activate as soon as OpenCode finds them.

---

## The compaction advantage

OpenCode's `experimental.session.compacting` hook fires **before** the LLM
generates the compaction summary, and provides an `output.context` array for
injecting text into what the LLM sees.

**Claude Code PreCompact:** fires before compaction, can run dream pipeline,
**cannot** inject into the compaction summary itself.

**OpenCode `experimental.session.compacting`:** fires before compaction, runs dream
pipeline, **and** pushes grounding text directly into `output.context` — the LLM
sees it when generating the continuation prompt. Key decisions persist through
compaction automatically, not just on the next `/brief`.

This hook is marked `experimental` by OpenCode and may not be available in all
versions. The plugin degrades gracefully if it is absent.

---

## Verification

```bash
# Check MCP connection
opencode mcp list          # mori should show as connected

# Inside a session — run a brief
/brief

# Health check from the terminal
curl -H "x-api-key: YOUR_KEY" http://YOUR-SERVER:8968/health
```

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| MCP tools not available | Confirm `mcp.json` url + key are set; check `opencode mcp list` |
| `401 Unauthorized` from Mori server | You sent `name:secret` instead of the bare secret — remove the `name:` prefix |
| Events not reaching Mori | Confirm `MORI_SERVER_URL` and `MORI_API_KEY` are exported in your shell |
| Skills missing | Confirm `.claude/skills/` or `.agents/skills/` are present; restart OpenCode |
| `/brief --post-compact` not firing after compaction | `MORI_POST_COMPACT_BRIEF` env var must **not** be set to `false`; confirm Node 18+ is in `PATH` |
| `experimental.session.compacting` not firing | This hook is experimental — it may not be present in your OpenCode version; the plugin degrades gracefully |

---

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `MORI_SERVER_URL` | — | **Required.** Base URL of your Mori server |
| `MORI_API_KEY` | — | **Required.** Bare secret from `MORI_API_KEYS` |
| `MORI_CLIENT` | `$HOSTNAME` | Override the client hostname reported to Mori |
| `MORI_POST_COMPACT_BRIEF` | _(unset)_ | Set to `false` to suppress post-compact `/brief` nudge |

Or set in `opencode.json`:

```json
{
  "env": {
    "MORI_SERVER_URL": "http://YOUR-SERVER:8968",
    "MORI_API_KEY": "YOUR-64-CHAR-BARE-SECRET"
  }
}
```
