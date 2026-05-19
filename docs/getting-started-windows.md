# Getting Started — Windows 11

Connect your Windows Claude Code instance to a shared Moku server. No server to
run — just point at your friend's Moku and you're done.

---

## Prerequisites

- Claude Code installed on Windows 11 (VS Code extension or CLI)
- Tailscale installed and connected to the same tailnet as the Moku server
- The Moku server's Tailscale IP address (ask your friend)

---

## 1. Add the MCP server

Create (or edit) `%USERPROFILE%\.claude\settings.json`. If the file doesn't
exist, create it:

```json
{
  "mcpServers": {
    "moku": {
      "type": "http",
      "url": "http://<moku-tailscale-ip>:8968/mcp"
    }
  }
}
```

Replace `<moku-tailscale-ip>` with the actual Tailscale IP.

**VS Code users:** Reload the window (`Ctrl+Shift+P` → Developer: Reload Window)
after saving.

**CLI users:** Close and restart Claude Code, or run `claude` in a new terminal.

---

## 2. Add the lifecycle hooks

The same `settings.json` file — add a `hooks` section so your session events
feed into the dream pipeline:

```json
{
  "mcpServers": {
    "moku": {
      "type": "http",
      "url": "http://<moku-tailscale-ip>:8968/mcp"
    }
  },
  "hooks": {
    "PostToolUse": [{
      "matcher": ".*",
      "hooks": [{
        "type": "command",
        "command": "curl -sf -X POST 'http://<moku-tailscale-ip>:8968/api/events/raw?client=my-windows-pc' -H 'Content-Type: application/json' -d @- >nul 2>&1 & exit 0"
      }]
    }],
    "PostToolUseFailure": [{
      "hooks": [{
        "type": "command",
        "command": "curl -sf -X POST 'http://<moku-tailscale-ip>:8968/api/events/raw?client=my-windows-pc' -H 'Content-Type: application/json' -d @- >nul 2>&1 & exit 0"
      }]
    }],
    "PreCompact": [{
      "hooks": [{
        "type": "command",
        "command": "curl -sf -X POST 'http://<moku-tailscale-ip>:8968/api/precompact?client=my-windows-pc' -H 'Content-Type: application/json' -d @- >nul 2>&1 & exit 0"
      }]
    }],
    "UserPromptSubmit": [{
      "hooks": [{
        "type": "command",
        "command": "curl -sf -X POST 'http://<moku-tailscale-ip>:8968/api/events/raw?client=my-windows-pc' -H 'Content-Type: application/json' -d @- >nul 2>&1 & exit 0"
      }]
    }],
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "curl -sf -X POST 'http://<moku-tailscale-ip>:8968/api/events/raw?client=my-windows-pc' -H 'Content-Type: application/json' -d @- >nul 2>&1 & exit 0"
      }]
    }]
  }
}
```

Replace `<moku-tailscale-ip>` and `my-windows-pc` with your actual values.

> **Note:** Windows 11 ships with `curl` built in. The `>nul 2>&1 & exit 0`
> silences output and prevents the hook from blocking Claude Code.

---

## 3. Install the slash commands

Download the skill files from the [moku repo skills folder](https://github.com/fjwood69/moku/tree/main/skills) and place them in:

```
%USERPROFILE%\.claude\skills\
```

The skills directory should contain these files:

- `brief.skill.md`
- `consult.skill.md`
- `dream.skill.md`
- `pensieve.skill.md`
- `req.skill.md`

**Quick PowerShell one-liner** (replace `MOKU_TAILSCALE_IP`):

```powershell
$ip = "MOKU_TAILSCALE_IP"
$dir = "$env:USERPROFILE\.claude\skills"
mkdir $dir -Force
@("brief","consult","dream","pensieve","req") | ForEach-Object {
    Invoke-WebRequest -Uri "https://raw.githubusercontent.com/fjwood69/moku/main/skills/$_.skill.md" -OutFile "$dir\$_.skill.md"
}
```

---

## 4. Verify it works

Start a Claude Code session and run:

```
/brief
```

You should see something like:

```
Ready — 45 memories, 5 standards loaded.
```

If you get a connection error:

1. Check Tailscale is running: `tailscale status` in PowerShell
2. Verify you can reach the server: `curl http://<moku-tailscale-ip>:8968/health`
3. Reload VS Code / restart Claude Code CLI after changing `settings.json`

---

## That's it

You're now connected to a shared Moku. Every session event feeds the dream
pipeline. Run `/dream` periodically to distil your sessions into the shared
memory store. Run `/pensieve` to search what others have learned.

Your friend (the server owner) handles updates, backups, and scaling.
You just use it.
