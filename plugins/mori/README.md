# Mori Plugin — unified multi-client package

Connects **Claude Code**, **Cursor**, and **Antigravity** to a running
[mori-advisor](../../mori_advisor) server. The plugin bundles:

- **MCP connection config** — points each client at your server's `/mcp` endpoint.
- **Skills** — shared agent skills (`/brief`, `/dream`, `/wrap`, etc.) readable by all three clients.
- **Hooks** (Claude Code only for now) — SessionStart context re-grounding + telemetry shipping.

> **Server not included.** This package is the *client side* only. Deploy the
> mori-advisor server separately; see the [root README](../../README.md) for
> instructions.

---

## Directory layout

```
plugins/mori/
├── .claude-plugin/plugin.json   Claude Code manifest (userConfig prompts on enable)
├── .cursor-plugin/plugin.json   Cursor manifest
├── plugin.json                  Antigravity manifest
├── .mcp.json                    Claude Code MCP config (userConfig substitution)
├── mcp.json                     Cursor MCP config (edit url/key manually)
├── mcp_config.json              Antigravity MCP config (edit url/key manually)
├── hooks/hooks.json             Claude Code hooks (SessionStart + telemetry)
├── scripts/
│   ├── mori-context-hook.mjs    SessionStart re-grounding hook (Node ESM)
│   ├── mori-ship-event.mjs      Telemetry shipper (Node ESM; Node 18+ required)
│   └── legacy/
│       ├── uninstall-mori-claude.sh   Migration: remove legacy bespoke entries (Linux/macOS)
│       └── uninstall-mori-claude.ps1  Migration: remove legacy bespoke entries (Windows)
├── skills/                      Shared skills (copied from repo root skills/)
└── tests/
    └── test_plugin_hooks.mjs    Hermetic lifecycle tests for both hook scripts
```

---

## Install

### Claude Code

**Option A — Marketplace / `--plugin-dir` (recommended)**

```bash
# From a directory that contains the plugins/mori folder:
claude --plugin-dir plugins/mori
# Or, once marketplace support is live:
# /add-plugin https://github.com/fjwood69/mori
```

On first enable, Claude Code prompts for two values from `userConfig`:

| Prompt | What to enter |
|--------|--------------|
| **Mori server URL** | Base URL of your mori-advisor server, e.g. `http://localhost:8968` |
| **Mori API key** | Your named API key (`name:secret`). Leave blank if your server is unauthenticated. |

The API key is marked `sensitive: true` and stored in the system keychain. The URL is
stored in plain text in Claude's plugin config. Both are substituted at runtime into
`.mcp.json` and `hooks/hooks.json` via `${user_config.*}` — you do not edit those files
directly.

**Option B — Project-level manual install**

Copy `plugins/mori/` into your project root's `.claude/plugins/mori/`, then add to your
project's `.mcp.json`:

```json
{
  "mcpServers": {
    "mori": {
      "type": "http",
      "url": "http://YOUR-SERVER:8968/mcp",
      "headers": { "x-api-key": "YOUR_KEY" }
    }
  }
}
```

---

### Cursor

Copy `plugins/mori/` to `~/.cursor/plugins/local/mori/` (or the workspace
`.cursor/plugins/mori/`).

Edit `mcp.json` with your server URL and API key:

```json
{
  "mcpServers": {
    "mori": {
      "type": "http",
      "url": "http://YOUR-SERVER:8968/mcp",
      "headers": { "x-api-key": "YOUR_KEY" }
    }
  }
}
```

Cursor reads `.cursor-plugin/plugin.json` as the manifest and `mcp.json` for the MCP
connection. Skills in `skills/` are auto-discovered.

> **No userConfig substitution in Cursor** — edit `mcp.json` directly. If you prefer
> not to store the key in the file, set `MORI_API_KEY` in your shell environment and
> leave the `x-api-key` value blank; the server will fall back to checking that env var
> if configured to do so.

---

### Antigravity

Copy `plugins/mori/` to `~/.gemini/config/plugins/mori/` (global) or
`.agents/plugins/mori/` (workspace).

Edit `mcp_config.json` with your server URL and API key:

```json
{
  "mcpServers": {
    "mori": {
      "type": "http",
      "url": "http://YOUR-SERVER:8968/mcp",
      "headers": { "x-api-key": "YOUR_KEY" }
    }
  }
}
```

