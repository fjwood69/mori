# Getting Started — Google Antigravity IDE

Connect your Google Antigravity IDE instance to your Mori shared memory server. This allows the Antigravity agent to load shared memories, use Mori's strategic advisor tools, and feed session events into the dream pipeline.

---

## Prerequisites

- Google Antigravity IDE installed.
- Access to a running Mori server (e.g. at `http://localhost:8968` or via a Tailscale IP).
- Optional: An API key if your Mori server has `MORI_ADVISOR_API_KEY` enabled.

---

## Install as a Plugin (Recommended)

Mori ships as a unified plugin package at `plugins/mori/`. It includes an Antigravity-specific manifest (`plugin.json` at root), an MCP config (`mcp_config.json`), and shared skills — all from a single package.

### 1. Copy the plugin directory
#### Global install

```bash
cp -r plugins/mori ~/.gemini/config/plugins/mori
```

### Workspace-scoped install

```bash
cp -r plugins/mori .agents/plugins/mori
```

### Configure your server URL and API key

Edit `~/.gemini/config/plugins/mori/mcp_config.json` (or the workspace copy):

Without API key:
```json
{
  "mcpServers": {
    "mori": {
      "type": "http",
      "serverUrl": "http://YOUR-SERVER:8968/mcp"
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
      "serverUrl": "http://YOUR-SERVER:8968/mcp",
      "headers": {
        "X-Api-Key": "YOUR_KEY"
      }
    }
  }
}
```

### 4. Enable Lifecycle Event Hooks

Wire the agent telemetry and post-compaction re-grounding hooks by running:

```bash
node plugins/mori/scripts/install-hooks-antigravity.mjs \
  --url "http://YOUR-SERVER:8968" \
  --api-key "YOUR_KEY" \
  --target both
```

* Use `--target cli|ide|both` to specify whether to write to the CLI profile, the IDE profile, or both.
* The installer merges the `"mori"` named hook block containing hooks for `PreInvocation`, `PostToolUse`, `Stop`, and `PostCompact` into `hooks.json`.

### 5. Reload Antigravity

Restart the Antigravity IDE or CLI session. Confirm the `mori` MCP server appears in your active MCP connections.

---

## Legacy Installer (Alternative Path)

> The plugin package is the recommended install path. The installer scripts below are the legacy approach, now superseded. They remain documented for users who prefer a script-driven setup or cannot use the plugin marketplace.

Run the setup script from the root of the Mori repository. The script will guide you step-by-step through configuring your server URL, API key, and client name, and then perform a connectivity test.

### Windows (PowerShell)

```powershell
powershell -File scripts/install-mori-antigravity.ps1
```

### Linux / macOS (Bash)

```bash
./scripts/install-mori-antigravity.sh
```

### What the Legacy Script Does

#### 1. Connects the MCP Server
Creates or updates `mcp_config.json` under your Antigravity target directory (`~/.gemini/antigravity/mcp_config.json` or `~/.gemini/antigravity-ide/mcp_config.json`).

#### 2. Enables Event Logging Hooks
Creates or updates `hooks.json` under your config directory:
* Binds agent lifecycle events (`PostToolUse`, `PostToolUseFailure`, `UserPromptSubmit`, `Stop`, `PreCompact`) to Mori's event logging shipper (`mori-ship-event.sh` or `mori-ship-event.ps1`). As of v2.1.24 the `Stop` hook also ships a bounded transcript tail, from which the server extracts the turn's assistant reasoning (plans, analysis, decisions).
* Overrides the event query with your configured client name and auth headers.
* Adds `"_mori_managed": true` to each hook entry so re-runs can find and update Mori's hooks cleanly without matching command strings.

Post-compaction re-grounding is handled by a **SessionStart hook** that checks `source: "compact"` and prompts the agent to run `/brief --post-compact`. This is the correct mechanism — PostCompact fires for observability only and cannot inject context into the session.

