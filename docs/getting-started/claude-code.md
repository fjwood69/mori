# Getting Started — Mori Claude Code Bridge

Connect your Claude Code CLI or VS Code extension to your Mori shared memory server. This gives you access to shared memories, strategic advisor tools, and the dream pipeline for session event distillation.

---

## Install as a Plugin (Recommended)

Mori ships as a native Claude Code plugin from the `fjwood69/mori` marketplace. The plugin handles MCP connection, hooks, skills, and credentials in one step — no scripts to clone or run.

### Step 1 — Add the marketplace

Inside Claude Code, run:

```
/plugin marketplace add fjwood69/mori
```

### Step 2 — Install the plugin

```
/plugin install mori@mori
```

On enable, Claude Code prompts for two values:

| Prompt | What to enter |
|--------|--------------|
| **Mori server URL** | Base URL of your mori-advisor server, e.g. `http://localhost:8968` |
| **Mori API key** | Your named API key (`name:secret`). Leave blank if the server is unauthenticated. |

The API key is marked `sensitive: true` and stored in the OS keychain — it is never written to any config file. The `MORI_API_KEY` environment variable is an alternative for headless or CI environments.

### Step 3 — Reload

```
/reload-plugins
```

Or restart Claude Code. That's it — MCP tools, hooks, and skills are all live.

### Local development / testing

```bash
claude --plugin-dir ./plugins/mori
```

Point `--plugin-dir` at the `plugins/mori/` package in a local clone. Useful for testing changes to hooks or skills before publishing.

---

## What the Plugin Provides

### MCP connection

The plugin wires the `mori` MCP server automatically using `userConfig` substitution — you do not edit any JSON config files directly.

### Hooks

| Hook | Purpose |
|------|---------|
| `SessionStart` (`source: "compact"`) | Emits a context nudge asking the agent to run `/brief --post-compact` after compaction. Disable with `MORI_POST_COMPACT_BRIEF=false`. |
| `SessionStart` (`source: "startup"` / `"resume"` / `"clear"`) | Optionally injects a context file via `MORI_SESSION_CONTEXT_FILE`. |
| `PostToolUse`, `PostToolUseFailure`, `UserPromptSubmit`, `Stop` | Ship event payloads to `/api/events/raw`. `Stop` includes a bounded transcript tail for dream pipeline extraction. |
| `PreCompact` | Ships a blocking pre-compaction event so the server persists state before the snapshot. |

> **Note on post-compaction re-grounding**: the re-ground is triggered by a **SessionStart hook** checking `source: "compact"` — not a PostCompact hook. PostCompact fires for observability only and cannot inject context into the session. The SessionStart hook is the correct mechanism.

### Skills

Deployed to Claude Code's skills directory:

| Command | Description |
|---------|-------------|
| `/brief` | Session bootstrap (full or `--post-compact` delta) |
| `/dream` | Distil session events into memories |
| `/consult` | Strategic review |
| `/pensieve` | Search memory store |
| `/ingest` | Ingest local files into remote store |
| `/req` | Requirements tracking |
| `/nats` | Cross-device NATS messages |
| `/msg` | Inter-agent inbox |
| `/wrap` | End-of-session publish + dream flush |

### Permissions

All `mcp__mori__*` tool names are pre-seeded in `permissions.allow` — no per-call prompts.

---

## Legacy Installer (Alternative Path)

> The plugin is the recommended install path. The installer scripts below are the legacy approach, now superseded. They remain documented for users who prefer a script-driven setup or cannot use the plugin marketplace (e.g. air-gapped environments).

Clone the Mori repository and run the setup script. The script guides you through server URL, API key, and client name, then performs a connectivity test.

### Windows (PowerShell)

```powershell
powershell -File scripts/legacy/install-mori-claude.ps1
```

### Linux / macOS (Bash)

```bash
./scripts/legacy/install-mori-claude.sh
```

### What You'll Be Asked

