# Getting Started — Mori Cursor Bridge

Connect Cursor 2.4+ to your Mori shared memory server: `/brief`, `/consult`, `/dream`, event capture, and cross-device messaging.

---

## Prerequisites

- **Cursor 2.4+** — loads `~/.claude/skills/` and reads the plugin's `mcp.json`.
- **Mori server** reachable (homelab, GCE, Tailscale, etc.).
- **Third-party skills enabled:** Settings → Rules, Skills, Subagents → **Enable third-party skills**.
- Optional: API key if the server uses `MORI_ADVISOR_API_KEY`.

---

## Install as a Plugin (Recommended)

Mori ships as a unified plugin package at `plugins/mori/`. It includes a Cursor-specific manifest (`.cursor-plugin/plugin.json`), an MCP config (`mcp.json`), and shared skills — all from a single package.

> **Hooks are a fast-follow for Cursor.** The Cursor hook event model is being verified against live Cursor docs before wiring. MCP tools and skills work fully today.

### Install from a local clone

```bash
# Clone or pull the repo, then copy the plugin package:
cp -r plugins/mori ~/.cursor/plugins/local/mori
```

Or symlink for easier updates (local dev only):

```bash
ln -s "$(pwd)/plugins/mori" ~/.cursor/plugins/local/mori
```

For workspace-scoped install, place it at `.cursor/plugins/mori/` in your project root instead.

### Configure your server URL and API key

Edit `~/.cursor/plugins/local/mori/mcp.json`:

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

If you prefer not to store the key in the file, set `MORI_API_KEY` in your shell environment and omit the `x-api-key` header.

### Reload Cursor

Command Palette → *Developer: Reload Window*. Confirm **mori** appears under Settings → MCP.

---

## Already set up via Claude Code?

If you use Claude Code on this machine, **Mori skills may already be deployed** under `~/.claude/`. Cursor reuses that path.

Check what you have:

```bash
ls ~/.claude/skills/
```

| You see | Cursor needs |
|---------|----------------|
| `brief`, `wrap`, `msg`, … under `~/.claude/skills/` | Skills OK |
| Nothing under `~/.cursor/plugins/` | Install the plugin (above) |

Shared memory lives on the **Mori server** — not on this laptop. Do not use a local `memories.db` from an old clone or sidecar install — that is not the live store.

---

## Legacy Installer (Alternative Path)

> The plugin package is the recommended install path. The installer scripts below are the legacy approach, now superseded. They remain documented for users who prefer a script-driven setup or cannot use the plugin marketplace.

Run from the **mori** repo root.

### Linux / macOS

```bash
./scripts/install-mori-cursor.sh --url "http://<your-server>:8968" \
  --api-key "<key-if-required>" --client "$(hostname)" --force
```

### Windows (PowerShell)

```powershell
powershell -File scripts/install-mori-cursor.ps1 `
  -MoriUrl "http://<your-server>:8968" `
  -ApiKey "<key-if-required>" `
  -ClientName $env:COMPUTERNAME `
  -Force
```

### Doctor and skill refresh

```bash
./scripts/install-mori-cursor.sh --doctor --url "http://<your-server>:8968"
./scripts/install-mori-cursor.sh --upgrade-skills --url "http://<your-server>:8968"
```

```powershell
powershell -File scripts/install-mori-cursor.ps1 -Doctor -MoriUrl "http://<your-server>:8968"
powershell -File scripts/install-mori-cursor.ps1 -UpgradeSkills -MoriUrl "http://<your-server>:8968" -Force
```

Windows installer is pure PowerShell (no Python).

### What the legacy installer writes

| Path | Purpose |
|------|---------|
| `~/.cursor/mcp.json` | HTTP MCP → `http://<server>:8968/mcp` |
| `~/.claude/settings.json` | Mori hooks (`_mori_managed`) + MCP `permissions.allow` |
| `~/.claude/mori-ship-event.*` | Event shipper |
| `~/.claude/skills/<name>/` | Copies from `mori/skills/<name>/SKILL.md` |

### Mori hooks (managed by legacy installer)

| Hook | Purpose |
|------|---------|
| `PostToolUse`, `PostToolUseFailure`, `UserPromptSubmit`, `Stop` | Ship events → `/api/events/raw` |
| `PreCompact` | Pre-compaction ship + dream (`--mode precompact`) |

Post-compaction re-grounding is handled by a **SessionStart hook** that checks `source: "compact"` and prompts the agent to run `/brief --post-compact`. The legacy installer deploys this as a shell script; re-runs upgrade legacy inline `curl` hooks to the shipper and set `_mori_managed: true` without removing other hook entries.

---

## Mori slash commands (skills)

Available in both plugin and legacy installs:

| Command | Description |
|---------|-------------|
| `/brief` | Session bootstrap (full or `--post-compact` delta) |
| `/dream` | Distil session events into memories |
| `/consult` | Strategic review |
| `/pensieve` | Search memory store |
| `/ingest` | Ingest local files into remote store |
| `/req` | Requirements tracking |
| `/nats` | Cross-device NATS messages |
| `/msg` | Inter-agent inbox (tasks, questions) |
| `/wrap` | End-of-session publish + dream flush |

See [slash-commands.md](../reference/slash-commands.md) for full options.

---

## Memory Store

The Mori server uses a **dual-backend** store: SQLite for solo or synchronous setups, and Postgres for team or asynchronous deployments. Shared memory lives on the server — not on this machine.

---

## Verify

1. **Reload Cursor window** (Command Palette → *Developer: Reload Window*).
2. **MCP** — Settings → MCP → `mori` connected.
3. **`/brief`** — counts + dream state from server via MCP.
4. **Events** — `curl http://<server>:8968/api/events/health` (count increases as you use Agent).
5. **`/dream --status`** — pipeline state on server.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|----------------|-----|
| `/brief` works in Claude Code but not Cursor | MCP not configured for Cursor | Install plugin or add `~/.cursor/mcp.json`; reload window |
| Agent cites local `memories.db` | MCP not connected | Doctor; memory is server-side only |
| MCP tools blocked | Incomplete install | Re-check plugin `mcp.json` or legacy `permissions.allow` |
| Stale slash commands | Old `~/.claude/skills` copy | Reinstall plugin or run `install-mori-cursor.sh --upgrade-skills` |
| No re-ground after compaction | SessionStart hook not wired | Plugin hooks for Cursor are a fast-follow; use legacy installer for hook support now |

---

## Known limitations

- **Hooks** — Cursor hook wiring is a fast-follow; not yet confirmed stable in the plugin. MCP tools and skills work today.
- **PostToolUseFailure** — not verified on Cursor.
- **Third-party skills** — Cursor updates can disable this; re-check Settings → Rules, Skills, Subagents.

---

## Notes

- Cursor reads `~/.claude/skills/` directly (also `.cursor/skills/` if you add skills there).
- The plugin's `skills/` directory is shared with Claude Code and Antigravity — no per-client copies.
- Optional: [git-hooks.md](../reference/git-hooks.md) for NATS push notifications on `git push`.

---

## Upgrading

**Plugin users**: pull the repo and re-copy `plugins/mori/` to `~/.cursor/plugins/local/mori/`.

**Legacy installer users**: re-run `install-mori-cursor` (use `--upgrade-skills` to refresh skill files). Hook failures: `%TEMP%\mori-hook.log` (Windows) or `/tmp/mori-hook.log` (Linux/macOS).
