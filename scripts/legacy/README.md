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

If you installed Mori with `install-mori-claude.sh` or `install-mori-claude.ps1`, run the legacy uninstaller first to remove the old `settings.json` MCP server entries and hooks before enabling the plugin:

**Linux / macOS:**
```bash
bash plugins/mori/scripts/legacy/uninstall-mori-claude.sh
```

**Windows (PowerShell):**
```powershell
.\plugins\mori\scripts\legacy\uninstall-mori-claude.ps1
```

Then install the plugin as described above.
