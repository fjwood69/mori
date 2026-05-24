# Getting Started — Cline

Connect your **Cline** instance to your Mori shared memory server. This allows the Cline agent to load shared memories, use Mori's strategic advisor tools, and feed session events into the dream pipeline.

Mori provides automated setup scripts that guide you through an interactive configuration wizard and deploy the necessary files.

---

## Prerequisites

- Cline installed (VS Code extension).
- Access to a running Mori server (e.g. at `http://localhost:8968` or via a Tailscale IP).
- Optional: An API key if your Mori server has `MORI_ADVISOR_API_KEY` enabled.

---

## Automated Installation (Recommended)

Run the setup script from the root of the Mori repository. The script will guide you step-by-step through configuring your server URL, API key, and client name, and then perform a connectivity test.

### Windows (PowerShell)
Open PowerShell and run:
```powershell
powershell -File scripts/install-mori-cline.ps1
```

### Linux / macOS (Bash)
Open your terminal and run:
```bash
./scripts/install-mori-cline.sh
```

---

## What the Script Does

The setup script automatically generates and deploys all required files:

### 1. Sets Environment Variables
Creates or updates a persistent profile script (`cline.env` on Linux/macOS, User environment variables on Windows) with your Mori server URL, client name, and optional API key.

### 2. Registers the MCP Plugin
Creates or updates the Cline plugin registration in the VS Code global storage directory, enabling the Mori MCP tools and slash commands within Cline.

### 3. Configures MCP Server
Adds the Mori MCP server configuration to your Cline settings, pointing at your Mori server's streamable HTTP endpoint.

### 4. Deploys Lifecycle Hooks
Configures Cline's custom hooks (`alwaysExecute`) to POST session events (`PostToolUse`, `PostToolUseFailure`, `UserPromptSubmit`, `Stop`, `PreCompact`) to Mori's event logging API.

### 5. Registers Custom Skills
Deploys all slash command `.skill.md` files from the `skills/` directory into Cline's IDE skills registry.

---

## What Gets Installed

| File | Location | Purpose |
|------|----------|---------|
| `cline.env` | `~/.config/mori/` (Linux/macOS) | Persistent env vars sourced by profile |
| User env vars | Windows registry | Persistent env vars (Windows) |
| `cline_mcp_config.json` | VS Code global storage | MCP server registration |
| Hooks config | VS Code global storage | Event lifecycle hooks |
| Skills | VS Code skills directory | Slash commands (`/brief`, `/wrap`, etc.) |
