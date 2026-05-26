# Getting Started — Mori Cursor Bridge

Connect your Cursor 2.4+ editor to your Mori shared memory server. This gives you access to shared memories via `/brief`, strategic advisor tools via `/consult`, and the dream pipeline for session event distillation — all without leaving Cursor.

Cursor 2.4+ natively loads Claude Code hooks from `~/.claude/settings.json` and slash commands from `.claude/skills/`. Mori's automated setup script handles both — no Claude Code required.

---

## Prerequisites

- **Cursor 2.4 or later** — earlier versions do not load hooks from `~/.claude/settings.json`.
- **Access to a running Mori server** (e.g. at `http://localhost:8968` or via a Tailscale IP).
- **Third-party skills enabled** in Cursor: Settings → Rules, Skills, Subagents → **Enable third-party skills**.
- Optional: An API key if your Mori server has `MORI_ADVISOR_API_KEY` enabled.

---

## Automated Installation (Recommended)

Run the setup script from the root of the Mori repository. The script will guide you step-by-step through configuring your server URL, API key, and client name, and then perform a connectivity test.

### Windows (PowerShell)

Open PowerShell and run:
```powershell
powershell -File scripts/install-mori-cursor.ps1
```

### Linux / macOS (Bash)

Open your terminal and run:
```bash
./scripts/install-mori-cursor.sh
```

### What You'll Be Asked

1. **Mori Server URL** — The address of your Mori server including port (default: `http://localhost:8968`)
2. **API Key** — Optional, skip if your server doesn't require one
3. **Client Name** — A name to identify this device in logs (default: hostname)

### Running Headless (Automation / CI)

```bash
./scripts/install-mori-cursor.sh --url "http://10.0.0.5:8968" --api-key "secret" --client "my-host" --force
```

```powershell
powershell -File scripts/install-mori-cursor.ps1 -MoriUrl "http://10.0.0.5:8968" -ApiKey "secret" -ClientName "my-host" -Force
```

### Doctor and skill upgrades

```bash
# Verify MCP config, server health, hooks, and skills (no changes)
./scripts/install-mori-cursor.sh --doctor --url "http://10.0.0.5:8968"

# Refresh mori-* slash skills after a repo pull
./scripts/install-mori-cursor.sh --upgrade-skills --url "http://10.0.0.5:8968"
```

```powershell
powershell -File scripts/install-mori-cursor.ps1 -Doctor -MoriUrl "http://10.0.0.5:8968"
powershell -File scripts/install-mori-cursor.ps1 -UpgradeSkills -MoriUrl "http://10.0.0.5:8968" -Force
```

Pure PowerShell on Windows — no Python required.

---

## Where shared memory lives

**Not on your laptop.** Cursor reads and writes via the remote Mori server (`mori-advisor` on GCE, homelab, etc.). The installer only creates:

| Local file | Purpose |
|------------|---------|
| `~/.cursor/mcp.json` | Points Agent MCP at `http://<server>:8968/mcp` |
| `~/.claude/settings.json` | Hooks that **ship** session events to the server |
| `~/.claude/skills/` | Slash-command workflows (`/brief`, `/wrap`, …) |

Do not look for `memories.db` under `~/ai-stack` or the mori git clone — that is not the live store for Cursor users.

---

## What the Script Does

### 1. Connects the MCP Server

Adds the `mori` MCP server to your Cursor MCP config:

| Platform | MCP Config Path |
|----------|----------------|
| **Linux / macOS** | `~/.cursor/mcp.json` |
| **Windows** | `%APPDATA%\Cursor\User\globalStorage\cursor.mcp\mcp.json` or `~\.cursor\mcp.json` |

```json
{
  "mcpServers": {
    "mori": {
      "type": "http",
      "url": "http://<mori-url>/mcp"
    }
  }
}
```

### 2. Enables Event Capture Hooks

Binds agent lifecycle events (`PostToolUse`, `PostToolUseFailure`, `UserPromptSubmit`, `Stop`, and `PreCompact`) to Mori's event logging endpoints. These hooks are written to `~/.claude/settings.json` — the same file Claude Code would use, so both editors share the same configuration.

**No Claude Code required.** If `~/.claude/settings.json` does not already exist, the installer creates it. If it exists with other hooks, Mori's hooks are merged in.

Each Mori hook entry includes `"_mori_managed": true` so re-runs can find and update Mori's hooks without matching command strings. Legacy installs (inline `curl` or shipper commands without the field) are upgraded silently on the next install.

