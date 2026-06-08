# Getting Started — Mori + Codex

Connect OpenAI Codex to your Mori shared memory server: `/brief`, `/consult`, `/dream`, event capture, and cross-device messaging.

---

## Prerequisites

- **OpenAI Codex** (latest) with Node.js 18+ available in `PATH`.
- **Mori server** reachable (homelab, GCE, Tailscale, Railway, etc.).
- Your **bare API key** — the 64-char hex secret from `MORI_API_KEYS` (not `name:secret`).

---

## Install as a Plugin (Recommended)

Mori ships as a unified plugin package at `plugins/mori/`. The Codex manifest (`.codex-plugin/plugin.json`) bundles skills, hooks, and the MCP server config — all from a single package.

### 1. Add the plugin to a marketplace

**Personal (all projects):** add an entry to `~/.agents/plugins/marketplace.json`:

```json
{
  "plugins": [
    {
      "name": "mori",
      "source": "github:fjwood69/mori/plugins/mori",
      "installPolicy": "manual"
    }
  ]
}
```

**Repo-scoped (one project):** add the same entry to `.agents/plugins/marketplace.json` at your repo root.

### 2. Install

```bash
codex plugin install mori
```

### 3. Configure your server URL and API key

Edit the installed plugin's `mcp.json`. Codex copies the plugin to your local plugins directory; find it with:

```bash
codex plugin info mori
```

Then edit `mcp.json`:

```json
{
  "mcpServers": {
    "mori": {
      "type": "http",
      "url": "http://YOUR-SERVER:8968/mcp",
      "headers": { "x-api-key": "YOUR-64-CHAR-BARE-SECRET" }
    }
  }
}
```

> **The `x-api-key` is the bare secret** — the 64-char hex string alone, **not** `name:secret`.
> The `name:` prefix only labels the key in the server's `MORI_API_KEYS`; clients send the secret alone.

### 4. Set environment variables (for telemetry hooks)

Add to your shell profile (or Codex env config):

```bash
export MORI_SERVER_URL=http://YOUR-SERVER:8968
export MORI_API_KEY=YOUR-64-CHAR-BARE-SECRET
```

These power the event capture hooks. The MCP skills work without them.

### 5. Reload Codex

```bash
codex plugin list  # mori should appear as enabled
```

---

## Manual Install (no marketplace)

Clone the repo and copy or symlink the plugin:

```bash
# Copy (safe for distribution)
cp -r plugins/mori ~/.codex/plugins/mori

# Symlink (easier to update during local dev)
ln -s "$(pwd)/plugins/mori" ~/.codex/plugins/mori
```

Then configure `mcp.json` as above and set the env vars.

---

## What the plugin wires

| Component | What it does |
|-----------|-------------|
| **MCP server** (`mcp.json`) | HTTP connection to your mori server — all 30+ MCP tools available immediately |
| **Skills** | `/brief`, `/dream`, `/consult`, `/pensieve`, `/wrap`, `/ingest`, `/req`, `/nats`, `/msg` |
| **SessionStart hook** | Re-grounds the agent at startup / resume / clear; nudges `/brief --post-compact` after compaction |
| **PostToolUse hook** | Ships tool call events to the mori server for the dream pipeline |
| **UserPromptSubmit hook** | Ships user turn events |
| **Stop hook** | Ships the turn summary + last 64KB of transcript (base64) |
| **PreCompact hook** | Flushes state to the server before the context window is compressed |

---

## Verification

```bash
# Check MCP connection
codex mcp list          # mori should show as connected

# Check skills are loaded
codex skill list        # brief, dream, pensieve etc should appear

# Inside a session — run a brief
/brief

# Health check from the terminal
curl -H "x-api-key: YOUR_KEY" http://YOUR-SERVER:8968/health
```

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| MCP tools not available | Confirm `mcp.json` url + key are set; check `codex mcp list` |
| `401 Unauthorized` from mori server | You sent `name:secret` instead of the bare secret — remove the `name:` prefix |
| Hooks not firing | Confirm `MORI_SERVER_URL` and `MORI_API_KEY` are exported; run `codex plugin info mori` to confirm hook paths |
| Skills missing | Run `codex skill list`; if absent, confirm the plugin installed successfully |
| `/brief --post-compact` not firing after compaction | Set `MORI_POST_COMPACT_BRIEF` to anything except `false`; confirm Node 18+ is in `PATH` |

---

## Notes

- **MCP is bundled.** Unlike Claude Code (where you run `claude mcp add` separately), Codex reads the bundled `mcp.json` directly from the plugin. Edit it in place after install.
- **Hooks use shared scripts.** Codex provides `CLAUDE_PLUGIN_ROOT` as a compatibility alias for `PLUGIN_ROOT`, so the same hook scripts used by the Claude Code plugin run unchanged.
- **Post-compact re-grounding** is triggered via `SessionStart` with `source=compact` — the same mechanism as Claude Code. The Codex explicit `PostCompact` event is not wired separately; `SessionStart` catches it.
- **Plugin marketplace publishing** is listed as "coming soon" by OpenAI. Until then, use the GitHub source entry shown above.