Antigravity reads `plugin.json` (root) as the manifest and `mcp_config.json` for the
MCP connection. Skills in `skills/` are auto-discovered.

---

## Skills

The `skills/` directory is copied from the repo root `skills/` at release time. All
three clients read the same files; no per-client skill copies are maintained.

**Known maintenance point**: `plugins/mori/skills/` is a snapshot — it will drift from
`skills/` between releases. A sync script (`scripts/sync-skills.sh`) is a planned
addition (tracked in ROADMAP). For now, re-copy manually after updating skills:

```bash
rm -rf plugins/mori/skills && cp -r skills plugins/mori/skills
```

Symlinks are intentionally avoided: plugin installers strip external symlinks when
copying the bundle to a user's local plugins directory.

---

## Hooks — SessionStart re-grounding and telemetry

Hooks are wired for **Claude Code** only. Cursor and Antigravity hook event models are
unconfirmed; they are a planned fast-follow (see TODO below).

### SessionStart re-grounding (`mori-context-hook.mjs`)

Fires once per session boundary (startup, resume, clear, or post-compaction). The script:

- **source = `compact`**: emits an `additionalContext` nudge asking the agent to run
  `/brief --post-compact` before continuing. Disable with `MORI_POST_COMPACT_BRIEF=false`.
- **source = `startup` / `resume` / `clear`**: if `MORI_SESSION_CONTEXT_FILE` is set to
  a readable file path, injects its contents as `additionalContext`. Unset by default —
  nothing is injected on a stock install.

### Telemetry shipping (`mori-ship-event.mjs`)

Ships hook event payloads to the mori server on every `PostToolUse`,
`PostToolUseFailure`, `UserPromptSubmit`, `Stop`, and `PreCompact` event. Enriches
`Stop` events with a bounded base64 tail of the session transcript (last 64 KB) so the
server's dream pipeline can extract assistant reasoning from the turn.

`PreCompact` uses `--mode precompact` which **blocks** (awaits the response) so the
server has time to persist state before the compaction snapshot is taken.

All network failures are logged to `$TMPDIR/mori-hook.log` and the script exits 0 —
hooks never interrupt the agent.

**Requirements**: Node.js 18+ (for global `fetch`). No npm packages; built-in ESM only.

### Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `MORI_POST_COMPACT_BRIEF` | _(unset)_ | Set to `false` to suppress the post-compact /brief nudge |
| `MORI_SESSION_CONTEXT_FILE` | _(unset)_ | Path to a file whose contents are injected at session start |

---

## TODO — Cursor and Antigravity hooks

- **Cursor** exposes a `workspaceOpen` event (and possibly tool events in recent builds).
  Hook format uses `hooks.json` at the plugin root. Once the Cursor event model is
  confirmed stable, add `hooks/cursor-hooks.json` wiring the same scripts.
- **Antigravity** has a `hooks.json` at the plugin root. Event names and payload shapes
  need verification against live Antigravity docs before wiring.

Neither client's hook system is confirmed at time of writing (2025-06); adding untested
hook entries risks breaking the plugin load for those clients. Raised as a tracked item
in [ROADMAP](../../ROADMAP.md).

---

## Migrating from legacy bespoke installer

If you previously installed mori using the legacy `install-mori-claude.sh` script, run
the uninstaller before enabling the plugin to avoid duplicate MCP servers and hooks:

**Linux / macOS:**
```bash
bash plugins/mori/scripts/legacy/uninstall-mori-claude.sh
# optional: pass a path if settings.json is not at the default location
bash plugins/mori/scripts/legacy/uninstall-mori-claude.sh /path/to/settings.json
```

**Windows (PowerShell):**
```powershell
.\plugins\mori\scripts\legacy\uninstall-mori-claude.ps1
# optional:
.\plugins\mori\scripts\legacy\uninstall-mori-claude.ps1 -SettingsPath "C:\...\settings.json"
```

The script prints each removed entry and writes nothing back if nothing needed removing.

---

## Running the tests

```bash
node plugins/mori/tests/test_plugin_hooks.mjs
```

Tests are hermetic — no running mori server is needed. Network calls are directed at a
dummy port; the test verifies that a connection failure still results in exit code 0.

---

## License

AGPL-3.0-only — same as the mori-advisor server. See [LICENSE](../../LICENSE).
