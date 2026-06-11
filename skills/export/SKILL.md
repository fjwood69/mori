---
name: export
description: Bundle the canonical memory set into one structured Markdown document for external-LLM review, audit, or dashboard download.
---

**If the Mori server is unreachable** (a tool call returns a connection error, ECONNREFUSED, or timeout), do not show a stack trace. Tell the user the Mori server appears to be down and that a running Mori server is required. Point them to the quickstart at https://github.com/fjwood69/mori#quickstart and suggest checking the server URL in plugin settings. Then stop.

When the user runs `/export`, call `mori-export_canon` with the parsed arguments and present the result. Internal provenance/PII is stripped by the server by default — the export is safe to upload to an external model.

## Argument parsing

- `--project <name>` — filter to a project tag (e.g. `mori`, `bifrost`)
- `--type <type>` — filter by memory type (`decision`, `pattern`, `standard`, …)
- `--format <fmt>` — one of:
  - `standard` (default) — grouped, readable Markdown with a reproducibility pin header
  - `consult` — the same content prefixed by a **coherence-review rubric**, ready to paste into Kimi/Qwen/Opus (alongside `/consult`) for a canon audit
  - `json` — raw structured JSON
- `--output <path>` — write the result to a file instead of returning it inline
- `--include-working` — include working-tier memories as well as canonical

## Execution

1. Parse the arguments above.
2. Call `mori-export_canon` with `project` / `type` / `format` / `include_working`.
3. If `--output <path>` was given, write the returned string to that path and report the path + size; otherwise present the export directly.
4. For `--format consult`, tell the user it's ready to paste into an external model alongside `/consult` for a coherence audit (the rubric scores internal coherence — contradictions, redundancy, clarity — not whether claims match the live codebase).
