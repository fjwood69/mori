# Mori Cline Plugin — v0.1.2

Ships lifecycle events (prompts, tool calls, session lifecycle) from Cline to
Mori's dream pipeline for cross-session memory distillation.

## How It Works

The plugin hooks into Cline's agent runtime via the SDK's `AgentRuntimePlugin`
interface. Four hooks capture events and POST them to the Mori advisor server:

| SDK Hook | Mori Event | What it captures |
|----------|-----------|-----------------|
| `beforeRun` | — | Replays any spooled events from previous offline sessions |
| `beforeModel` | `UserPromptSubmit` | Your prompt text, extracted from the pending model request |
| `afterTool` | `PostToolUse` | Tool name, input, output, and error status |
| `afterRun` | `Stop` + dream flush | Session end signal, then triggers dream distillation |

All events are fire-and-forget — Mori failures never block or slow Cline.

## Installation

### Via Cline CLI

```bash
cline plugin install ./extensions/mori-cline-plugin
```

### Global install (all projects)

```bash
cp -r extensions/mori-cline-plugin ~/.cline/plugins/
```

### Or manual

```bash
cd extensions/mori-cline-plugin
npm install
npm run build
```

Then reference `dist/mori-plugin.js` in your Cline config.

## Configuration

Set these environment variables before launching Cline:

| Variable | Default | Description |
|----------|---------|-------------|
| `MORI_API_URL` | `http://localhost:8968` | Mori advisor server base URL |
| `MORI_API_KEY` | `""` | API key sent as `X-Api-Key` header |
| `MORI_CLIENT` | `os.hostname()` | Client name sent as `?client=` query param |

```bash
export MORI_API_URL=<mori-server-url>
export MORI_API_KEY=<mori-api-key>
export MORI_CLIENT=$(hostname)
```

Windows (PowerShell):

```powershell
$env:MORI_API_URL = "<mori-server-url>"
$env:MORI_API_KEY = "<mori-api-key>"
$env:MORI_CLIENT  = "<hostname>"
```

## MCP Server Config for Cline

Add Mori as an MCP server in Cline's settings so `/brief`, `/dream`, `/consult`,
`/pensieve`, `/wrap`, `/req` are available as slash commands:

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

## Skills

Mori's existing Claude Code skills work in Cline without modification — Cline
reads from `.claude/skills/` as a valid skill location. Copy or symlink:

```bash
# Cline also reads .cline/skills/ — symlink from CC skills
ln -s .claude/skills .cline/skills
```

This gives Cline users `/brief`, `/dream`, `/consult`, `/pensieve`, `/wrap`,
`/req` slash commands identical to CC.

## Offline Resilience

Events that fail to reach the Mori server are spooled to disk:

- Spool directory: `~/.mori/queue/`
- Retry schedule: 10s, 30s, 1m, 2m, 5m, 10m (capped at 10m)
- Max retries: 10, then dead-lettered to `~/.mori/dead/`
- At the start of your next session, `beforeRun` replays all pending spools

## Building from Source

```bash
cd extensions/mori-cline-plugin
npm install
npm run build
# Output: dist/mori-plugin.js
```

## Verification

1. Confirm Mori advisor is running:
   ```bash
   curl http://localhost:8968/health
   ```

2. Install the plugin and start a Cline session.

3. Send a message and check events landed:
   ```bash
   curl http://localhost:8968/api/events/health
   # total_events should increment
   ```

4. Check event types in the database:
   ```bash
   sqlite3 /data/mori-advisor/memories.db \
     "SELECT event_name FROM session_events ORDER BY id DESC LIMIT 5"
   # Should show: UserPromptSubmit, PostToolUse, Stop
   ```

5. End the session — dream flush fires automatically:
   ```bash
   curl http://localhost:8968/api/events/health
   # Dream state should show updated memory count
   ```

## Out of Scope for v0.1.2

- VS Code extension — `mori-shipper` (v0.1.1) handles Continue and OpenCode
- PreCompact hook — Cline has a `custom-compaction` mechanism; evaluate in v0.1.3
- NATS integration — handled server-side by the Mori advisor
- Plugin publishing to npm — manual install for now