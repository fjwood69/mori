# Changelog

## v2.1.19 — Move Dockerfile to Python 3.13

- **`Dockerfile`**: Change `PYTHON_VERSION` from `3.14.5` to `3.13.4`. FastMCP 3.2.0 has a
  `custom_route` registration bug on Python 3.14 where routes defined after a certain index in
  the handler list silently fail to register. Python 3.13 is the current supported release with
  active security patches — not the EOL-approaching 3.12 we briefly tried. UAT masked this
  because the local build uses the system Python 3.12 on NUC. Production was running Python 3.14
  (Dockerfile default), which explains why all new endpoints returned 404 despite the code being
  present in the image.

## v2.1.18 — Pin FastMCP to 3.2.0

- **`requirements.txt`**: Pin `fastmcp==3.2.0`. FastMCP 3.3.1 silently dropped newly-registered
  `custom_route` handlers after a certain point in the handler list — only routes present before
  v2.1.16 were reachable. UAT masked this because the local build resolved 3.2.0 while GitHub
  Actions resolved the latest compatible (3.3.1). (Root cause turned out to be Python 3.14 in the
  Dockerfile, not the FastMCP version — see v2.1.19.)

## v2.1.16 — Git commit ingestion + consult output capture

- **`POST /api/git/ingest`**: New endpoint that ingests git commit messages from a post-push
  hook. Each commit is written as a working-tier project memory tagged `project:<repo>` and
  `pusher:<client>`. Server-side dedup via `ingestion_log.source_hash` makes repeated calls
  idempotent. Watermarks are per `(repo, ref)` so pushes to different branches maintain
  independent ingestion state.
- **`GET /api/git/watermark`**: Narrowly-scoped endpoint that returns the last ingested
  commit SHA for a given `(repo, ref)`. Used by post-push hooks to compute the commit range
  without exposing the full dream state keyspace.
- **`scripts/post-push.sh` / `post-push.ps1`**: Extended with a git commit ingestion block.
  API key is sourced from `~/.claude/.secrets` at push time (not the shell profile). Commit
  body text is included alongside the subject. Output: `[mori] ingested N commit(s) from
  <repo>/<branch>` on success. Hook always exits 0 — never blocks a push.
- **Consult output capture**: Every successful `consult_advisor` call now writes the question,
  focus, and advisor response as a working-tier project memory tagged `consult` and
  `advisor-output`. The dream pipeline reviews and promotes advice that was followed; advice
  that was superseded ages out naturally. Set `MORI_CONSULT_CAPTURE=false` to opt out.

## v2.1.15 — Postgres-first GCP deployment

- **Postgres in GCP startup script**: `deploy/gcp/startup.sh.tpl` now starts a Postgres 16
  container bound to `/data/postgres/pgdata` on the persistent disk as part of the standard boot
  sequence. Postgres data survives VM stops and rebuilds — named container volumes are no longer
  used for stateful data.
- **`MORI_REQUIRE_POSTGRES`**: New env var — if set to `true`, mori-advisor aborts at startup
  when Postgres is unreachable, preventing silent fallback to SQLite. Recommended for all team
  and GCP deployments.
- **`pg_isready` startup gate**: mori-advisor will not start until Postgres accepts connections
  (30×2s timeout with fatal exit). Eliminates the previous race condition where the server could
  start against an unavailable database.
- **pg_dump backup cron**: Daily `pg_dump` to GCS replaces the SQLite Litestream backup cron
  in the GCP deployment path. Backups use GCE metadata server auth — no credentials in env vars.
- **Credentials via Secret Manager**: GCP deployment fetches the Postgres password from GCP
  Secret Manager at boot and writes `MORI_DATABASE_URL` to `/data/mori-advisor/.env` on the
  persistent disk. No credentials in the startup script or repository.
- **Tailscale state preserved across rebuilds**: Startup script restores Tailscale state from
  the persistent disk so the VM retains its Tailscale identity after a rebuild.
- **SSH host keys preserved across rebuilds**: Startup script restores SSH host keys from the
  persistent disk to prevent host-key warnings after VM recreation.
- **skills/brief: remove dead `mori-config` pull step**: The `git -C ~/mori-config pull` step
  in the `/brief` skill was a leftover from an earlier config management approach. Removed from
  both `mori/skills/brief/SKILL.md` and the installed skill files.

