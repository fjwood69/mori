# Changelog

## v0.1.13 — Git push NATS notification

![Mori — A shared memory layer for AI coding agents](https://raw.githubusercontent.com/fjwood69/mori/842fbfb3912db78e52a2e6a692e4f3f5bc3fff95/docs/assets/header-blank.svg)

### New: git push NATS notification hook

When you push to any git repo with the hook installed, a `GitPush` event is published immediately to NATS — so every other active Claude Code instance sees the push in real time via `/nats sub` and `/brief` replay.

**New files:**
- `scripts/post-push.sh` / `scripts/post-push.ps1` — the hook itself; always `exit 0`, fire-and-forget
- `scripts/install-git-hooks.sh` / `scripts/install-git-hooks.ps1` — one-command install per repo
- `docs/getting-started/git-hooks.md` — installation guide

**Server change (`main.py`):**
- `_nats_publish_git_push` helper — publishes to `cc.<client>` immediately on receipt, bypassing the dream pipeline for instant cross-device visibility
- `/api/events/raw` handler — fires the NATS publish via `asyncio.create_task` after logging `GitPush` events

**Install:**
```bash
# From the mori repo root
./scripts/install-git-hooks.sh

# Other repos
./scripts/install-git-hooks.sh --repo ~/bifrost
./scripts/install-git-hooks.sh --repo ~/dotfiles
```

Set `MORI_URL`, `MORI_API_KEY`, `MORI_CLIENT` in your environment — see `docs/getting-started/git-hooks.md`.

---

## v0.1.12 — NATS import fix

![Mori — A shared memory layer for AI coding agents](https://raw.githubusercontent.com/fjwood69/mori/1eb4fa8efffcc66643da9ad3ad85ad70319629283/docs/assets/header-blank.svg)

### Fix: `TimeoutError` import path

`nats.js.errors` does not export `TimeoutError` — caused `ImportError` on deploy (Python 3.14). Changed to `nats.errors.TimeoutError`.

---

## v0.1.11 — `/wrap` skill, NATS replay fix

![Mori — A shared memory layer for AI coding agents](https://raw.githubusercontent.com/fjwood69/mori/559229efffcc66643da9ad3ad85ad70319629283/docs/assets/header-blank.svg)

### New `/wrap` skill

Session wrap-up as a single command — captures work before a release. Runs the full sign-off sequence:

- **Summarise** — writes a concise session summary
- **cc-share** — publishes to cross-session storage (7-day TTL)
- **NATS** — broadcasts one-liner to the message bus
- **Dream** — flushes undreamed events to durable memory

Use before every release tag to avoid losing session context when the MCP server restarts.

### NATS replay fix

`nats_sub(replay=True)` silently returned "No NATS messages" because the `cc` JetStream stream was never created. The replay branch now auto-creates the stream on first call and cleans up ephemeral consumers after each read.

Includes the lint fix from CI: removed unused `StreamConfig` import and ruff-organised inline imports.

---

## v0.1.9 — /update skill deployment fixed

![Mori — A shared memory layer for AI coding agents](https://raw.githubusercontent.com/fjwood69/mori/89af2974c249b473e426199e3e574c05c4119364/docs/assets/header-blank.svg)

### `/update` skill deployment — three fixes

The `/update` MCP tool generates shell commands to deploy skills to all Claude profile
directories on a target device. Three bugs prevented it from working at all.

**Skills now shipped in the Docker image**

`skills/` directory was not included in the Dockerfile `COPY` — `MORI_SKILLS_DIR` was
unset and `_list_skills()` always returned an empty list. Fixed:

```dockerfile
COPY skills/ ./skills/
ENV MORI_SKILLS_DIR=/app/skills
```

**Correct subdirectory format**

Skills were stored as flat files (`skills/brief.skill.md`) but Claude Code expects and
`_list_skills()` looks for subdirectory format (`skills/brief/SKILL.md`). All 7 skill files
renamed to match:

```
skills/brief/SKILL.md
skills/consult/SKILL.md
skills/dream/SKILL.md
skills/ingest/SKILL.md
skills/nats/SKILL.md
skills/pensieve/SKILL.md
skills/req/SKILL.md
```

**Bash generation for Linux devices**

`_update_all()` always emitted PowerShell syntax regardless of device family, producing broken
commands for Linux targets (CB14P, NUC). Fixed with a family branch — Linux devices now get
bash heredoc commands; Windows (Twiggy, UX3405) retains PowerShell output.

**Usage after this release:**

```
/update cb14p all   → pasteable bash that deploys all 7 skills to 4 profile dirs
/update twiggy all  → pasteable PowerShell equivalent
```

---

## v0.1.8 — Project-scoped /brief, dream auto-tagging

![Mori — A shared memory layer for AI coding agents](https://raw.githubusercontent.com/fjwood69/mori/89af2974c249b473e426199e3e574c05c4119364/docs/assets/header-blank.svg)

### Project-scoped `/brief`

`/brief` previously loaded all memories up to a hard cap of 50 — bifrost sessions got mori
memories they didn't need, and busy projects lost relevant memories to truncation. Project
scoping fixes both problems simultaneously.

**Three new `/brief` invocations:**

| Command | Effect |
|---|---|
| `/brief` | Unscoped — existing behaviour, all memories up to cap |
| `/brief --project <name>` | Scoped to a project — right memories in full |
| `/brief --auto` | Auto-detect project from working directory |

**Three-bucket loading (scoped mode):**
- **Project memories** — canonical always in full; working ≤14 days in full; working >14 days as summary only
- **Global memories** — `scope:global`, `scope:cross-project`, type `profile`/`pattern` — always loaded regardless of project
- **Other-project index** — one line per project with count; cross-project awareness without loading cost

Output header:
```
**Mori Brief — project: mori** (23 project + 18 global memories)
153 memories from other projects — /pensieve to explore
```

**Implementation:**
- `memory_store.get_memories_by_project()` — all filtering pushed to SQLite; no superset-then-filter in Python
- `brief()` MCP tool gains `project`, `include_global`, `include_index` parameters
- Requirements filtered to current project when scoped
- Graceful fallback to unscoped on any exception

### Dream pipeline auto-tagging

New memories written by the dream pipeline are now automatically tagged `project:<name>` based
on the working directory of the session that produced them.

**Resolver chain** (first match wins):
1. `.mori-project` file — place at repo root (or any parent) with the project name as content
2. `MORI_PROJECT` environment variable — for CI or non-interactive shells
3. `git rev-parse --show-toplevel` — uses the git repository root directory name as fallback

New methods on `DreamPipeline`: `_resolve_project(cwd)` and `_extract_project_from_events(events)`.

### Backfill migration script

`scripts/backfill_project_tags.py` — one-time idempotent pass to tag existing memories.
Maps name prefixes to project tags (`project-mori-*` → `project:mori`, etc.) and adds
`scope:global` to profiles, patterns, and cross-cutting memories. Safe to re-run.

```bash
python scripts/backfill_project_tags.py /data/mori-advisor/memories.db --dry-run
python scripts/backfill_project_tags.py /data/mori-advisor/memories.db
```

### Docs

- `docs/for-teams.md` — new **Project scoping** section: commands, resolver chain, backfill instructions, cost-annotated example configs updated with actual `--auto` / `--project` commands
- `docs/reference/slash-commands.md` — `--project` and `--auto` flags documented with cost table

---

## v0.1.4 — Remote client ingestion

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

## v0.1.1 — mori-shipper VS Code extension

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
