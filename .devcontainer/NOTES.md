# Mori in GitHub Codespaces

## Setup (one time)

### 1. Edit `.env`

The file was copied from `.env.example` when the container started. Open it and set your provider details:

```
MORI_PROVIDER_MODE=direct
MORI_API_KEY=your-key-here
MORI_BASE_URL=https://api.novita.ai/v3/openai   # or any OpenAI-compatible endpoint
MORI_ADVISOR_MODEL=moonshotai/kimi-k2.6          # or any model at your provider
```

See `.env.example` for all available options.

### 2. Generate a server API key

```bash
python3 -c "import secrets; print('MORI_API_KEYS=codespace:' + secrets.token_hex(32))"
```

Add the output line to `.env`. You'll use this key to connect your agent.

### 3. Run

```bash
python -m mori_advisor.main
```

Mori starts on port 8968. A notification will appear — click it to open the dashboard, or check the **Ports** tab.

---

## Connect your agent

Point Claude Code, Cursor, or Antigravity at the forwarded Codespaces URL:

```
https://<your-codespace-name>-8968.app.github.dev/mcp?api-key=<your-key>
```

Find the full URL in the **Ports** tab (port 8968).

> ⚠️ **Port visibility:** Codespaces ports are **private by default**. External MCP
> clients (Claude Code on another machine, Cursor, etc.) will get a silent 401 until
> you set the port to **Public**:
>
> **Ports tab → right-click port 8968 → Port Visibility → Public**
>
> The Mori dashboard in your browser works without this — only cross-machine MCP
> connections need the port public.

---

## Data

Memories are stored in `./data/` inside the Codespace (`/workspaces/mori/data/`),
visible in the Explorer panel.

> ⚠️ **Codespaces are ephemeral.** Data persists while the Codespace exists but is
> lost when it is deleted. For persistent memory across sessions, deploy to Railway,
> Render, or Fly — see the one-click deploy options in the README.

To export your memories before deleting the Codespace:

```bash
python -m mori_advisor.cli.export --output ./data/export.jsonl
```

---

## Health check

```bash
curl http://localhost:8968/health
# {"status":"ok","service":"mori-advisor"}
```