1. **Mori Server URL** — The address of your Mori server including port (default: `http://localhost:8968`)
2. **API Key** — Your named key from `MORI_API_KEYS` on the server. Skip only if the server is running in open mode (no keys configured). Generate a key: `python3 -c "import secrets; print(secrets.token_hex(32))"` or use `mori-key_generate name="myhostname"` via MCP once connected.
3. **Client Name** — A name to identify this device in logs (default: hostname)
4. **Install Target** — Whether to install for CLI, VS Code, or both

### What the Script Does

#### 1. Connects the MCP Server
Adds the `mori` MCP server to your Claude Code settings:

| Target | Config File |
|--------|------------|
| **CLI** | `~/.claude/settings.json` or `$CLAUDE_CONFIG_DIR/settings.json` (Linux/macOS) / `%USERPROFILE%\.claude\settings.json` (Windows) |
| **VS Code** | `~/.config/Code/User/settings.json` (Linux) / `%APPDATA%\Code\User\settings.json` (Windows) |

For VS Code, the script also detects any named profiles and offers to install into a specific profile instead.

```json
{
  "mcpServers": {
    "mori": {
      "type": "http",
      "url": "http://<mori-url>/mcp"
    }
  },
  ...
}
```

#### 2. Enables Event Logging Hooks
Binds agent lifecycle events (`PostToolUse`, `PostToolUseFailure`, `UserPromptSubmit`, `Stop`, `PreCompact`) to Mori's event logging endpoints (`/api/events/raw` and `/api/precompact`). Hooks are merged per-event — any existing non-Mori hooks are preserved. As of v2.1.24 the `Stop` hook also ships a bounded transcript tail, from which the server extracts the turn's assistant reasoning (plans, analysis, decisions).

Post-compaction re-grounding is handled by a **SessionStart hook** that checks `source: "compact"` and prompts you to run `/brief --post-compact`. It is enabled by default. To disable it:

```bash
export MORI_POST_COMPACT_BRIEF=false
```

#### 3. Seeds MCP Tool Permissions
Populates `permissions.allow` with all `mcp__mori__*` tool names so they run without per-call prompts. Entries are added additively — existing permissions are not removed.

#### 4. Registers Custom Skills
Translates all `.skill.md` files from the `skills/` folder into Claude Code's `SKILL.md` format and deploys them to the skills directory. Already-present skills are skipped unless `--upgrade-skills` / `-UpgradeSkills` is passed.

```
skills/
  brief/SKILL.md
  consult/SKILL.md
  dream/SKILL.md
  ingest/SKILL.md
  msg/SKILL.md
  nats/SKILL.md
  pensieve/SKILL.md
  req/SKILL.md
  wrap/SKILL.md
```

### Command Line Options (Automation)

If you are scripting the installation or running in CI/CD, you can bypass the wizard prompts:

#### PowerShell Options:
```powershell
powershell -File scripts/legacy/install-mori-claude.ps1 -MoriUrl "http://10.0.0.5:8968" -ApiKey "secret" -ClientName "my-client" -Target both -Force
```

#### Bash Options:
```bash
./scripts/legacy/install-mori-claude.sh --url "http://10.0.0.5:8968" --api-key "secret" --client "my-client" --target both --force
```

Use `--target cli`, `--target vscode`, or `--target both` to select the install target without the interactive prompt. Use `-Force` / `--force` to bypass health check warnings.

### Doctor and skill upgrades

```bash
# Verify MCP config, server health, hooks, permissions, and skills (no changes)
./scripts/legacy/install-mori-claude.sh --doctor --url "http://10.0.0.5:8968"

# Refresh mori-* skills after a repo pull
./scripts/legacy/install-mori-claude.sh --upgrade-skills --url "http://10.0.0.5:8968" --client "my-client"
```

```powershell
powershell -File scripts/legacy/install-mori-claude.ps1 -Doctor -MoriUrl "http://10.0.0.5:8968"
powershell -File scripts/legacy/install-mori-claude.ps1 -UpgradeSkills -MoriUrl "http://10.0.0.5:8968" -ClientName "my-client" -Force
```

---

## Verify It's Working

