**VS Code Event Shipper — Proposal**

Build a single VS Code extension (`moku-vscode-shipper`) that watches the conversation artefacts Cline and Continue already write to disk, diffs them, and POSTs structured deltas to your raw-events endpoint. No upstream changes needed from either tool.

**Architecture**

| Component | Responsibility |
|-----------|--------------|
| **WatcherService** | Platform-specific `chokidar` or VS Code `createFileSystemWatcher` on known conversation directories |
| **DiffEngine** | In-memory SHA of each watched file; on change, parses JSON and isolates new array elements |
| **EventTranslator** | Maps foreign message schemas to your three moku lifecycle events |
| **Spooler** | Append-only JSONL queue on disk for offline resilience |
| **ShipperClient** | Drains spooler via HTTPS POST with retry backoff |

**Event Sources**

Both extensions serialise conversation state to JSON. We tail those files.

| Client | Typical Path (Linux/macOS) | File Pattern |
|--------|---------------------------|--------------|
| Cline | `~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/tasks/<task_id>/` | `api_conversation_history.json` |
| Continue | `~/.continue/sessions/` or workspace `.continue/` | `<uuid>.json` |

On Windows the Cline path shifts under `%APPDATA%/Code/User/globalStorage/…`; the extension resolves this via `context.globalStorageUri` and `os.homedir()`.

**Event Mapping**

Cline and Continue do not emit Claude Code-style hooks, but their JSON structures contain the same data.

| Moku Hook | Detected Delta | Payload Extracted |
|-----------|---------------|-------------------|
| `UserPromptSubmit` | New object with `role: 'user'` | `prompt` text, timestamp, task/session ID |
| `PostToolUse` | New object with `type: 'tool_result'` or `name: 'execute'` | Tool name, input args, output text/JSON, model identifier |
| `Stop` | File write with `status: 'completed'` or new session file created | Stop reason (`completed`, `error`), full transcript hash |

The translator coerces these into the same schema your `/api/events/raw` endpoint already accepts, so the moku-advisor ingests them indistinguishably from native Claude Code hooks.

**Spooler & Retry**

Do not fire-and-forget across the Atlantic. A local disk queue prevents event loss when Tailscale rebinds or the Toronto GCE instance restarts.

- **Queue**: `~/.moku-shipper/queue/<iso-timestamp>-<<pid>.jsonl`
- **Flush**: Background `setInterval` every 5 s drains the directory oldest-first
- **Backoff**: 1 s, 2 s, 4 s, 8 s, cap at 60 s. After 10 failures, move the line to `../dead-letter/` for inspection
- **Security**: `chmod 600` on queue files; they contain prompt text

This mirrors the reliability your moku-advisor server enjoys, but client-side.

**Configuration**

VS Code `settings.json` integration:

```json
{
  "mokuShipper.enabled": true,
  "mokuShipper.apiUrl": "https://[internal system]/api/events/raw",
  "mokuShipper.apiKey": "${env:MOKU_ADVISOR_API_KEY}",
  "mokuShipper.clientName": "uk-smr-twiggy-win11",
  "mokuShipper.watchPaths": [
    "~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/tasks/**/api_conversation_history.json",
    "~/.continue/sessions/*.json"
  ]
}
```

Hostname auto-detects via `os.hostname()` unless overridden.

**Packaging & Distribution**

Build as a `.vsix` for side-loading; you are not publishing to the marketplace. Update `sr-model.ps1` and `switch-claude-profile.sh` to run:

```powershell
code --install-extension \\[internal system]\share\moku-shipper-0.1.0.vsix --force
```

This binds the shipper version to your dotfiles pipeline. When you switch profiles or devices, the correct binary is always present.

**Alternative: Proxy Sidecar**

If Cline changes its file layout and breaks the watcher, the fallback is a local HTTP proxy (Python FastAPI or Go) on `127.0.0.1:8970` that masquerades as the Anthropic/OpenAI endpoint. Cline/Continue point their base URL to the proxy; the proxy logs request/response pairs, forwards to the real upstream, and ships telemetry to moku. This captures *everything* but requires reconfiguring each client’s provider settings and managing TLS termination. Deploy only if the file watcher proves brittle.

**Risks**

| Risk | Mitigation |
|------|------------|
| Schema drift when Cline/Continue update | Pin supported versions in the extension manifest; validate JSON shape with `zod` and silently skip unrecognised structures rather than crash |
| Multiple VS Code instances colliding on the spool | Include `process.pid` in queue filenames; the flusher ignores files younger than 1 s |
| Sensitive prompt exposure in queue | `chmod 600`, store under user home, clear dead-letter after 7 days |
| Excessive file-system I/O | Debounce watcher at 500 ms; batch consecutive deltas into a single POST |

**Bottom Line**

A file-system watcher inside a VS Code extension is the pragmatic path. It respects the sandbox boundaries of the host editor, needs no privileged network intercepts, and runs on every platform you use. The disk-backed spooler is non-negotiable while you are firing events from Ealing to Toronto. Build the `.vsix`, add the install line to your existing switcher scripts, and Cline/Continue memories start distilling into the same dream pipeline as Claude Code within the hour.
