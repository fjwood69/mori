- name: ingest
- description: Universal ingestion — extract durable memories from PDFs, images, transcripts, git history, and code

1. If the user passes `--status`, call `mori-ingest_status` and present the table.
2. If the user passes `--preview` or `--dry-run` with no source provided, remind them to provide `--source <path>`.
3. For `--preview`: call `mori-ingest_preview` with the `--source` path(s) and any `--type` or `--since` arguments. Present the chunk breakdown and cost estimate. Remind the user this is zero-cost — no LLM was called.
4. For `--dry-run`: call `mori-ingest` with `dry_run=true` and all provided arguments. Report what would be written and the actual cost incurred (the LLM was called, just nothing committed).
5. For a real ingestion run: call `mori-ingest` with all provided arguments. Report: sources processed, chunks sent, memories written, cost estimate, errors.
6. If errors occurred for specific files, report them individually.
7. After successful ingestion, suggest running `/brief` to reload shared memories so the new entries are visible.
8. For large or expensive-looking sources, suggest `/ingest --preview` first to check before committing.

## Argument mapping

| User flag | MCP tool param |
|-----------|---------------|
| `--source <path>` (repeatable) | `source` list |
| `--type <type>` | `type` (auto, transcripts, git, docs, image) |
| `--focus <area>` | `focus` (all, decisions, architecture, conventions, gotchas) |
| `--tier <tier>` | `tier` (working, canonical, ephemeral) |
| `--tags <tags>` | `tags` (comma-separated string) |
| `--since <duration>` | `since` (e.g. "30d", "90d") |
| `--dry-run` | `dry_run=true` |
| `--force` | `force=true` |
| `--model <model>` | `model` (passed but currently ignored — uses dream VK) |
| `--max-cost <amount>` | `max_cost` (float, USD) |
| `--preview` | call `mori-ingest_preview` instead of `mori-ingest` |