- name: brief
- description: Session bootstrap — load shared knowledge from the Mori server via MCP

## Argument Parsing

Parse the raw input for:
- `--project <name>`: scope the brief to a specific project
- `--auto`: detect project from the current git working directory

If no arguments: run the standard unscoped brief.

## Execution

### 1. Pull latest config

`git -C ~/mori-config pull 2>/dev/null || true`

### 2. Resolve project (if --auto or --project)

**If `--auto`:**
1. Check for `.mori-project` file — walk up from CWD to filesystem root; if found, use its text content (stripped, lowercased)
2. Else check `MORI_PROJECT` environment variable
3. Else run `git rev-parse --show-toplevel 2>/dev/null` and use `basename` of the result (lowercased)
4. If no project detected: fall back to unscoped brief and note "no project detected, loading unscoped"

**If `--project <name>`:** use that name directly.

### 3. Call the MCP tool

- With project: call `mori-brief` with `project=<name>`
- Without project: call `mori-brief` (no params — loads all memories up to cap)

### 4. Report

Report "Ready" — summarise what was loaded (memory counts, project scope, dream state). Do not take autonomous actions.
