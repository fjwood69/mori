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

## Content mode (remote clients — HTTP upload)

When the user passes `--content <path>`, upload the file directly to the mori-ingestion pod.
Do NOT use the `mori-ingest_content` MCP tool — it has been removed. Use HTTP multipart upload only.

Requires:
- `MORI_INGEST_URL` env var (set by installer — NOT derived from `MORI_URL`)
- `MORI_API_KEY` env var (same key as MCP auth)

### Linux/macOS

```bash
JOB=$(curl -sf -X POST "${MORI_INGEST_URL}/api/ingest/upload" \
  -H "X-Api-Key: ${MORI_API_KEY}" \
  -F "files=@${path}" \
  -F "focus=${focus}" \
  -F "tier=${tier}" \
  -F "tags=${tags}" \
  -F "dry_run=${dry_run}")
JOB_ID=$(echo "$JOB" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
```

### Windows (PowerShell)

```powershell
$r = Invoke-RestMethod -Uri "$env:MORI_INGEST_URL/api/ingest/upload" `
  -Method POST `
  -Headers @{"X-Api-Key" = $env:MORI_API_KEY} `
  -Form @{
    files   = Get-Item $path
    focus   = $focus
    tier    = $tier
    tags    = $tags
    dry_run = $dry_run
  }
$JOB_ID = $r.job_id
```

### Polling (both platforms)

Poll every 5 seconds until `status` is `complete` or `failed`:

```bash
# Linux/macOS
while true; do
  STATUS=$(curl -sf "${MORI_INGEST_URL}/api/ingest/job/${JOB_ID}" \
    -H "X-Api-Key: ${MORI_API_KEY}")
  STATE=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  [ "$STATE" = "complete" ] || [ "$STATE" = "failed" ] && break
  sleep 5
done
echo "$STATUS"
```

```powershell
# Windows
do {
  $status = Invoke-RestMethod -Uri "$env:MORI_INGEST_URL/api/ingest/job/$JOB_ID" `
    -Headers @{"X-Api-Key" = $env:MORI_API_KEY}
  if ($status.status -notin "queued","running") { break }
  Start-Sleep 5
} while ($true)
$status
```

Report: memories written, estimated cost, errors. After success, suggest `/brief` to reload shared memories.

### Error handling

- `MORI_INGEST_URL` not set → tell user to set it (e.g. `http://100.90.219.111:8969`) and re-run
- HTTP 413 → file too large (50MB/file, 200MB total)
- HTTP 401 → API key mismatch — check `MORI_API_KEY`
- status `failed` → report `errors` field from job response

## Argument mapping

| User flag | Action |
|-----------|--------|
| `--source <path>` (repeatable) | `mori-ingest` MCP tool → `source` list |
| `--content <path>` | HTTP multipart upload to `MORI_INGEST_URL` |
| `--type <type>` | `type` (auto, transcripts, git, docs, image) |
| `--focus <area>` | `focus` (all, decisions, architecture, conventions, gotchas) |
| `--tier <tier>` | `tier` (working, canonical, ephemeral) |
| `--tags <tags>` | `tags` (comma-separated string) |
| `--since <duration>` | `since` (e.g. "30d", "90d") — filesystem mode only |
| `--dry-run` | `dry_run=true` |
| `--force` | `force=true` |
| `--max-cost <amount>` | `max_cost` (float, USD) — filesystem mode only |
| `--preview` | call `mori-ingest_preview` instead of `mori-ingest` |
| `--status` | call `mori-ingest_status` |
