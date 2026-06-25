# Getting Started — Mori Cursor Bridge

Connect Cursor 2.4+ to your Mori shared memory server: `/brief`, `/consult`, `/dream`, event capture, and cross-device messaging.

---

## Prerequisites

- **Cursor 2.4+** — plugin MCP, `~/.cursor/hooks.json`, and `~/.claude/skills/` (or plugin `skills/`).
- **Mori server** reachable (homelab, GCE, Tailscale, etc.).
- **Third-party skills enabled:** Settings → Rules, Skills, Subagents → **Enable third-party skills**.
- Optional: API key if the server uses `MORI_ADVISOR_API_KEY`.
- **Node.js 18+** for native hook scripts (bundled in the plugin).

---

## Install as a Plugin (Recommended)

Mori ships as a unified plugin at [`plugins/mori/`](../../plugins/mori/). One installer copies the package, configures MCP, and wires hooks.

### One-command install (Linux / macOS)

From the **mori** repo root:

```bash
# Minimal: MCP + skills + core native hooks (sessionStart, postToolUse, stop)
./scripts/install-mori-cursor-plugin.sh \
  --url "http://<your-server>:8968" \
  --api-key "<bare-secret-if-required>" \
  --client "$(hostname)" \
  --force

# Parity: true up to legacy hook depth (adds prompt/failure native hooks + PreCompact/PostCompact compat)
./scripts/install-mori-cursor-plugin.sh \
  --url "http://<your-server>:8968" \
  --api-key "<bare-secret-if-required>" \
  --parity \
  --force
```

Use `--upgrade` to refresh the plugin copy from the repo. Use `--doctor` (add `--parity` if you installed with parity) to print a capability matrix.

### Manual install

```bash
cp -r plugins/mori ~/.cursor/plugins/local/mori
# Edit ~/.cursor/plugins/local/mori/mcp.json (URL + x-api-key)
node ~/.cursor/plugins/local/mori/scripts/install-hooks-cursor.mjs \
  --url http://<server>:8968 --api-key <key>
# Optional parity compat layer:
./scripts/install-mori-cursor-plugin.sh --url http://<server>:8968 --parity --force
# (re-runs hook steps; use after manual copy)
```

Symlink for local dev: `ln -s "$(pwd)/plugins/mori" ~/.cursor/plugins/local/mori`

### Reload Cursor

Command Palette → *Developer: Reload Window*. Confirm **mori** under Settings → MCP.

---

## Minimal vs parity

| Capability | Minimal (default) | `--parity` |
|------------|-------------------|------------|
| MCP + 9 skills | Plugin `mcp.json` + `skills/` | Same |
| Session context inject | `sessionStart` native hook | Same |
| Tool telemetry | `postToolUse`, `stop` | Same |
| Prompt telemetry | — | `beforeSubmitPrompt` native |
| Tool failure telemetry | — | `postToolUseFailure` native (best-effort) |
| Pre-compact dream | — | `PreCompact` via `~/.claude/settings.json` |
| Post-compact re-ground | — | `PostCompact` → brief script in `settings.json` |
| MCP `permissions.allow` | — | Merged in `settings.json` |

**Two layers in parity mode** (no duplicate telemetry):

1. **Native** — `~/.cursor/hooks.json` (Cursor hook events, Node scripts in plugin)
2. **Compat** — `~/.claude/settings.json` for `PreCompact` / `PostCompact` only; overlaps with native are pruned

Claude Code’s `SessionStart source=compact` nudge has **no Cursor equivalent** — parity uses the `PostCompact` compat hook (same approach as the legacy installer).

---

## Event capture (native hooks)

| Cursor event | Mori script | What gets shipped |
|--------------|-------------|-------------------|
| `sessionStart` | `mori-context-hook-cursor.mjs` | Injects `MORI_SESSION_CONTEXT_FILE` (if set) via `additional_context` |
| `postToolUse` | `mori-ship-event-cursor.mjs` | `tool_name`, `tool_input`, and Cursor’s **`tool_output`** (JSON-stringified result) → `/api/events/raw` |
| `stop` | `mori-ship-event-cursor.mjs` | Stop event + optional transcript tail (`transcript_tail_b64`) |
| `beforeSubmitPrompt` | *(parity only)* | Prompt text |
| `postToolUseFailure` | *(parity only)* | Failure metadata (best-effort) |

