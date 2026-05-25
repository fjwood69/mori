# Getting Started — Mori Cursor Bridge

Connect your Cursor 2.4+ editor to your Mori shared memory server. This gives you access to shared memories via `/brief`, strategic advisor tools via `/consult`, and the dream pipeline for session event distillation — all without leaving Cursor.

Cursor 2.4+ natively loads Claude Code hooks from `~/.claude/settings.json` and slash commands from `.claude/skills/`. Mori's automated setup script handles both — no Claude Code required.

---

## Prerequisites

- **Cursor 2.4 or later** — earlier versions do not load hooks from `~/.claude/settings.json`.
- **Access to a running Mori server** (e.g. at `http://localhost:8968` or via a Tailscale IP).
- **Third-party skills enabled** in Cursor: Settings → Features → Third-party skills → **Enable**.
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

---

## MCP Server Config (Manual)

If you prefer to configure manually rather than using the installer, create or edit `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "mori": {
      "type": "http",
      "url": "http://localhost:8968/mcp"
    }
  }
}
```

Then manually add the event capture hooks to `~/.claude/settings.json` (see [examples/settings.json](../examples/settings.json)) and copy the skills from `skills/` to `~/.claude/skills/`.

---

## Verify It's Working

1. **Restart Cursor** after installation.
2. Open the Cursor Agent panel.
3. Type `/brief` — Mori should load shared memories into the conversation context.
4. Check events are flowing:
   ```bash
   curl http://localhost:8968/api/events/health
   ```
   This should show an event count that increments as you use Cursor.
5. Type `/dream --status` — the dream pipeline state should be shown.

---

## Known Limitations

- **PostToolUseFailure hook** — Cursor's support for this hook name has not been verified. If event capture fails silently on tool errors, Mori may miss some error events from Cursor sessions. PostToolUse, UserPromptSubmit, PreCompact, and Stop are confirmed working.
- **If hooks or skills stop working**, verify that **Third-party skills** is still enabled in Cursor Settings → Features. Cursor updates can reset this setting.

---

## Notes

- Mori's skills in `.claude/skills/` work in Cursor without any format changes — SKILL.md is the same format for both editors.
- Cursor also loads from `.cursor/skills/`, `.agents/skills/`, and `.codex/skills/`. No need to copy or symlink — Cursor reads `.claude/skills/` directly.
- One Mori server serves both Claude Code and Cursor simultaneously.