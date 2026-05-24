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
Binds agent lifecycle events (`PostToolUse`, `PostToolUseFailure`, `UserPromptSubmit`, `Stop`, and `PreCompact`) to Mori's event logging endpoints (`/api/events/raw` and `/api/precompact`).

### 3. Registers Custom Skills
Translates all `.skill.md` files from the `skills/` folder into Claude Code's `SKILL.md` format and deploys them to the skills directory for your selected target.

```
skills/
  mori-brief/SKILL.md
  mori-consult/SKILL.md
  mori-dream/SKILL.md
  mori-pensieve/SKILL.md
  ...
```

---

## Command Line Customizations (Automation)

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