The shipper normalises Cursor’s snake_case payload (including `tool_output`) before POST. The mori server stores tool results on the event row as `tool_response`. **`preToolUse`** is supported by Cursor but **not wired** by Mori’s installer (no gate/rewrite hook by default).

Field names follow [Cursor hooks](https://cursor.com/docs/hooks) (`Shell`, `Read`, `Write`, …; `tool_output` on `postToolUse`).

---

## Already set up via Claude Code?

If Claude Code deployed skills under `~/.claude/skills/`, Cursor can use them. The plugin installer does not require wiping that directory.

```bash
ls ~/.claude/skills/
ls ~/.cursor/plugins/local/mori/skills/
```

Shared memory lives on the **Mori server** — not on this laptop.

---

## Legacy installer (fallback)

Full `~/.claude/settings.json` hook stack in one step (no plugin directory):

```bash
./scripts/install-mori-cursor.sh --url "http://<server>:8968" \
  --api-key "<key>" --client "$(hostname)" --force
```

Windows: `powershell -File scripts/install-mori-cursor.ps1`

Use when plugin install is not possible or you want the older layout only.

---

## Mori slash commands (skills)

| Command | Description |
|---------|-------------|
| `/brief` | Session bootstrap (full or `--post-compact` delta) |
| `/dream` | Distil session events into memories |
| `/consult` | Strategic review |
| `/pensieve` | Search memory store |
| `/ingest` | Ingest local files into remote store |
| `/req` | Requirements tracking |
| `/nats` | Cross-device NATS messages |
| `/msg` | Inter-agent inbox |
| `/wrap` | End-of-session publish + dream flush |

All require MCP connected. See [slash-commands.md](../reference/slash-commands.md).

---

## Verify

```bash
./scripts/install-mori-cursor-plugin.sh --doctor --url "http://<server>:8968"
./scripts/install-mori-cursor-plugin.sh --doctor --parity --url "http://<server>:8968"
```

1. Reload Cursor window
2. MCP → `mori` connected
3. `/brief` — server counts via MCP
4. Use Agent; event count at `curl http://<server>:8968/api/events/health`
5. After a tool call, confirm a `PostToolUse` row landed with non-empty tool output (server `session_events.tool_response`, or `/api/events/raw` ingest logs)

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| MCP not connected | Re-run plugin installer; check plugin `mcp.json`; reload |
| Skills missing | `--upgrade` plugin install; enable third-party skills |
| No events shipping | Run hook installer step; check `/tmp/mori-hook.log` |
| Events ship but no tool output | Upgrade plugin (`--upgrade`); hook paths are absolute to the plugin dir — no need to re-run `install-hooks-cursor.mjs` unless you changed server URL/key |
| No post-compact re-ground | Install with `--parity` or legacy installer |
| Duplicate events | Don't run legacy + plugin native hooks without `--parity` prune; use `tidy-up.mjs --client cursor` |

Migration from bespoke install: `node plugins/mori/scripts/legacy/tidy-up.mjs --client cursor --dry-run`

---

## Known limitations

- **PostToolUseFailure** — wired in parity mode; not fully verified on all Cursor versions
- **preToolUse** — not wired by Mori (Cursor supports it; Mori minimal/parity install does not)
- **Post-compact** — no `SessionStart source=compact`; use `PostCompact` compat hook or manual `/brief --post-compact`
- **Third-party skills** — Cursor updates can disable the toggle

---

## Upgrading

```bash
git pull
./scripts/sync-plugin-skills.sh   # maintainers: refresh plugin skills snapshot
./scripts/install-mori-cursor-plugin.sh --url ... --upgrade --parity --force
```

`--upgrade` refreshes `~/.cursor/plugins/local/mori/`; hook entries in `~/.cursor/hooks.json` point at absolute paths under that directory, so translator fixes (e.g. `postToolUse` `tool_output` mapping) apply after upgrade + Cursor reload — re-run the hook installer only if URL/API key changed.

Optional: [git-hooks.md](../reference/git-hooks.md) for per-repo `git push` ingest.
