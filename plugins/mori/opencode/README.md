# Mori plugin for OpenCode

Connects OpenCode to a self-hosted [Mori](https://github.com/fjwood69/mori) server.

Ships lifecycle events to the dream pipeline, re-grounds the agent at session start,
and (if the `experimental.session.compacting` hook fires) injects curated canon
directly into the compaction summary so key decisions survive context compression.

---

## Quick install

### 1. Copy the plugin

```bash
# Global (all projects)
mkdir -p ~/.config/opencode/plugins/mori
cp -r . ~/.config/opencode/plugins/mori

# Or project-scoped
mkdir -p .opencode/plugins/mori
cp -r . .opencode/plugins/mori
```

Or use the repo installer:

```bash
./scripts/install-mori-opencode.sh http://YOUR-SERVER:8968 YOUR-64-CHAR-SECRET
```

### 2. Configure env vars

```bash
export MORI_SERVER_URL=http://YOUR-SERVER:8968
export MORI_API_KEY=YOUR-64-CHAR-BARE-SECRET   # bare secret, not name:secret
```

Add to your shell profile (`~/.bashrc`, `~/.zshrc`, or `~/.profile`) so they
persist across sessions. Windows: set as user environment variables.

### 3. Enable in `opencode.json`

For npm-installed plugin:

```json
{
  "plugin": ["opencode-mori"]
}
```

For local copy, OpenCode discovers plugins in the `plugins/` directory automatically.

### 4. Add the MCP server

Copy `mcp.json` into your project or global OpenCode config, replacing the env var
placeholders with your actual URL and key — or let the env vars expand at runtime
if your OpenCode version supports it:

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

> **The `x-api-key` is the bare secret** — the 64-char hex string alone, not
> `name:secret`. The `name:` prefix only labels the key in the server's
> `MORI_API_KEYS`; clients send the secret alone or get 401.

---

## What it wires

| Hook | What it does |
|------|-------------|
| `session.created` | Ships SessionStart event; re-grounding nudge at session open |
| `session.compacted` | Ships PostCompact event; nudges agent to run `/brief --post-compact` |
| `session.idle` | Ships Stop event for dream ingestion |
| `tool.execute.after` | Ships PostToolUse events (capped at 2KB) |
| `experimental.session.compacting` | Runs dream pipeline before compression; injects grounding text into compaction summary |
| `shell.env` | Propagates `MORI_SERVER_URL`, `MORI_API_KEY`, `MORI_CLIENT` to sub-shells |

Skills (`/brief`, `/dream`, `/pensieve`, `/consult`, `/wrap`) are auto-discovered
from `.claude/skills/` and `.agents/skills/` — no extra configuration needed.

---

## Full guide

[docs/getting-started/opencode.md](../../../docs/getting-started/opencode.md)
