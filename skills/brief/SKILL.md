---
name: brief
description: Session bootstrap — loads shared memories and team standards from the Mori server. Use at session start, or after a context compaction with --post-compact.
---

**If the Mori server is unreachable** (a tool call returns a connection error, ECONNREFUSED, or timeout), do not show a stack trace. Tell the user the Mori server appears to be down and that a running Mori server is required. Point them to the quickstart at https://github.com/fjwood69/mori#quickstart and suggest checking the server URL in plugin settings. Then stop.

## Argument Parsing

Parse the raw input for:
- `--project <name>`: scope the brief to a specific project
- `--auto`: detect project from the current git working directory
- `--post-compact`: lightweight **delta** re-grounding after a context compaction —
  surfaces only what changed in shared state since the last brief, not the full base.
  Fired automatically by the Mori `SessionStart` hook (`source=compact`); can also be run manually.

If no arguments: run the standard unscoped brief.

## Marker file

The brief boundary is tracked in a per-config-dir marker:

```
${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.mori-last-brief
```

It holds the UTC ISO-8601 timestamp of the most recent brief. Stamp it (overwrite
with the current UTC time) at the **end** of every brief run, in all modes:

```bash
date -u +%Y-%m-%dT%H:%M:%SZ > "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.mori-last-brief"
```

## Execution

### 1. Resolve project (if --auto or --project)

**If `--auto`:**
1. Check for `.mori-project` file — walk up from CWD to filesystem root; if found, use its text content (stripped, lowercased)
2. Else check `MORI_PROJECT` environment variable
3. Else run `git rev-parse --show-toplevel 2>/dev/null` and use `basename` of the result (lowercased)
4. If no project detected: fall back to unscoped brief and note "no project detected, loading unscoped"

**If `--project <name>`:** use that name directly.

### 2. Call the MCP tool

**Standard brief (default):**
- With project: call `mori-brief` with `project=<name>`
- Without project: call `mori-brief` (no params — loads all memories up to cap)

**Post-compact brief (`--post-compact`):** resolve the `since` boundary first
(session-aware), then call the delta tool.

1. **Read the marker** `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.mori-last-brief`. If it
   exists and is non-empty, use its contents as `since`.
2. **Else derive session start** (best-effort): the start time of the current session
   transcript `.jsonl` (e.g. the file's first-event timestamp, or its creation time).
3. **Else omit `since`** and let the server default window (`MORI_POST_COMPACT_WINDOW`,
   default `6h`) apply.

Then call `mori-brief` with `post_compact=true`, plus `project=<name>` if resolved and
`since=<resolved>` if found:

```
mori-brief(post_compact=true, project="<name>", since="<iso-or-omitted>")
```

The delta lists are capped; if the tool reports "…N more — run a full /brief if you
need the rest", relay that pointer rather than auto-running a full brief.

### 3. Check pending messages

Call `mori-msg_recv(unacked=True)` (both modes — new messages are part of the delta).

If messages are returned, surface them after the memory summary:

- **task / question** — prominent; include a ready-made reply command:
  - task → `mori-msg_send(to="<from_host>", type="ack", reply_to="<id>", body="acknowledged")`
  - question → `mori-msg_send(to="<from_host>", type="reply", reply_to="<id>", body="...")`
- **decision / broadcast** — awareness items only (lower prominence)

Skip this section silently if no pending messages or if `mori-msg_recv` fails (daemon may not be running).

On `--post-compact`, also catch cross-device traffic that arrived during the session:
call `mori-nats_sub(replay=true, wait=2)` and surface anything new.

### 4. Stamp the marker

Write the current UTC time to the marker file (see **Marker file** above). Do this in
both modes, after the tool calls.

### 5. Report

**Standard brief:** report "Ready" — summarise what was loaded (memory counts, project
scope, dream state, pending message count if any). Do not take autonomous actions.

**Post-compact brief:** report a one-line **abbreviated** re-grounding summary, e.g.
"Re-grounded — N changed, M superseded, K messages". Do not re-dump the full base; the
working context is already preserved by the compaction summary.