## v2.1.14 — Fix Windows Installer Hook Format & Session-Based Auth

- **Windows Installer Hook Format Fix**: Fixed a bug in `scripts/install-mori-claude.ps1` where the `PostToolUse` event hook was missing the `matcher` field (e.g. `matcher: "*"`), which caused Claude Code to reject the generated configuration. The installer now matches the correct hook wrapping behavior of `install-mori-claude.sh`.
- **Session-Based Auth Bypass**: Added an in-memory session tracker `_AUTHENTICATED_SESSIONS` to `ApiKeyMiddleware` to bypass API key headers validation for subsequent POST/DELETE requests belonging to successfully pre-authenticated SSE connections (fixing connection handshake `401 Unauthorized` issues on IDE restart).

## v2.1.13 — Native Prometheus /metrics Exposition

- **Native Prometheus Exposition**: Replaced `/metrics` endpoint implementation with a native Prometheus exposition format (`text/plain; version=0.0.4` / OpenMetrics compatibility) using `prometheus_client` directly, allowing direct scraping by homelab Prometheus instances without an intermediate OTel collector.
- **Pluggable Database Count & Filters**: Extended the memory store interfaces (`count()`, `pending_count()`, `count_messages()`, `count_ingestion()`) for both SQLite and Postgres backends to support filtering (such as memory tier, protection status, message status) directly in the database queries.
- **OTel Backward Compatibility**: Preserved OpenTelemetry gauges update flow inside the `/metrics` endpoint scrape handler, ensuring any configured background push exporter continues receiving metrics updates.

## v2.1.12 — Fix MCP Session Auth Bypass for SSE POST/DELETE Requests

- **Session-Based Auth Bypass**: Added an in-memory session tracker `_AUTHENTICATED_SESSIONS` to `ApiKeyMiddleware`. Once a client successfully authenticates via API key on the initial SSE GET request, subsequent POST/DELETE requests belonging to that session ID bypass the API key header check, fixing `401 Unauthorized` errors on clients that fail to propagate custom headers.

## v2.1.11 — Postgres UAT Dream Run Fixes & Savepoint Isolation

- **Postgres Savepoint Isolation**: Wrapped each `_write_memory()` call in the dream pipeline inside a nested transaction (savepoint) using `async with txn_conn.transaction():` when running on Postgres (`asyncpg`). This prevents individual database write failures (such as unique key constraint violations) from aborting the entire transaction block, ensuring successful memory writes persist and the watermark advances cleanly.
- **Dream datetime fix**: `dream.py` event grouper was slicing `TIMESTAMPTZ` values returned by asyncpg as `datetime` objects — not strings — causing `dream_run` to crash with `TypeError: 'datetime.datetime' object is not subscriptable`. Fixed to use `.isoformat()` when the value has that method.
- **Database Seeding Sequence Reset**: Added automatic primary key sequence resetting to `start-uat.sh` immediately following the `pg_dump` seed step. Resets sequences to `COALESCE(max(id), 1)` for `memories`, `memory_versions`, `pending_writes`, `ingestion_log`, `session_events`, and `delegate_tasks` to prevent constraint conflicts on subsequent insertions.
- **Smoke Test Robustness**: Upgraded `smoke-test.sh` to dynamically report check keys and handle JSON parsing errors robustly, and to gracefully output and skip display for `db_write` (marked as `skipped`) instead of treating it as a test failure when run against the Postgres backend.
- **APP_PORT**: `mori_advisor/main.py` server port is now configurable via `APP_PORT` env var (defaults to 8968). Enables side-by-side UAT instances without rebuilding the image.

## v2.1.10 — Antigravity Installer Profile Parity & PostCompact Hook

- **Target Selection**: Added `--target cli/ide/both` (Bash) and `-Target cli/ide/both` (PowerShell) option to installers, directing MCP config (`mcp_config.json`) and hooks (`hooks.json`) to `~/.gemini/antigravity` (CLI), `~/.gemini/antigravity-ide` (IDE), or both. Default in headless mode is `ide` to match the NUC active IDE app data folder.
- **PostCompact Hook**: Deployed `mori-post-compact-brief` shipper script and registered the `PostCompact` hook in Antigravity's `hooks.json` configuration, matching Claude Code installer capability to trigger automatic re-grounding via `/brief`.
- **Robust Skill Parsing**: Upgraded the PowerShell skill installer `Deploy-MoriSkills` to support both standard YAML frontmatter blocks (`---`) and bulleted headers (`- name:`).
- **Symlink Diagnostics**: Upgraded the `--doctor` diagnostics in `mori_antigravity_install.py` and `install-mori-antigravity.ps1` to detect and print remediation instructions when the `~/.gemini/config` symlink points to a mismatching variant.

