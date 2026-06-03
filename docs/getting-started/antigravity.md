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
Creates or updates `mcp_config.json` under your Antigravity target directory (`~/.gemini/antigravity/mcp_config.json` or `~/.gemini/antigravity-ide/mcp_config.json`):

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
Creates or updates `hooks.json` under your config directory (`~/.gemini/antigravity/hooks.json` or `~/.gemini/antigravity-ide/hooks.json`):
* Binds agent lifecycle events (`PostToolUse`, `PostToolUseFailure`, `UserPromptSubmit`, `Stop`, `PreCompact`, and `PostCompact`) to Mori's event logging shipper (`mori-ship-event.sh` or `mori-ship-event.ps1`) and the re-grounding shipper (`mori-post-compact-brief.sh` or `mori-post-compact-brief.ps1`). As of v2.1.24 the `Stop` hook also ships a bounded transcript tail, from which the server extracts the turn's assistant reasoning (plans, analysis, decisions).
* Overrides the event query with your configured client name and auth headers.
* Adds `"_mori_managed": true` to each hook entry so re-runs can find and update Mori's hooks cleanly without matching command strings.

### 3. Registers Custom Skills
Creates a custom Antigravity plugin under your plugins directory:
* Deploys `plugin.json` to register the bridge.
* Translates all Claude Code `.skill.md` files from the `skills/` folder into Google Antigravity's YAML frontmatter structure (`SKILL.md`) under `mori-bridge/skills/mori-<name>/SKILL.md`.

---

## Command Line Customizations (Automation)

If you are scripting the installation or running in CI/CD, you can bypass the wizard prompts by passing arguments:

### PowerShell Options:
```powershell
powershell -File scripts/install-mori-antigravity.ps1 -MoriUrl "http://10.0.0.5:8968" -ApiKey "secret" -ClientName "my-client" -Target "ide" -Force -UpgradeSkills
```

### Bash Options:
```bash
./scripts/install-mori-antigravity.sh --url "http://10.0.0.5:8968" --api-key "secret" --client "my-client" --target "ide" --force --upgrade-skills
```

Use the `-Target` / `--target` option to specify `'cli'` (`~/.gemini/antigravity`), `'ide'` (`~/.gemini/antigravity-ide`), or `'both'` (default is `'ide'` on Windows and interactive on Linux/macOS).
Use the `-Force` / `--force` switch to bypass interactive prompts if the server is offline during the setup.
Use the `-UpgradeSkills` / `--upgrade-skills` switch to force overwriting existing skills in the plugin folder (by default, the installer skips existing skills to protect manual edits).

---

## Doctor Mode (Diagnostics)

Validate your installation using the doctor check:

### PowerShell:
```powershell
powershell -File scripts/install-mori-antigravity.ps1 -Doctor -MoriUrl "http://localhost:8968" -Target "ide"
```

### Bash:
```bash
./scripts/install-mori-antigravity.sh --doctor --url "http://localhost:8968" --target "ide"
```

---

## Upgrading from an Earlier Version

Re-running the installer upgrades event capture hooks and skills automatically:
* It merges event logging hooks into `hooks.json` cleanly, preserving other third-party hooks.
* It deploys shipper scripts (`mori-ship-event` and `mori-post-compact-brief`) to the plugins directory.
* It updates legacy hook entries (inline curl or shipper commands without the field) with the `"_mori_managed": true` flag on the first re-run.

The shipper scripts provide:
- Reliable stdin capture (no subprocess pipe issues)
- Local failure logging (`%TEMP%\mori-hook.log` on Windows, `/tmp/mori-hook.log` on Linux/macOS)
- Log rotation at 100 KB
- PostCompact re-grounding: triggers `/brief` automatically after context compression to keep the agent contextualized.
- Always exit 0 so a Mori outage never interrupts your Antigravity session

---

## Usage (Using Mori in Antigravity)

Once installed, the easiest way to interact with Mori is by using the custom slash commands registered by the plugin's skills. In the Antigravity chat UI, you can call them directly:

* **`/mori-brief`**: Session bootstrap. Loads recent/canonical shared memories, checks team standards, and verifies server status. (Equivalent to `/brief` in Cursor/Claude Code)
* **`/mori-consult`**: Requests strategic guidance from the Mori Advisor model (e.g., `/mori-consult --focus architecture "Review my current approach"`).
* **`/mori-req`**: Manages project requirements and delivery tracking.
* **`/mori-dream`**: Manages and runs the dream distillation pipeline.
* **`/mori-pensieve`**: Performs a search query across the shared memory store.
* **`/mori-nats`**: Real-time cross-device messaging awareness tools.
* **`/mori-ingest`**: Feeds documents, code, transcripts, or git history into the shared memory store — reads files from this device, works with remote mori-advisor instances. Full reference: [docs/reference/slash-commands.md](../reference/slash-commands.md).

Using these slash commands instructs the agent to invoke the underlying Mori MCP tools automatically, presenting the context directly inside your session.



---

## Optional: Git push notifications

Install the post-push hook in your repos to broadcast a `GitPush` event to NATS whenever you push — every other active instance sees it at the next `/brief`. See [docs/reference/git-hooks.md](../reference/git-hooks.md).