### 3. Deploys Slash Commands

Translates all skills from the `skills/` folder into `SKILL.md` format and deploys them to `~/.claude/skills/`. Cursor loads these automatically.

| Slash Command | Description |
|---------------|-------------|
| `/brief` | Load shared memories at session start |
| `/dream` | Distil session events into structured memories |
| `/consult` | Strategic review with focus areas |
| `/pensieve` | Search the shared memory store |
| `/ingest` | Feed documents, code, or transcripts into memory |
| `/req` | Lightweight requirements tracking |
| `/nats` | Cross-device awareness via NATS messaging |
| `/wrap` | Session wrap + publish state of play |

**Ingesting files:** `/ingest` reads files from your local Cursor machine and sends them to the remote mori-advisor — no shared filesystem needed. See [docs/reference/slash-commands.md](../reference/slash-commands.md) for full options.

---

## MCP Server Config (Manual)

If you prefer to configure manually rather than using the installer, copy [.cursor/mcp.json.example](../../.cursor/mcp.json.example) to `~/.cursor/mcp.json` and set your server URL.

Then manually add the event capture hooks to `~/.claude/settings.json` (see [examples/settings.json](../examples/settings.json)) and copy the skills from `skills/` to `~/.claude/skills/`.

---

## Verify It's Working

1. **Reload Cursor window** after installation (Command Palette → *Developer: Reload Window*).
2. Run the doctor:
   ```bash
   ./scripts/install-mori-cursor.sh --doctor --url "http://<your-server>:8968"
   ```
   ```powershell
   powershell -File scripts/install-mori-cursor.ps1 -Doctor -MoriUrl "http://<your-server>:8968"
   ```
3. Open the Cursor Agent panel → confirm **mori** is connected under MCP settings.
4. Type `/brief` — should return memory counts and dream state from the server via MCP; follow with `/pensieve` or `memory_read` for detail as needed.
5. Check events are flowing:
   ```bash
   curl http://<your-server>:8968/api/events/health
   ```
   Event count should increase as you use Cursor.
6. Type `/dream --status` — dream pipeline state from the server.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|----------------|-----|
| `/brief` works on Windows but not Linux | `~/.cursor/mcp.json` missing | Run `install-mori-cursor.sh`; reload window |
| Agent guesses local `memories.db` paths | MCP not connected | Doctor + reload; memory is on the server only |
| Hooks ship events but MCP tools missing | Half-install (hooks only) | Re-run installer; check `mcp.json` |
| `jq parse error` during install (older script) | Nested Cursor hook JSON | Use current installer (`mori_cursor_install.py` merge) |
| Slash commands not listed | Third-party skills off | Settings → Features → Third-party skills |
| Stale `/brief` skill text | Skills not upgraded | `--upgrade-skills` |

---

## Known Limitations

- **PostToolUseFailure hook** — Cursor's support for this hook name has not been verified. If event capture fails silently on tool errors, Mori may miss some error events from Cursor sessions. PostToolUse, UserPromptSubmit, PreCompact, and Stop are confirmed working.
- **If hooks or skills stop working**, verify that **Third-party skills** is still enabled in Cursor Settings → Rules, Skills, Subagents. Cursor updates can reset this setting.

---

## Notes

- Mori's skills in `.claude/skills/` work in Cursor without any format changes — SKILL.md is the same format for both editors.
- Cursor also loads from `.cursor/skills/`, `.agents/skills/`, and `.codex/skills/`. No need to copy or symlink — Cursor reads `.claude/skills/` directly.
- One Mori server serves both Claude Code and Cursor simultaneously.

---

## Upgrading from an Earlier Version

If you installed Mori before the shipper-script update, your `~/.claude/settings.json` may still contain inline `curl` hook commands. Re-run the installer — it finds Mori hooks by `"_mori_managed": true`, or (for older entries) by command patterns (`mori-ship-event`, `/api/events/raw`, `/api/precompact`), then rewrites the command and sets `_mori_managed` on first merge.

The shipper scripts (`mori-ship-event.ps1` / `mori-ship-event.sh` in `~/.claude/`) provide:
- Reliable stdin capture (no subprocess pipe issues)
- Local failure logging (`%TEMP%\mori-hook.log` on Windows, `/tmp/mori-hook.log` on Linux/macOS)
- Log rotation at 100 KB
- Always exit 0 so a Mori outage never interrupts your Cursor session