## v2.1.9 — Fix Postgres brief() interface mismatches

- **`get_memories_by_project`**: Rewrote `PostgresStore.get_memories_by_project()` to return the correct three-key dict (`project_memories`, `global_memories`, `other_projects`) matching the SQLite spec. The previous implementation returned `{name: dict}` which caused a `KeyError('project_memories')` in `brief()`.
- **`check_freshness`**: Fixed `PostgresStore.check_freshness()` — was calling `await llm_consult(dict(row))` (wrong: sync function, wrong argument shape, wrong return shape). Now fetches all rows first, releases the connection, calls `llm_consult(system=..., user=..., vk="fast", ...)` synchronously per row with a fresh connection for each write, and returns `{checked, fresh, stale, no, errors}`.

## v2.1.8 — Async Postgres Ingestion & Security Hardening

- **Async Ingestion Pipeline**: Converted `IngestionPipeline` execution flow and ingestion tasks to `async def` and integrated the dynamic `_a()` helper to resolve and await asynchronous `PostgresStore` writes/logs.
- **Async Contradiction Scans**: Refactored `run_contradiction_scan` to be async-native. Under Postgres, it operates inside non-blocking database transactions to perform updates and queue eviction notices.
- **MCP Endpoint Security**: Removed `/mcp` from `OPEN_PATHS` in `ApiKeyMiddleware`, requiring a valid API key for all MCP connections and tool invocations, and added query-based API key support (`api-key` / `api_key`) to support cloud-discovered Claude Code clients.
- **NATS Timeout in Smoke Test**: Wrapped `nats.connect` inside `asyncio.wait_for` with a 2.0 second timeout to prevent the health check/smoke endpoint from hanging indefinitely during auth failures.
- **UAT & Installer Verification**: Verified local UAT execution against the Postgres standby node, resolved a double-quoting JSON parsing issue and a missing matcher field in the Claude Code settings installer, and confirmed full installer idempotency.

## v2.1.6 — Fix Postgres dream transaction poisoning

Fix: PostgresStore.write() uses SELECT-then-INSERT, which causes
`UniqueViolationError` when the dream model produces duplicate memory names.
One error poisons the entire transaction, losing all 12+ memories and the
watermark update.

Replaced with `INSERT ... ON CONFLICT (name) DO UPDATE` — matching SQLite's
atomic upsert. Also adds origin array merging, canonical tier preservation,
and protection flag preservation on update.

## v2.1.0 — Named API key authentication + PostCompact re-grounding

### New: PostCompact re-grounding hook

A `PostCompact` hook (`~/.claude/hooks/post-compact-brief.sh`) is now installed
alongside the other Mori lifecycle hooks. It fires after every context compression
and injects a prompt instructing the agent to run `/brief` — re-establishing NATS
messages, pending mori-msg items, and session state from before compaction.

Enabled by default. Opt out with `MORI_POST_COMPACT_BRIEF=false`.

A dedicated `/brief --post-compact` flag that pulls the compact summary directly
is planned; plain `/brief` is the correct interim approach.

### New: per-client named API keys

Mori now authenticates every request at the transport layer — MCP tools, event
endpoints, and the dream trigger — using named API keys. Previously only 4 HTTP
endpoints were protected by a single shared key; the entire MCP surface was open.

**Key format:** `MORI_API_KEYS=name:secret,name:secret,...`

Each client gets its own named key. The name appears in logs and audit trail.
Secrets are 32-byte hex strings generated via `python3 -c "import secrets; print(secrets.token_hex(32))"` or the new `mori-key_generate` MCP tool.

**New modules:**
- `mori_advisor/auth.py` — key loading, `check_key()` with `hmac.compare_digest`, `generate_key()`
- `mori_advisor/middleware.py` — Starlette `BaseHTTPMiddleware`; applied via `mcp.run(middleware=[...])`

