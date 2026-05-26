# Getting Started — Mori Claude Code Bridge

Connect your Claude Code CLI or VS Code extension to your Mori shared memory server. This gives you access to shared memories, strategic advisor tools, and the dream pipeline for session event distillation.

Mori provides automated setup scripts that guide you through an interactive configuration wizard and deploy the necessary files.

---

## Prerequisites

- Claude Code CLI or VS Code extension installed.
- Access to a running Mori server (e.g. at `http://localhost:8968` or via a Tailscale IP).
- Optional: An API key if your Mori server has `MORI_ADVISOR_API_KEY` enabled.

---

## Automated Installation (Recommended)

Run the setup script from the root of the Mori repository. The script will guide you step-by-step through configuring your server URL, API key, and client name, and then perform a connectivity test.

### Windows (PowerShell)
Open PowerShell and run:
```powershell
powershell -File scripts/install-mori-claude.ps1
```

### Linux / macOS (Bash)
Open your terminal and run:
```bash
./scripts/install-mori-claude.sh
```

### What You'll Be Asked

1. **Mori Server URL** — The address of your Mori server including port (default: `http://localhost:8968`)
2. **API Key** — Optional, skip if your server doesn't require one
3. **Client Name** — A name to identify this device in logs (default: hostname)
4. **Install Target** — Whether to install for CLI, VS Code, or both

---

## What the Script Does

### 1. Connects the MCP Server
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

### 2. Enables Event Logging Hooks
Binds agent lifecycle events (`PostToolUse`, `PostToolUseFailure`, `UserPromptSubmit`, `Stop`, and `PreCompact`) to Mori's event logging endpoints (`/api/events/raw` and `/api/precompact`). Hooks are merged per-event — any existing non-Mori hooks are preserved.

### 3. Seeds MCP Tool Permissions
Populates `permissions.allow` with all `mcp__mori__*` tool names so they run without per-call prompts. Entries are added additively — existing permissions are not removed.

### 4. Registers Custom Skills
Translates all `.skill.md` files from the `skills/` folder into Claude Code's `SKILL.md` format and deploys them to the skills directory. Already-present skills are skipped unless `--upgrade-skills` / `-UpgradeSkills` is passed.

```
skills/
  mori-brief/SKILL.md
  mori-consult/SKILL.md
  mori-dream/SKILL.md
  mori-pensieve/SKILL.md
  ...
```

---

## Command Line Options (Automation)

If you are scripting the installation or running in CI/CD, you can bypass the wizard prompts:

### PowerShell Options:
```powershell
powershell -File scripts/install-mori-claude.ps1 -MoriUrl "http://10.0.0.5:8968" -ApiKey "secret" -ClientName "my-client" -Target both -Force
```

### Bash Options:
```bash
./scripts/install-mori-claude.sh --url "http://10.0.0.5:8968" --api-key "secret" --client "my-client" --target both --force
```

Use `--target cli`, `--target vscode`, or `--target both` to select the install target without the interactive prompt. Use `-Force` / `--force` to bypass health check warnings.

### Doctor and skill upgrades

```bash
# Verify MCP config, server health, hooks, permissions, and skills (no changes)
./scripts/install-mori-claude.sh --doctor --url "http://10.0.0.5:8968"

# Refresh mori-* skills after a repo pull
./scripts/install-mori-claude.sh --upgrade-skills --url "http://10.0.0.5:8968" --client "my-client"
```

```powershell
powershell -File scripts/install-mori-claude.ps1 -Doctor -MoriUrl "http://10.0.0.5:8968"
powershell -File scripts/install-mori-claude.ps1 -UpgradeSkills -MoriUrl "http://10.0.0.5:8968" -ClientName "my-client" -Force
```

---

## Verify It's Working

1. **Reload VS Code window** after installation (Command Palette → *Developer: Reload Window*).
2. Run the doctor:
   ```bash
   ./scripts/install-mori-claude.sh --doctor --url "http://<your-server>:8968"
   ```
   ```powershell
   powershell -File scripts/install-mori-claude.ps1 -Doctor -MoriUrl "http://<your-server>:8968"
   ```
3. Confirm **mori** is connected under Settings → MCP.
4. Type `/brief` — should return memory counts and dream state from the server via MCP.
5. Check events are flowing:
   ```bash
   curl http://<your-server>:8968/api/events/health
   ```
   Event count should increase as you use Claude Code.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|----------------|-----|
| Permission prompt on every mori tool call | `permissions.allow` not seeded | Re-run installer; check doctor output |
| `/brief` returns nothing / MCP error | MCP not connected | Reload window; run doctor + confirm `mcpServers.mori` in settings.json |
| Hooks not shipping events | Shipper script missing or hook not installed | Run doctor; check `%TEMP%\mori-hook.log` (Windows) or `/tmp/mori-hook.log` |
| VS Code profile install ignored | No profiles found or wrong choice | Check `%APPDATA%\Code\User\profiles\`; re-run targeting the correct number |
| Stale `/brief` skill text | Skills not upgraded | Re-run with `--upgrade-skills` / `-UpgradeSkills` |
| Non-Mori hooks disappeared after install | Old installer version (pre-merge-fix) | Re-run current installer — hooks are now merged per-event, not replaced |

---

## Known Limitations

- **VS Code profile skills** — Skills are deployed to the profile's own `skills/` folder. If Claude Code CLI and a VS Code profile share the same server URL, the CLI skills in `~/.claude/skills/` take precedence for the CLI; VS Code reads from its own profile folder.
- **PostToolUseFailure hook** — Verify this hook is firing in your Claude Code version if you notice missing error events. `PostToolUse`, `UserPromptSubmit`, `PreCompact`, and `Stop` are confirmed working.

---

## Ingesting Files into Memory

Use `/ingest` to bootstrap the shared memory store from existing source material — PDFs, images, code, CC transcripts, or git history. Files are read on the client device and sent over the wire, so it works even when mori-advisor runs on a remote server.

Full reference and all options: [docs/reference/slash-commands.md](../reference/slash-commands.md).

---

## Upgrading from an Earlier Version

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