#### 3. Registers Custom Skills
Creates a custom Antigravity plugin under your plugins directory:
* Deploys `plugin.json` to register the bridge.
* Translates all Claude Code `.skill.md` files from the `skills/` folder into Google Antigravity's YAML frontmatter structure (`SKILL.md`) under `mori-bridge/skills/mori-<name>/SKILL.md`.

### Command Line Options (Automation)

#### PowerShell Options:
```powershell
powershell -File scripts/install-mori-antigravity.ps1 -MoriUrl "http://10.0.0.5:8968" -ApiKey "secret" -ClientName "my-client" -Target "ide" -Force -UpgradeSkills
```

#### Bash Options:
```bash
./scripts/install-mori-antigravity.sh --url "http://10.0.0.5:8968" --api-key "secret" --client "my-client" --target "ide" --force --upgrade-skills
```

Use the `-Target` / `--target` option to specify `'cli'` (`~/.gemini/antigravity`), `'ide'` (`~/.gemini/antigravity-ide`), or `'both'` (default is `'ide'` on Windows and interactive on Linux/macOS).
Use the `-Force` / `--force` switch to bypass interactive prompts if the server is offline during the setup.
Use the `-UpgradeSkills` / `--upgrade-skills` switch to force overwriting existing skills in the plugin folder (by default, the installer skips existing skills to protect manual edits).

### Doctor Mode (Diagnostics)

#### PowerShell:
```powershell
powershell -File scripts/install-mori-antigravity.ps1 -Doctor -MoriUrl "http://localhost:8968" -Target "ide"
```

#### Bash:
```bash
./scripts/install-mori-antigravity.sh --doctor --url "http://localhost:8968" --target "ide"
```

---

## Usage — Mori slash commands

Once installed (plugin or legacy), use Mori slash commands in the Antigravity chat UI:

* **`/mori-brief`**: Session bootstrap. Loads recent/canonical shared memories, checks team standards, and verifies server status.
* **`/mori-consult`**: Requests strategic guidance from the Mori Advisor model (e.g., `/mori-consult --focus architecture "Review my current approach"`).
* **`/mori-req`**: Manages project requirements and delivery tracking.
* **`/mori-dream`**: Manages and runs the dream distillation pipeline.
* **`/mori-pensieve`**: Performs a search query across the shared memory store.
* **`/mori-nats`**: Real-time cross-device messaging awareness tools.
* **`/mori-ingest`**: Feeds documents, code, transcripts, or git history into the shared memory store — reads files from this device, works with remote mori-advisor instances. Full reference: [docs/reference/slash-commands.md](../reference/slash-commands.md).

---

## Memory Store

The Mori server uses a **dual-backend** store: SQLite for solo or synchronous setups, and Postgres for team or asynchronous deployments. Shared memory lives on the server — not on this machine.

---

## Upgrading

**Plugin users**: pull the repo and re-copy `plugins/mori/` to `~/.gemini/config/plugins/mori/` (or the workspace `.agents/plugins/mori/`).

**Legacy installer users**: re-running the installer upgrades event capture hooks and skills automatically:
* It merges event logging hooks into `hooks.json` cleanly, preserving other third-party hooks.
* It deploys shipper scripts (`mori-ship-event`) to the plugins directory.
* It updates legacy hook entries (inline curl or shipper commands without the field) with the `"_mori_managed": true` flag on the first re-run.

The shipper scripts provide:
- Reliable stdin capture (no subprocess pipe issues)
- Local failure logging (`%TEMP%\mori-hook.log` on Windows, `/tmp/mori-hook.log` on Linux/macOS)
- Log rotation at 100 KB
- Always exit 0 so a Mori outage never interrupts your Antigravity session

---

## Optional: Git push notifications

Install the post-push hook in your repos to broadcast a `GitPush` event to NATS whenever you push — every other active instance sees it at the next `/brief`. See [docs/reference/git-hooks.md](../reference/git-hooks.md).
