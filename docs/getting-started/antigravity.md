# Getting Started — Google Antigravity IDE

Connect your Google Antigravity IDE instance to your Mori shared memory server. This allows the Antigravity agent to load shared memories, use Mori's strategic advisor tools, and feed session events into the dream pipeline.

Mori provides automated setup scripts that guide you through an interactive configuration wizard and deploy the necessary files.

---

## Prerequisites

- Google Antigravity IDE installed.
- Access to a running Mori server (e.g. at `http://localhost:8968` or via a Tailscale IP).
- Optional: An API key if your Mori server has `MORI_ADVISOR_API_KEY` enabled.

---

## Automated Installation (Recommended)

Run the setup script from the root of the Mori repository. The script will guide you step-by-step through configuring your server URL, API key, and client name, and then perform a connectivity test.

### Windows (PowerShell)
Open PowerShell and run:
```powershell
powershell -File scripts/install-mori-antigravity.ps1
```

### Linux / macOS (Bash)
Open your terminal and run:
```bash
./scripts/install-mori-antigravity.sh
```

---

## What the Script Does

The setup script automatically generates and deploys all required files:

### 1. Connects the MCP Server
Creates or updates `mcp_config.json` under your Antigravity IDE directory (`~/.gemini/antigravity/mcp_config.json`):

Without API key:
```json
{
  "mcpServers": {
    "mori": {
      "type": "http",
      "serverUrl": "http://<mori-url>/mcp"
    }
  }
}
```

With API key authentication:
```json
{
  "mcpServers": {
    "mori": {
      "type": "http",
      "serverUrl": "http://<mori-url>/mcp",
      "headers": {
        "X-Api-Key": "your-api-key"
      }
    }
  }
}
```

### 2. Enables Event Logging Hooks
Creates or updates `hooks.json` under your global config directory (`~/.gemini/config/hooks.json`):
* Binds agent lifecycle events (`PostToolUse`, `PostToolUseFailure`, `UserPromptSubmit`, `Stop`, and `PreCompact`) to Mori's event logging endpoints (`/api/events/raw` and `/api/precompact`).
* Overrides the event query with your configured client name and auth headers.

### 3. Registers Custom Skills
Creates a custom Antigravity plugin under `~/.gemini/config/plugins/mori-bridge/`:
* Deploys `plugin.json` to register the bridge.
* Translates all Claude Code `.skill.md` files from the `skills/` folder into Google Antigravity's YAML frontmatter structure (`SKILL.md`) under `mori-bridge/skills/mori-<name>/SKILL.md`.

---

## Command Line Customizations (Automation)

If you are scripting the installation or running in CI/CD, you can bypass the wizard prompts by passing arguments:

### PowerShell Options:
```powershell
powershell -File scripts/install-mori-antigravity.ps1 -MoriUrl "http://10.0.0.5:8968" -ApiKey "secret" -ClientName "my-client" -Force -UpgradeSkills
```

### Bash Options:
```bash
./scripts/install-mori-antigravity.sh --url "http://10.0.0.5:8968" --api-key "secret" --client "my-client" --force --upgrade-skills
```

Use the `-Force` / `--force` switch to bypass interactive prompts if the server is offline during the setup.
Use the `-UpgradeSkills` / `--upgrade-skills` switch to force overwriting existing skills in the plugin folder (by default, the installer skips existing skills to protect manual edits).

---

## Doctor Mode (Diagnostics)

Validate your installation using the doctor check:

### PowerShell:
```powershell
powershell -File scripts/install-mori-antigravity.ps1 -Doctor -MoriUrl "http://localhost:8968"
```

### Bash:
```bash
./scripts/install-mori-antigravity.sh --doctor --url "http://localhost:8968"
```

---

## Upgrading from an Earlier Version

Re-running the installer upgrades event capture hooks and skills automatically:
* It merges event logging hooks into `hooks.json` cleanly, preserving other third-party hooks.
* It deploys `mori-ship-event.sh` (Linux/macOS) or `mori-ship-event.ps1` (Windows) to `~/.gemini/config/plugins/mori-bridge/`.

The shipper scripts provide:
- Reliable stdin capture (no subprocess pipe issues)
- Local failure logging (`%TEMP%\mori-hook.log` on Windows, `/tmp/mori-hook.log` on Linux/macOS)
- Log rotation at 100 KB
- Always exit 0 so a Mori outage never interrupts your Antigravity session
