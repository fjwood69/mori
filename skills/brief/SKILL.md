- name: brief
- description: Session bootstrap — load shared knowledge from the Mori server via MCP

## Argument Parsing

Parse the raw input for:
- `--project <name>`: scope the brief to a specific project
- `--auto`: detect project from the current git working directory

If no arguments: run the standard unscoped brief.

## Execution

### 1. Resolve project (if --auto or --project)

**If `--auto`:**
1. Check for `.mori-project` file — walk up from CWD to filesystem root; if found, use its text content (stripped, lowercased)
2. Else check `MORI_PROJECT` environment variable
3. Else run `git rev-parse --show-toplevel 2>/dev/null` and use `basename` of the result (lowercased)
4. If no project detected: fall back to unscoped brief and note "no project detected, loading unscoped"

**If `--project <name>`:** use that name directly.

### 2. Call the MCP tool

- With project: call `mori-brief` with `project=<name>`
- Without project: call `mori-brief` (no params — loads all memories up to cap)

### 3. Check pending messages

Call `mori-msg_recv(unacked=True)`.

If messages are returned, surface them after the memory summary:

- **task / question** — prominent; include a ready-made reply command:
  - task → `mori-msg_send(to="<from_host>", type="ack", reply_to="<id>", body="acknowledged")`
  - question → `mori-msg_send(to="<from_host>", type="reply", reply_to="<id>", body="...")`
- **decision / broadcast** — awareness items only (lower prominence)

Skip this section silently if no pending messages or if `mori-msg_recv` fails (daemon may not be running).

### 4. Report

Report "Ready" — summarise what was loaded (memory counts, project scope, dream state, pending message count if any). Do not take autonomous actions.
