# Mori Event Shipper

A VS Code extension that watches Cline and Continue conversation files and ships events to Mori's dream pipeline.

## Setup

1. Install the extension:
   ```powershell
   code --install-extension mori-shipper-0.1.1.vsix
   ```

2. Open VS Code settings (`Ctrl+,`), search `moriShipper`, and configure:

   | Setting | Value | Notes |
   |---------|-------|-------|
   | `moriShipper.apiUrl` | `http://<mori-host>:8968/api/events/raw` | Your Mori server endpoint |
   | `moriShipper.apiKey` | `<your-api-key>` | Value of `MORI_ADVISOR_API_KEY` on your server |
   | `moriShipper.clientName` | your device name | e.g. `my-laptop`, `workstation` |

3. Restart VS Code. The extension activates on startup and runs in the background.

## What it does

- Watches Cline's `api_conversation_history.json` and Continue's `sessions/` directory
- On new messages: POSTs structured events to Mori's `/api/events/raw`
- Events flow into Mori's dream pipeline for cross-tool memory distillation
- Offline resilience: if the server is unreachable, events spool to `~/.mori-shipper/queue/` and retry automatically (exponential backoff, max 10 retries, then dead-letter)

## Available settings

| Setting | Default | Description |
|---------|---------|-------------|
| `moriShipper.apiUrl` | `http://localhost:8968/api/events/raw` | Mori server endpoint |
| `moriShipper.apiKey` | `""` | X-Api-Key header value |
| `moriShipper.clientName` | OS hostname | Sent as `?client=` query param |
| `moriShipper.enableCline` | `true` | Watch Cline conversations |
| `moriShipper.enableContinue` | `true` | Watch Continue sessions |
| `moriShipper.enableOpenCode` | `false` | Watch OpenCode sessions |
| `moriShipper.openCodePath` | `""` | Custom path for OpenCode sessions |

## Building from source

```powershell
cd extensions/mori-shipper
npm install
npm run compile
vsce package
```
