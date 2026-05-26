# Changelog

## v0.1.4 — Remote client ingestion, GitHub Actions CI/CD

### Remote client ingestion (`mori_ingest_content`)

Solves the remote-server boundary problem. `mori_ingest` resolves paths
server-side — unusable when mori-advisor runs on GCE and the client is on a
different machine. `mori_ingest_content` flips the model: the MCP client
(Claude Code, Cursor, etc.) reads files locally, base64-encodes them, and
sends bytes over the wire. The server processes in memory.

- **`Chunk.from_content()`** — create chunks from raw bytes rather than filesystem paths
- **`parse_content()`** on all 5 parsers (text, PDF, image, transcript, git) — in-memory
  extraction; git parser accepts pre-collected `git log --patch` stdout as `text/x-git-log`
- **`IngestionJob` + `_run_pipeline()`** — shared execution engine used by both
  `ingest()` (path-based) and `ingest_content()` (wire-based); no logic duplication
- **`_parser_for_mime()`** — MIME routing table maps content types to registered parsers
- **New MCP tool**: `mori_ingest_content` — accepts `[{name, content_b64, mime_type}]`
- **`/ingest` skill updated** — dual-mode: resolves paths locally, reads + encodes files,
  calls `mori_ingest_content`; batches ≤20 files/call; git log collected client-side
- **Dedup**: SHA256 computed from decoded bytes; `source_uri = "<content:name>"`
- **Allow lists**: `mcp__mori__mori_ingest_content` added across all bridge installers

### GitHub Actions CI/CD

- **`ci.yml`** — ruff lint + format check + Docker build check on every push/PR to `main`
- **`cd.yml`** — multi-arch (`linux/amd64,linux/arm64`) build+push to
  `ghcr.io/fjwood69/mori` on semver tags; blue/green GCE deploy with exponential-backoff
  health check — new container starts as `mori-advisor-new`, old container only stopped
  after health passes; rolls back on failure
- **`pyproject.toml`** — ruff config: `line-length = 100`, `target-version = "py313"`

## v0.1.3 — Universal Ingestion, model refactor, shared utilities

### Ingestion pipeline (`/ingest`)

Cold-start problem solved. Feed Mori any source material — PDFs, screenshots,
CC transcripts, git history, plain text — and the pipeline extracts durable
memories into the shared store using the same distillation logic as dream.

- **5 parsers**: text/code, PDF (pymupdf preferred, pypdf2 fallback), image/vision
  (Pillow → base64 → Kimi K2.6 via OpenAI Vision format), CC transcripts (.jsonl
  with `--since` filter via first-event timestamp), git history (`git log` +
  diffs via subprocess)
- **Three-tier execution**: preview (parse-only, zero-cost), dry-run (full LLM
  but no writes), ingest (commits everything)
- **Persistence**: `ingestion_log` table with SHA256 dedup, `--force` to re-ingest
- **Cost guard**: `--max-cost` per-source with token estimation (heuristic — not
  pixel-perfect for image-heavy PDFs)
- **Focus extraction**: architecture, decisions, conventions, gotchas
- **New MCP tools**: `mori_ingest`, `mori_ingest_status`, `mori_ingest_preview`
- **Slash command**: `/ingest --source <path> [--preview | --dry-run] [--focus decisions] [--since 30d]`

### Model architecture refactor

Three distinct model roles, each with its own VK and env var:

| Role | Default model | Default VK | Use |
|------|--------------|------------|-----|
| Advisor | `moonshotai/kimi-k2.6` | `moku-advisor-local` | `/consult` strategic guidance |
| Dream | `moonshotai/kimi-k2.6` | `moku-dream-local` | Dream pipeline + ingestion distillation |
| Fast | `Novita/deepseek/deepseek-v4-flash` | `moku-fast-local` | Contradiction scans, cheap checks |

New env vars: `MORI_ADVISOR_MODEL`, `MORI_DREAM_MODEL`, `MORI_FAST_MODEL`,
`MORI_BIFROST_ADVISOR_VK`, `MORI_BIFROST_DREAM_VK`, `MORI_BIFROST_FAST_VK`.

