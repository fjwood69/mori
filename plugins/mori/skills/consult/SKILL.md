---
name: consult
description: Strategic guidance from the advisor model on a question, with optional focus area and depth. Use before consequential decisions or to review a plan.
---

## Argument Parsing

Parse the user's input for:
- First positional argument → `question`
- `--focus`: one of `general`, `architecture`, `security`, `performance`, `style`
- `--depth`: one of `quick`, `balanced`, `deep`
- `--file` / `-f`: file path(s) to include as context

## Attachment verification (Part 2.1 — fail-loud)

Before calling `mori-consult_advisor`, scan the question text for attachment references:
- Explicit `(attached: <name>)` patterns
- Instructions matching a read-the-source intent: "attached", "READ the", "read the actual",
  "source read", "vs pin", "primary source", "primary-source"

For each referenced file: verify it exists on disk. If **any** referenced file is absent, **abort —
do not call `mori-consult_advisor`** — and report:

```
CONSULT ABORTED: attached file(s) missing from payload:
  - <path>  (referenced in question but not found on disk)
Fix: pass --file <path> or ensure the file exists before consulting.
```

After verifying (or if no attachment references), append a manifest line to the context:

```
ATTACHED FILES: <name1> (<bytes>B), <name2> (<bytes>B)
```

or `ATTACHED FILES: none` if no files were supplied.

## Source-dependence classification (Part 2.2 — mode-integrity)

A consult is **source-dependent** if the question contains any attachment reference OR `--file`
arguments were supplied.

## File content injection (client-side)

If `--file` arguments were supplied and all files passed verification:

For each path, use the CC `Read` tool to read the file content **client-side**. Format each
as a fenced code block with a `### <filename>` header:

    ### path/to/file.py
    ```python
    <content>
    ```

Prepend all file blocks to the `context` parameter (before any other context text).
Record each file's name and byte count (bytes = `len(content)`) for the `ATTACHED FILES:` manifest.

**Do NOT pass raw paths in `files=`** — the MCP server runs on remote GCE; local filesystem
paths are inaccessible there. Always pass `files=[]` (empty list); file content arrives via `context`.

## Execution

1. Run attachment verification. Abort loudly if any referenced file is missing.
2. Classify source-dependence.
3. Read each `--file` path client-side (CC `Read` tool); prepend content blocks to `context`; append `ATTACHED FILES:` manifest.
4. Call `mori-consult_advisor` with `question`, `context`, `focus`, `depth`, and `files=[]`.
5. Present the result.

**On error or empty response:**
- **Not source-dependent**: retry once with `--depth quick`.
- **Source-dependent**: do NOT fall back. Return:

```
CONSULT UNAVAILABLE (source-dependent; fallback would be source-blind).
Options: retry manually, postpone, or proceed without consultation (disclose this).
```

Empty deep-mode responses must surface as `EMPTY_RESPONSE (elapsed: Xs)` — not silent success.