1. **Reload VS Code window** after installation (Command Palette → *Developer: Reload Window*).
2. Confirm **mori** is connected under Settings → MCP.
3. Type `/brief` — should return memory counts and dream state from the server via MCP.
4. Check events are flowing:
   ```bash
   curl http://<your-server>:8968/api/events/health
   ```
   Event count should increase as you use Claude Code.

---

## Memory Store

The Mori server uses a **dual-backend** store: SQLite for solo or synchronous setups, and Postgres for team or asynchronous deployments. Shared memory lives on the server — not on your local machine. Do not use a local `memories.db` from an old clone or sidecar install.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|----------------|-----|
| Permission prompt on every mori tool call | `permissions.allow` not seeded | Re-run installer or reinstall plugin; check doctor output |
| `/brief` returns nothing / MCP error | MCP not connected | Reload window; confirm `mcpServers.mori` in settings.json or plugin MCP config |
| Hooks not shipping events | Shipper script missing or hook not installed | Run doctor; check `%TEMP%\mori-hook.log` (Windows) or `/tmp/mori-hook.log` |
| VS Code profile install ignored | No profiles found or wrong choice | Check `%APPDATA%\Code\User\profiles\`; re-run targeting the correct number |
| Stale `/brief` skill text | Skills not upgraded | Re-run with `--upgrade-skills` / `-UpgradeSkills` or reinstall plugin |
| Non-Mori hooks disappeared after install | Old installer version (pre-merge-fix) | Re-run current installer — hooks are now merged per-event, not replaced |

---

## Known Limitations

- **VS Code profile skills** — Skills are deployed to the profile's own `skills/` folder. If Claude Code CLI and a VS Code profile share the same server URL, the CLI skills in `~/.claude/skills/` take precedence for the CLI; VS Code reads from its own profile folder.
- **PostToolUseFailure hook** — Verify this hook is firing in your Claude Code version if you notice missing error events. `PostToolUse`, `UserPromptSubmit`, `PreCompact`, and `Stop` are confirmed working.
- **Plugin hooks require Node.js 18+** — the plugin hooks are Node ESM scripts (`mori-context-hook.mjs`, `mori-ship-event.mjs`). The legacy installer scripts use shell/PowerShell instead.

---

## Ingesting Files into Memory

Use `/ingest` to bootstrap the shared memory store from existing source material — PDFs, images, code, CC transcripts, or git history. Files are read on the client device and sent over the wire, so it works even when mori-advisor runs on a remote server.

Full reference and all options: [docs/reference/slash-commands.md](../reference/slash-commands.md).

---

## Upgrading from the Legacy Installer to the Plugin

If you previously installed Mori using `install-mori-claude.sh` / `.ps1`, run the uninstaller before enabling the plugin to avoid duplicate MCP server entries and hooks:

**Linux / macOS:**
```bash
bash plugins/mori/scripts/legacy/uninstall-mori-claude.sh
```

**Windows (PowerShell):**
```powershell
.\plugins\mori\scripts\legacy\uninstall-mori-claude.ps1
```

Then follow the plugin install steps above.

---

## Upgrading the Legacy Installer

If you installed Mori before the shipper-script update, your `settings.json` will contain inline curl hook commands like:

```
"curl -sf -X POST \"http://...\" -d @- >/dev/null 2>&1; exit 0"
```

Re-running the installer upgrades them automatically. The installer checks whether `mori-ship-event.sh` (Linux/macOS) or `mori-ship-event.ps1` (Windows) is already present in your hook commands. Since the old curl-based hooks do not match, the installer replaces them with the new shipper-script pattern and deploys the shipper to `~/.claude/`.

The shipper scripts provide:
- Reliable stdin capture (no subprocess pipe issues)
- Local failure logging to `%TEMP%\mori-hook.log` (Windows) or `/tmp/mori-hook.log` (Linux/macOS)
- Log rotation at 100 KB
- Always exit 0 so a Mori outage never interrupts your AI session


---

## Optional: Git push notifications

Install the post-push hook in your repos to broadcast a `GitPush` event to NATS whenever you push — every other active instance sees it at the next `/brief`. See [docs/reference/git-hooks.md](../reference/git-hooks.md).
