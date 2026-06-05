# Mori Plugin — unified multi-client package

Connects **Claude Code**, **Cursor**, and **Antigravity** to a running
[mori-advisor](../../mori_advisor) server. The plugin ships:

- **Skills** — shared agent skills (`/brief`, `/dream`, `/pensieve`, `/wrap`, …) for all three clients.
- **Hooks** — SessionStart context re-grounding + optional telemetry shipping.

The **MCP connection is NOT bundled for Claude Code** — you add it yourself with
`claude mcp add` (see [Install](#claude-code)), so it can never collide with the plugin.
Cursor and Antigravity still use a bundled `mcp.json` / `mcp_config.json` you edit by hand.

> **Server not included.** This package is the *client side* only. Deploy the
> mori-advisor server separately; see the [root README](../../README.md) for
> instructions.

---

## Directory layout

```
plugins/mori/
├── .claude-plugin/plugin.json   Claude Code manifest (skills + hooks; no bundled MCP)
├── .cursor-plugin/plugin.json   Cursor manifest
├── plugin.json                  Antigravity manifest
├── mcp.json                     Cursor MCP config (edit url + bare-secret key manually)
├── mcp_config.json              Antigravity MCP config (edit url/key manually)
├── hooks/hooks.json             Claude Code hooks (SessionStart + telemetry)
├── scripts/
│   ├── mori-context-hook.mjs              Claude Code SessionStart hook (Node ESM)
│   ├── mori-ship-event.mjs                Claude Code telemetry shipper (Node ESM)
│   ├── mori-context-hook-cursor.mjs       Cursor sessionStart context hook
│   ├── mori-ship-event-cursor.mjs         Cursor postToolUse/stop telemetry shipper
│   ├── mori-context-hook-antigravity.mjs  Antigravity PreInvocation context hook
│   ├── mori-ship-event-antigravity.mjs    Antigravity PostToolUse/Stop telemetry shipper
│   ├── install-hooks-cursor.mjs           Installer: writes ~/.cursor/hooks.json
│   ├── install-hooks-antigravity.mjs      Installer: writes ~/.gemini/config/hooks.json
│   ├── lib/
│   │   ├── canonical.mjs    Canonical event schema normalizer (client → mori schema)
│   │   ├── fail-open.mjs    Fail-open wrapper: any throw → exit 0
│   │   ├── post.mjs         Fail-soft HTTP POST helper
│   │   └── throttle.mjs     Once-per-conversation temp-file flag
│   └── legacy/
│       └── tidy-up.mjs                Migration: remove bespoke entries (all clients, cross-platform)
├── skills/                      Shared skills (copied from repo root skills/)
└── tests/
    ├── test_plugin_hooks.mjs          Hermetic tests for Claude Code hook scripts
    ├── test_cursor_hooks.mjs          Hermetic tests for Cursor hook scripts
    ├── test_antigravity_hooks.mjs     Hermetic tests for Antigravity hook scripts
    └── fixtures/
        ├── cursor-sessionStart.json   Cursor sessionStart input fixture
        ├── cursor-postToolUse.json    Cursor postToolUse input fixture
        ├── cursor-stop.json           Cursor stop input fixture
        ├── cursor-preToolUse.json     Cursor preToolUse input fixture
        ├── antigravity-PreInvocation.json  Antigravity PreInvocation input fixture
        ├── antigravity-PostToolUse.json    Antigravity PostToolUse input fixture
        └── antigravity-Stop.json           Antigravity Stop input fixture
```

---

## Install

### Claude Code

The plugin ships **skills + hooks only**. The mori **connection** is a standard Claude
Code MCP server you add separately — so there's nothing to configure inside the plugin
and no second server to collide with.

**1. Add your mori server.** The `x-api-key` is the **bare secret** — *not* `name:secret`
(the `name:` belongs only in the server's `MORI_API_KEYS`; the client sends the secret
alone, or it 401s):

```bash
claude mcp add --transport http mori http://YOUR-SERVER:8968/mcp \
  --header "x-api-key: YOUR-64-CHAR-SECRET" --scope user
```

**2. Install the plugin** for the skills (`/brief`, `/dream`, `/pensieve`, …):

```bash
/plugin marketplace add fjwood69/mori
/plugin install mori@mori
# Or point Claude at a local checkout:
claude --plugin-dir plugins/mori
```

**3. Reload the window.** `claude mcp list` should show `mori ✓ Connected`; the skills run
against it.

> **Optional telemetry hooks.** The SessionStart/event hooks ship session events to your
> server. They read `MORI_SERVER_URL` and `MORI_API_KEY` (the **bare secret**) from the
> environment — set those (shell profile, or `claudeCode.environmentVariables` in VS Code)
> for auto-capture. The skills work without them.

**Alternative — project-scoped, no marketplace.** Instead of `claude mcp add`, copy
`plugins/mori/` into your project's `.claude/plugins/mori/` and add the server to the
project's own `.mcp.json` (same bare-secret rule):

```json
{
  "mcpServers": {
    "mori": {
      "type": "http",
      "url": "http://YOUR-SERVER:8968/mcp",
      "headers": { "x-api-key": "YOUR-64-CHAR-BARE-SECRET" }
    }
  }
}
```

---

### Cursor

Copy `plugins/mori/` to `~/.cursor/plugins/local/mori/` (or the workspace
`.cursor/plugins/mori/`).

Edit `mcp.json` with your server URL and API key:

```json
{
  "mcpServers": {
    "mori": {
      "type": "http",
      "url": "http://YOUR-SERVER:8968/mcp",
      "headers": { "x-api-key": "YOUR-64-CHAR-BARE-SECRET" }
    }
  }
}
```

Cursor reads `.cursor-plugin/plugin.json` as the manifest and `mcp.json` for the MCP
connection. Skills in `skills/` are auto-discovered.

> **The `x-api-key` is the bare secret** — the 64-char hex string alone, **not**
> `name:secret`. The `name:` only labels the key in the server's `MORI_API_KEYS`; the
> client sends the secret by itself, or the server returns `401`.

---

### Antigravity

Copy `plugins/mori/` to `~/.gemini/config/plugins/mori/` (global) or
`.agents/plugins/mori/` (workspace).

Edit `mcp_config.json` with your server URL and API key:

```json
{
  "mcpServers": {
    "mori": {
      "type": "http",
      "url": "http://YOUR-SERVER:8968/mcp",
      "headers": { "x-api-key": "YOUR-64-CHAR-BARE-SECRET" }
    }
  }
}
```

Antigravity reads `plugin.json` (root) as the manifest and `mcp_config.json` for the
MCP connection. Skills in `skills/` are auto-discovered.

---

## Skills

The `skills/` directory is copied from the repo root `skills/` at release time. All
three clients read the same files; no per-client skill copies are maintained.

**Known maintenance point**: `plugins/mori/skills/` is a snapshot — it will drift from
`skills/` between releases. A sync script (`scripts/sync-skills.sh`) is a planned
addition (tracked in ROADMAP). For now, re-copy manually after updating skills:

```bash
rm -rf plugins/mori/skills && cp -r skills plugins/mori/skills
```

Symlinks are intentionally avoided: plugin installers strip external symlinks when
copying the bundle to a user's local plugins directory.

---

## Hooks — SessionStart re-grounding and telemetry

### Claude Code

Hooks are wired into the plugin via `hooks/hooks.json` using the `${CLAUDE_PLUGIN_ROOT}`
variable. No manual configuration needed — they activate when the plugin is enabled.

#### SessionStart re-grounding (`mori-context-hook.mjs`)

Fires once per session boundary (startup, resume, clear, or post-compaction). The script:

- **source = `compact`**: emits an `additionalContext` nudge asking the agent to run
  `/brief --post-compact` before continuing. Disable with `MORI_POST_COMPACT_BRIEF=false`.
- **source = `startup` / `resume` / `clear`**: if `MORI_SESSION_CONTEXT_FILE` is set to
  a readable file path, injects its contents as `additionalContext`. Unset by default —
  nothing is injected on a stock install.

#### Telemetry shipping (`mori-ship-event.mjs`)

Ships hook event payloads to the mori server on every `PostToolUse`,
`PostToolUseFailure`, `UserPromptSubmit`, `Stop`, and `PreCompact` event. Enriches
`Stop` events with a bounded base64 tail of the session transcript (last 64 KB) so the
server's dream pipeline can extract assistant reasoning from the turn.

`PreCompact` uses `--mode precompact` which **blocks** (awaits the response) so the
server has time to persist state before the compaction snapshot is taken.

All network failures are logged to `$TMPDIR/mori-hook.log` and the script exits 0 —
hooks never interrupt the agent.

**Requirements**: Node.js 18+ (for global `fetch`). No npm packages; built-in ESM only.

---

### Cursor hooks

Cursor plugin hook bundling (plugin-root-relative paths) is undocumented. Mori uses the
standalone `~/.cursor/hooks.json` with absolute paths, written by an install script.

After installing the plugin (MCP + skills), wire the telemetry and context hooks by
running the installer once:

```bash
node plugins/mori/scripts/install-hooks-cursor.mjs \
  --url http://YOUR-SERVER:8968 \
  --api-key YOUR_BARE_SECRET
```

Or via environment variables:

```bash
MORI_SERVER_URL=http://YOUR-SERVER:8968 MORI_API_KEY=YOUR_BARE_SECRET \
  node plugins/mori/scripts/install-hooks-cursor.mjs
```

Use `--dry-run` to preview the JSON that would be written without touching
`~/.cursor/hooks.json`.

The installer merges three hook entries into `~/.cursor/hooks.json`:

| Cursor event | Script wired |
|---|---|
| `sessionStart` | `mori-context-hook-cursor.mjs` — injects `MORI_SESSION_CONTEXT_FILE` if set |
| `postToolUse` | `mori-ship-event-cursor.mjs` — ships normalised event to mori server |
| `stop` | `mori-ship-event-cursor.mjs` — ships Stop event (enriched with transcript tail) |

Existing non-mori hook entries are preserved. Reload Cursor after running the installer.

> **No post-compaction inject point in Cursor.** Cursor has no equivalent to
> Claude Code's `SessionStart source=compact`. Context re-grounding is only injected
> at session start.

---

### Antigravity hooks

After installing the plugin (MCP + skills), wire the hooks by running:

```bash
node plugins/mori/scripts/install-hooks-antigravity.mjs \
  --url http://YOUR-SERVER:8968 \
  --api-key YOUR_BARE_SECRET
```

Or via environment variables:

```bash
MORI_SERVER_URL=http://YOUR-SERVER:8968 MORI_API_KEY=YOUR_BARE_SECRET \
  node plugins/mori/scripts/install-hooks-antigravity.mjs
```

Use `--dry-run` to preview the JSON without writing `~/.gemini/config/hooks.json`.

The installer writes a `"mori"` named-hook block into `~/.gemini/config/hooks.json`:

| Antigravity event | Script wired |
|---|---|
| `PreInvocation` | `mori-context-hook-antigravity.mjs` — injects `MORI_SESSION_CONTEXT_FILE` once per conversation (throttled by conversationId) |
| `PostToolUse` | `mori-ship-event-antigravity.mjs` — ships normalised event to mori server |
| `Stop` | `mori-ship-event-antigravity.mjs` — ships Stop event (enriched with transcript tail) |

Existing non-mori named hooks are preserved. Restart Antigravity after running the installer.

> **PreInvocation fires before every model call.** The context hook uses a per-conversation
> temp-file throttle (`$TMPDIR/mori-conv-<id>`) so context is injected exactly once per
> conversation, not on every invocation.

> **No session-start or post-compaction event in Antigravity.** The `PreInvocation`
> once-per-conversation injection is the closest equivalent. Post-compact re-grounding
> is Claude Code only.

---

### Shared environment variables (all clients)

| Variable | Default | Purpose |
|----------|---------|---------|
| `MORI_POST_COMPACT_BRIEF` | _(unset)_ | Claude Code only. Set to `false` to suppress the post-compact /brief nudge |
| `MORI_SESSION_CONTEXT_FILE` | _(unset)_ | Path to a file whose contents are injected at session start (all clients) |
| `MORI_CLIENT_ID` | `os.hostname()` | Override the `?client=` query param sent to the mori server (Cursor + Antigravity) |

---

## Migration / Cleanup

If you previously installed mori using any of the bespoke installer scripts
(`install-mori-claude.sh`, `install-mori-cursor.sh`, `install-mori-antigravity.sh`,
or their `.ps1` counterparts), run the tidy-up tool before enabling the plugin
to remove duplicate MCP server entries, hooks, and permissions.

**Preview what would be removed (dry-run, writes nothing):**
```bash
node plugins/mori/scripts/legacy/tidy-up.mjs
```

**Apply changes for all clients:**
```bash
node plugins/mori/scripts/legacy/tidy-up.mjs --confirm
```

**Limit to one client:**
```bash
node plugins/mori/scripts/legacy/tidy-up.mjs --confirm --client claude
node plugins/mori/scripts/legacy/tidy-up.mjs --confirm --client cursor
node plugins/mori/scripts/legacy/tidy-up.mjs --confirm --client antigravity
```

**Also remove bespoke mori skill directories (optional — backs up first):**
```bash
node plugins/mori/scripts/legacy/tidy-up.mjs --confirm --include-skills
```

The tool:
- Defaults to **dry-run** — nothing is written without `--confirm`.
- Creates a **timestamped backup** (`<file>.mori-backup-<ISO>`) before every write.
- Is **fail-gradual** — a missing or malformed file for one client does not stop the others.
- Removes only exact mori signatures (MCP key, hook commands, `mcp__mori__*` permissions).
  No other config is touched.

---

## Running the tests

```bash
# Claude Code hook scripts
node plugins/mori/tests/test_plugin_hooks.mjs

# Cursor hook scripts
node plugins/mori/tests/test_cursor_hooks.mjs

# Antigravity hook scripts
node plugins/mori/tests/test_antigravity_hooks.mjs
```

Tests are hermetic — no running mori server is needed. Network calls are directed at a
dummy port; the test verifies that a connection failure still results in exit code 0.

Node.js 18+ is required for all hook scripts (uses global `fetch`).

---

## License

AGPL-3.0-only — same as the mori-advisor server. See [LICENSE](../../LICENSE).
