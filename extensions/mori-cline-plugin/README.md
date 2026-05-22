# Mori Cline Plugin — v0.1.2

Ships Cline session events (prompts, tool calls, session lifecycle) to Mori's
dream pipeline for cross-session memory distillation. Works alongside the
mori-shipper VS Code extension, which captures Continue and OpenCode sessions
via file watching.

All events are fire-and-forget — Mori failures never block or slow Cline.

## Quick Start — 4 Steps

### 1. Clone the repo

```bash
git clone https://github.com/fjwood69/mori.git
cd mori
```

### 2. Set environment variables

The plugin posts events to the Mori advisor server on a GCE VM via Tailscale.
Set these before launching Cline:

**Linux / macOS:**
```bash
export MORI_API_URL=<mori-server-url>
export MORI_API_KEY=<mori-api-key>
export MORI_CLIENT=$(hostname)
```

**Windows (PowerShell):**
```powershell
$env:MORI_API_URL = "<mori-server-url>"
$env:MORI_API_KEY = "<mori-api-key>"
$env:MORI_CLIENT  = "twiggy"
```

To make these permanent on Windows, add them as User environment variables:
`Win+R` → `sysdm.cpl` → Advanced → Environment Variables → New (under User variables).

### 3. Register the plugin

**Via Cline CLI:**
```bash
clite plugin install /path/to/mori/extensions/mori-cline-plugin
```

**Via VS Code settings.json** (`Ctrl+Shift+P` → Preferences: Open User Settings (JSON)):
```json
{
  "cline.agentRuntimePlugins": [
    "/path/to/mori/extensions/mori-cline-plugin/dist/mori-plugin.js"
  ]
}
```

### 4. Add Mori MCP server

Still in `settings.json`, add the Mori MCP server so `/pensieve`, `/dream`,
`/brief`, `/consult`, `/req` slash commands work:

```json
{
  "cline.mcpServers": {
    "mori": {
      "type": "http",
      "url": "<mori-server-url>/mcp"
    }
  }
}
```

If you already have `mcpServers` in your settings, add the `"mori"` entry to it.

### 5. Reload and verify

1. **Restart VS Code** completely (not just reload window)
2. Start a Cline session and send a message
3. The plugin works silently — verify events are flowing:

```bash
curl <mori-server-url>/api/events/health
```

If `total_events` is incrementing, the plugin is working.

## Slash Commands

If you also use Claude Code, symlink its skills into Cline:

**Linux / macOS:**
```bash
ln -s ~/.claude/skills ~/.cline/skills
```

**Windows (PowerShell, run as Admin):**
```powershell
New-Item -ItemType SymbolicLink -Path "$env:USERPROFILE\.cline\skills" -Target "$env:USERPROFILE\.claude\skills" -Force
```

If admin isn't available, copy instead:
```powershell
Copy-Item -Recurse "$env:USERPROFILE\.claude\skills" "$env:USERPROFILE\.cline\skills" -Force
```

Then these work in Cline:
| Command | What it does |
|---------|-------------|
| `/pensieve` | Search shared memories |
| `/dream` | Run dream distillation now |
| `/brief` | Load shared knowledge for this session |
| `/consult` | Get strategic guidance on a question |
| `/req` | Project requirements tracking |

## How the Plugin Works

| SDK Hook | Mori Event | What it captures |
|----------|-----------|-----------------|
| `beforeRun` | — | Replays any spooled events from previous offline sessions |
| `beforeModel` | `UserPromptSubmit` | Your prompt text |
| `afterTool` | `PostToolUse` | Tool name, input, output, and error status |
| `afterRun` | `Stop` + dream flush | Session end signal, then triggers dream distillation |

Events are fire-and-forget with a disk-backed spooler for offline resilience:
- Spool directory: `~/.mori/queue/`
- Retry schedule: 10s, 30s, 1m, 2m, 5m, 10m (capped)
- Max 10 retries, then dead-lettered to `~/.mori/dead/`
- `beforeRun` replays any pending spools at session start

## Verification

```bash
# Check events are flowing
curl <mori-server-url>/api/events/health

# Trigger dream manually
curl -X POST <mori-server-url>/api/dream/run
```

## Project Structure

```
extensions/mori-cline-plugin/
├── package.json
├── tsconfig.json
├── src/mori-plugin.ts     # Source (TypeScript)
├── dist/mori-plugin.js    # Built plugin (committed, no build needed)
└── README.md
```
