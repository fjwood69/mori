# scripts/legacy — Superseded Bespoke Installers

The files in this directory are the original bespoke installers for the Claude Code bridge.
They have been superseded by the unified plugin package at `plugins/mori/`.

## Recommended path

Install the Mori plugin directly inside Claude Code:

```
/plugin marketplace add fjwood69/mori
/plugin install mori@mori
```

The plugin provides the MCP connection, skills, `SessionStart` re-ground hook, and telemetry in
one step — no scripts to clone or run, and no manual `settings.json` edits.

## Files in this directory

| File | Status |
|------|--------|
| `install-mori-claude.sh` | Superseded by `plugins/mori/`. Retained for air-gapped or script-driven setups. |
| `install-mori-claude.ps1` | Superseded by `plugins/mori/`. Retained for air-gapped or script-driven setups. |

The Cursor and Antigravity bespoke installers (`scripts/install-mori-cursor.sh`, `scripts/install-mori-antigravity.sh`, and their `.ps1` counterparts) remain in `scripts/` because their platform-specific plugin hook layers are a fast-follow; they are not fully superseded yet.

The Cline installer (`scripts/install-mori-cline.sh` / `.ps1`) is unchanged — there is no Cline plugin yet.

## Migrating from the bespoke installer to the plugin

If you installed Mori with any of the bespoke installers (Claude Code, Cursor, or Antigravity),
run the tidy-up tool to remove the old `settings.json` / `mcp.json` / `hooks.json` entries
before enabling the plugin:

**Preview what would be removed (dry-run — writes nothing):**
```bash
node plugins/mori/scripts/legacy/tidy-up.mjs
```

**Apply changes for all clients:**
```bash
node plugins/mori/scripts/legacy/tidy-up.mjs --confirm
```

**Apply for a specific client only:**
```bash
node plugins/mori/scripts/legacy/tidy-up.mjs --confirm --client claude
node plugins/mori/scripts/legacy/tidy-up.mjs --confirm --client cursor
node plugins/mori/scripts/legacy/tidy-up.mjs --confirm --client antigravity
```

**Also remove bespoke skill directories (optional — backs up first):**
```bash
node plugins/mori/scripts/legacy/tidy-up.mjs --confirm --include-skills
```

Timestamped backups are created automatically before any write. Then install the plugin as described above.