### Shared utilities

`utils.py` extracted from dream.py — `parse_model_json_response()` and
`run_contradiction_scan()` now shared between dream and ingestion pipelines.
Reduces duplication, single point of maintenance for JSON response parsing.

### Vision support

`BifrostClient.consult_vision()` — multimodal ingestion routes images through
Kimi K2.6 via standard OpenAI Vision content array format. Dream model only
(fast model DeepSeek V4 Flash does not support vision).

### Fixes

- **VK_CONFIG**: corrected from `mori-*-local` to `moku-*-local` to match actual Bifrost DB keys

### Installer improvements

Brought all three bridge installers (Claude Code, Cursor, Antigravity) to full parity:

- **Doctor mode** (`-Doctor` / `--doctor`) — validates settings.json, MCP config, server health, event hooks, permissions seeding, and skills; each check includes an actionable fix hint
- **UpgradeSkills** (`-UpgradeSkills` / `--upgrade-skills`) — skips already-deployed skill folders by default; flag forces refresh
- **MCP permissions seeding** — `permissions.allow` populated with all 31 `mcp__mori__*` tools; eliminates per-call permission prompts in Claude Code and Cursor
- **Hook discriminator** (`_mori_managed: true`) — hook entries now carry a reserved field; merge identifies Mori hooks by field rather than command-string substring; backwards-compatible fallback for old installs
- **Hook merge fix** — per-event in-place merge preserves non-Mori hooks; previous behaviour replaced the entire hooks object on re-run
- **MCP allow list expanded** from 13 to 31 tools — previous list missing `pensieve`, `standards_reload`, all `mori_ingest_*` tools, and extended memory management tools
- **Headless detection** (PS1) — wizard prompts suppressed when required args supplied on CLI

### Docs

- Configuration reference updated with model role and VK env vars
- `.env.example` updated with three model roles and Bifrost VK section
- Slash commands reference documents `/ingest`
- `docs/getting-started/claude-code.md` — new installer flags, Verify It's Working, Troubleshooting, and Known Limitations sections added
- Changelog created (this file)

## v0.1.2 — Security fixes, Antigravity IDE, built-in standards

### Security
- Command injection fix: `/update` tool sanitises skill names before shell interpolation
- LLM-in-transaction fix: contradiction scan runs outside the DB write lock
- Concurrency fix: MemoryStore and SessionLog use per-method short-lived connections
- Hostname spoofing fix: client param removed from memory_write MCP tool

### New features
- Google Antigravity IDE setup documentation and bridge installer scripts
- Built-in standards shipped in Docker image by default
- External service access standards document
- NATS slash command: nats.skill.md for `/nats ping`, `/nats sub`, `/nats pub`

### Improvements
- Dream pipeline contradiction scan routed to fast model (Novita DS V4 Flash)
- README Provider Policy section replaced with Recommended Models table
- Updated image URLs in README
- mori-cline-plugin v0.1.2 with event hooks and spooler
- Alpine security patches in Dockerfile

### Docs
- Antigravity IDE getting-started guide
- External service access standards
- mori-shipper VS Code extension README

## v0.1.1 — Moku → Mori rename, mori-shipper VS Code extension

- Project renamed from Moku to Mori throughout codebase, docs, and configs
- mori-shipper VS Code extension (v0.1.1) — ships events from VS Code-native CC instances
- README images and terminology update

## v0.1.0 — Initial release

- Dream pipeline: session event distillation into durable memories
- Persistent memory store with versioning, attribution, protection
- Session context (`/brief`) with standards injection and freshness checks
- Strategic advisor (`/consult`) with focus areas
- Cross-device NATS messaging
- Skill deployment (`/update`)
- Requirements tracking (`/req`)
- Memory governance: trusted dreamers, pending writes, approval workflow
- Export/import for portability
- Docker Compose, Podman, macOS native, Windows, GCP deployment paths
- Claude Code, Antigravity, and Cline integration