**Open paths** (always accessible, no key required): `/health`, `/ready`, `/metrics`

**Open mode:** if no keys are configured, the server starts with a warning and
accepts all connections — preserves backward compatibility for Tailscale-only
deployments.

**Backward compat:** existing `MORI_ADVISOR_API_KEY` deployments continue working
without config changes — the single key is loaded as `{"legacy": <key>}`.

**New MCP tool:** `mori-key_generate name="clientname"` — generates a secret and
returns the line to add to `MORI_API_KEYS`.

**Smoke test:** `/api/smoke` now includes an `auth` check showing configured client names.

**Migration:** see [docs/reference/configuration.md — Authentication](docs/reference/configuration.md#authentication).

---

## v2.0.0 — Dual-backend store (SQLite + PostgreSQL)

![Mori — A shared memory layer for AI coding agents](https://raw.githubusercontent.com/fjwood69/mori/f4fee0826da3ab8b234f8677fa8f96f37ce07e88/docs/assets/header-blank.svg)

### New: pluggable persistence layer — SQLite (solo) or PostgreSQL (team)

Mori now supports PostgreSQL as a drop-in replacement for SQLite, selected at
runtime via `MORI_DATABASE_URL`. SQLite remains the default — zero breaking
change for existing deployments.

**Why this matters:** solo deployments stay on SQLite (no deps, no ops). Team
deployments with concurrent dream runs, PITR backups, or multi-pod write
contention activate PostgreSQL by setting one environment variable.

**New modules:**
- `mori_advisor/store/` — `BaseStore` ABC, `SQLiteStore` (delegation wrapper over
  existing `MemoryStore` / `SessionLog` / `MsgStore`), `PostgresStore` (asyncpg pool)
- `mori_advisor/store/__init__.py` — `get_store()` factory, selects backend from env
- `mori_advisor/cli/export.py` — dump SQLite to JSONL (dependency-safe order, WAL flush)
- `mori_advisor/cli/import_.py` — load JSONL into either backend (idempotent, type-coerced)

**All callers updated:** `main.py`, `dream.py`, `ingestion.py`, `ingestion_server.py`,
`utils.py` — store layer injected via `store=` kwarg, `db_path=` fallbacks preserved.

**PostgreSQL notes:**
- asyncpg pool, `statement_cache_size=0` (pgBouncer session mode compatible)
- JSONB for tag arrays, TIMESTAMPTZ for all timestamps
- Serialization errors (SQLSTATE 40001) retried up to 3× with exponential backoff
- `asyncpg` is optional — not required for SQLite deployments

**Deploy directory restructured:**
- `deploy/solo/` — SQLite posture (Docker Compose, replaces `deploy/homelab/` for Docker users)
- `deploy/team/` — PostgreSQL + pgBouncer (Docker Compose, WAL-G documented)
- `deploy/homelab/` — retained for backward compatibility (raw Podman + systemd units)

**Migration:** export from SQLite, import to Postgres, verify counts match, flip
`MORI_DATABASE_URL`. Rollback: remove the variable, restart — SQLite file untouched.
See [docs/reference/team-configuration.md](docs/reference/team-configuration.md).

**UAT results:** 68/68 memories, 5006/5006 session events verified across both
backends on NUC before tagging.

---

## v1.1.0 — Inter-agent messaging (mori-msg)

![Mori — A shared memory layer for AI coding agents](https://raw.githubusercontent.com/fjwood69/mori/f4fee0826da3ab8b234f8677fa8f96f37ce07e88/docs/assets/header-blank.svg)

### New: `mori-msg` — addressed, typed, reply-threaded messages between agents

Agents can now delegate tasks, ask questions, and share decisions across the device network without a shared session. Messages are picked up at the next `/brief` — no mid-session push, no extra infrastructure.

**New MCP tools:** `mori-msg_send`, `mori-msg_recv`, `mori-msg_thread`

**New daemon:** `mori_advisor/msg_daemon.py` — long-running durable JetStream pull consumer. Same image as `mori-advisor`, different entrypoint. Sole writer to `msg.db`; dispatches by type:
- `decision` → written directly to `memory_store` (no human session needed)
- `task` → persisted + auto-acked; appears in next `/brief`
- `question` / `broadcast` → persisted for `/brief` pickup
- `done` / `ack` / `reply` → update referenced message status

**Infrastructure:** new `MORI_MSG` JetStream stream (`mori.msg.*` + `mori.reply.*`, 7-day retention). Separate `msg.db` (not `memories.db`) — sole writer is the daemon, clean WAL ownership.

**Updated pod stack:** `mori-advisor` (8968) + `mori-ingestion` (8969) + `mori-dream` (internal) + `mori-msg` (internal daemon)

**Skills:** `/brief` calls `mori-msg_recv(unacked=True)` at session start; `/wrap` broadcasts session summary to `mori-msg`; new `/msg` skill for direct inbox/send/thread use.

**Opt-in headless CC:** `MORI_MSG_HEADLESS_ENABLED=true` + `MORI_MSG_HEADLESS_TRUSTED=<hostnames>`. Off by default.

---

## v1.0.0 — AGPL-3.0 licence, defensive publication

![Mori — A shared memory layer for AI coding agents](https://raw.githubusercontent.com/fjwood69/mori/97ee8bb6b52ba12cabcb6ce308a75ce12f7367c5/docs/assets/header-blank.svg)

### Licence: MIT → AGPL-3.0

Mori is now released under the [GNU Affero General Public License v3.0](LICENSE).

Under AGPL-3.0, if you run Mori as a network service and modify the source code, you must release those modifications under AGPL-3.0. A commercial licence removes this requirement — see [COMMERCIAL.md](COMMERCIAL.md).

### Defensive publication — prior art established

[DISCLOSURE.md](DISCLOSURE.md) is a formal technical disclosure establishing prior art for the inventions in Mori: the dream pipeline, PreCompact synchronous distillation, multi-instance memory coherence, three-tier memory lifecycle, trusted dreamer governance, universal ingestion pipeline, and git push cross-instance notification. Published to prevent third-party patenting of these methods.

### What v1.0 represents

Mori has been running in production across a multi-device homelab since May 2026, accumulating 5,000+ session events and 60+ canonical memories across Claude Code, Cursor, and Cline instances. The 1.0 milestone reflects a stable core:

- **Dream pipeline** — automatic session distillation via lifecycle hooks
- **Session grounding** — `/brief` loads shared memories at session start
- **Universal ingestion** — PDFs, images, transcripts, git history → memories
- **Cross-device messaging** — NATS pub/sub, `/wrap`, git push notifications
- **Governance** — trusted dreamers, pending write approval, full version history
- **Smoke test** — `/api/smoke` endpoint for pre-deploy verification

---

## v0.1.14 — Fix GitPush NATS publish

![Mori — A shared memory layer for AI coding agents](https://raw.githubusercontent.com/fjwood69/mori/ea4eb044f8c22bff2ea064cb7aec75a41f1d1303/docs/assets/header-blank.svg)

### Fix: `asyncio.create_task` GC bug in GitPush NATS publish

`asyncio.create_task(_nats_publish_git_push(...))` discards the task reference — Python only holds a weak reference, so the task is garbage collected before it runs and the NATS message is never sent. Changed to `await _nats_publish_git_push(body)`. Also removes the now-redundant local `import asyncio` inside `nats_sub` (moved to module level in v0.1.13).

---

## v0.1.13 — Git push NATS notification

![Mori — A shared memory layer for AI coding agents](https://raw.githubusercontent.com/fjwood69/mori/842fbfb3912db78e52a2e6a692e4f3f5bc3fff95/docs/assets/header-blank.svg)

### New: git push NATS notification hook

When you push to any git repo with the hook installed, a `GitPush` event is published immediately to NATS — so every other active Claude Code instance sees the push in real time via `/nats sub` and `/brief` replay.

**New files:**
- `scripts/post-push.sh` / `scripts/post-push.ps1` — the hook itself; always `exit 0`, fire-and-forget
- `scripts/install-git-hooks.sh` / `scripts/install-git-hooks.ps1` — one-command install per repo
- `docs/reference/git-hooks.md` — installation guide

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

Set `MORI_URL`, `MORI_API_KEY`, `MORI_CLIENT` in your environment — see `docs/reference/git-hooks.md`.

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
bash heredoc commands; Windows devices retain PowerShell output.

**Usage after this release:**

```
/update my-linux-device all   → pasteable bash that deploys all 7 skills to 4 profile dirs
/update my-windows-device all  → pasteable PowerShell equivalent
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
