- name: ingest
- description: Universal ingestion — extract durable memories from PDFs, images, transcripts, git history, and code

## Filesystem mode (server-local paths)

1. If the user passes `--status`, call `mori-ingest_status` and present the table.
2. If the user passes `--preview` or `--dry-run` with no source provided, remind them to provide `--source <path>`.
3. For `--preview`: call `mori-ingest_preview` with the `--source` path(s) and any `--type` or `--since` arguments. Present the chunk breakdown and cost estimate. Remind the user this is zero-cost — no LLM was called.
4. For `--dry-run`: call `mori-ingest` with `dry_run=true` and all provided arguments. Report what would be written and the actual cost incurred (the LLM was called, just nothing committed).
5. For a real ingestion run: call `mori-ingest` with all provided arguments. Report: sources processed, chunks sent, memories written, cost estimate, errors.
6. If errors occurred for specific files, report them individually.
7. After successful ingestion, suggest running `/brief` to reload shared memories so the new entries are visible.
8. For large or expensive-looking sources, suggest `/ingest --preview` first to check before committing.

## Content mode (remote clients)

When the server runs on GCE and cannot access the client filesystem,
use `--content <path>` instead of `--source <path>`. This reads the
file client-side, base64-encodes it, and sends it over the wire.

1. For `--content <path>`: read the file client-side (not on the server),
   base64-encode the bytes, determine the MIME type, then call
   `mori-ingest_content` with `files = [{name, content_b64, mime_type}]`.
2. Git repos via content mode: run `git log --patch` locally, pass the
   output as `text/x-git-log` MIME type.
3. `--since` filtering is **not available** in content mode for transcripts
   (file mtime is unknown from bytes). Pre-filter JSONL files by mtime
   client-side before sending.
4. Maximum file size: 10MB per file (enforced server-side).
5. Dedup uses SHA256 hashing of the file bytes — same content sent twice
   is skipped unless `--force` is used.

## Argument mapping

| User flag | MCP tool param |
|-----------|---------------|
| `--source <path>` (repeatable) | `mori-ingest` → `source` list |
| `--content <path>` | `mori-ingest_content` → `files` list (read + b64 encode client-side) |
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
