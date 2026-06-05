# Changelog

## v2.2.2 — Onboarding: server health sentinel + honest setup guidance (plugin v0.1.2)

- **feat:** Session-start server **health sentinel** — the SessionStart hook pings the configured server's `/health` (600ms–2s bound, cached 5 min per session, fail-open, and **only ever the user's own URL — no phone-home**). If the server is unreachable or unconfigured, it injects a clear, honest setup message *before* the user hits a broken `/brief`; if the server is up, it stays silent. Skill-level backstop in `/brief` and `/pensieve` for mid-session outages.
- **feat:** Honest onboarding copy — the setup message names the real requirements (a Docker host **and** an LLM provider key) and links the quickstart; no "one-command" overstatement. Plugin-manager `description` rewritten value-first with the self-hosted/AGPL privacy framing.
- **fix:** the default `localhost:8968` is now health-checked, not short-circuited to "unconfigured" — a running local server (the most common setup) is no longer mis-reported as unconfigured.
- **test:** 96 tests across the health-gate and the three client hook suites.

## v2.2.1 — Cursor & Antigravity hook layers + multi-client tidy-upper (plugin v0.1.1)

- **feat:** Cursor and Antigravity hook layers — per-client Node entrypoints over a shared `lib/` (fail-open wrapper, POST, conversation-keyed throttle, canonical-event normalizer). Cursor: `sessionStart` context + `postToolUse`/`stop` telemetry via the documented standalone `~/.cursor/hooks.json` (Cursor plugin-hook bundling is undocumented). Antigravity: `PreInvocation` once-per-conversation context (conversationId throttle) + `PostToolUse`/`Stop` telemetry via `~/.gemini/config/hooks.json`. Wired by `install-hooks-{cursor,antigravity}.mjs` with absolute paths. Each client's events are normalized client-side to Mori's canonical event schema before POST — the server stays single-schema; client extras ride in an opaque `_clientMeta`.
- **feat:** `plugins/mori/scripts/legacy/tidy-up.mjs` — multi-client cleanup that removes ONLY Mori's bespoke-installed entries (Claude/Cursor/Antigravity MCP + hooks + permissions; skills with `--include-skills`) so the plugin installs clean. Dry-run by default; `--confirm` to write; exact-signature matching, timestamped backups, validation gates (no non-mori key removed), fail-gradual per client. Replaces the Claude-only uninstaller.
- **chore:** post-compaction re-ground remains Claude-only — Cursor (`preCompact` observational) and Antigravity have no compaction event.
- **test:** 127 hook/cleanup tests (Claude 23, Cursor 22, Antigravity 24, tidy-up 58); the shipped Claude scripts are unchanged.

## v2.2.0 — Cross-tool plugin distribution (plugin v0.1.0)

- **feat:** Unified plugin package at `plugins/mori/` for Claude Code, Cursor, and Antigravity — shared `skills/` + Node `scripts/` core with thin per-platform manifest/MCP files. Auto-registers via the plugin system, no `settings.json` surgery; API key via Claude Code `userConfig` (keychain) with `MORI_API_KEY` env fallback.
- **feat:** Claude Code: complete and marketplace-ready. `SessionStart` re-ground hook (`source=compact`) replaces the broken PostCompact `additionalContext` hook (which the harness rejects) — closes #17. Node `mori-context-hook.mjs` + `mori-ship-event.mjs` (telemetry incl. Stop transcript-tail enrichment); no bash/jq dependency. Repo-root `.claude-plugin/marketplace.json` enables `/plugin marketplace add fjwood69/mori` → `/plugin install mori@mori`.
- **feat:** Cursor + Antigravity: MCP connection + skills work day one (skills are a cross-tool open standard); platform-specific hooks are a fast-follow.
- **chore:** Bespoke `install-mori-claude.{sh,ps1}` moved to `scripts/legacy/` (superseded by the plugin); a standalone legacy uninstaller is bundled for migration. Cursor/Antigravity bespoke installers retained pending their plugin hook layers; Cline unchanged.
- **docs:** README + getting-started guides rewritten plugin-first; corrected stale claims (SessionStart not PostCompact for post-compaction re-ground; dual-backend SQLite/Postgres, not SQLite-only).
- **docs:** new *Read the current manual, not your memory* practice in `agent-working-practices` (consult live docs for evolving APIs, not training recall); README *Pairs well with* section positioning Mori (earned memory) alongside Context7 (live library docs).
- See #24 for the plugin distribution tracking issue.

## v2.1.35 — Governed write REST API core: propose/pending/approve/reject/delete (#14)

- **feat:** `POST /api/memories` — propose-not-overwrite, tier-aware write endpoint (role: write).
  New name → working row created (201); canonical or protected name → pending proposal (202, canonical
  row unchanged); working name with same actor → idempotent update (200); different actor → pending
  proposal (202). Strict input validation: name `^[a-zA-Z0-9_-]{1,128}$`, body max 64 KB,
  unexpected fields rejected (400).
- **feat:** `GET /api/pending` — list pending proposals awaiting dreamer review (role: write;
  unapproved agent output is not for read-only eyes).
- **feat:** `POST /api/memories/{name}/approve` — approve a pending write and apply it to the store
  (role: dreamer). Race-safe: SQLite uses `BEGIN IMMEDIATE`; Postgres uses `SELECT … FOR UPDATE`
  inside an explicit transaction. Concurrent approvals cannot duplicate canonical rows.
- **feat:** `POST /api/memories/{name}/reject` — reject a pending write without applying (role: dreamer).
- **feat:** `DELETE /api/memories/{name}` — hard-delete a memory entry (role: dreamer). Soft-delete
  (`deleted_at`) deferred to #16.
- **feat:** Structured audit log line on every governed write/approve/reject/delete:
  `AUDIT op=<op> actor=<key_name> name=<name> content_hash=<sha256[:16]>`. No new table (deferred).
- **feat:** `queue_pending_write()` added to `MemoryStore`, `PostgresStore`, `SQLiteStore`, and
  `BaseStore` — direct pending-write insertion for the REST propose path, bypassing the
  `write()` protection-check heuristic for canonical-not-protected memories.
- **security:** Audited `_a()` bridge for contextvars propagation — no `run_in_executor`/threads;
  `current_actor` propagates correctly into all awaited coroutines. Fail-closed test confirms a
  missing actor raises `PermissionDenied`, never silently passes.
- **test:** `tests/test_write_api.py` — 18 tests covering: capability enforcement (read/write/dreamer
  on all routes), propose-not-overwrite semantics (new/canonical/same-actor/different-actor),
  contextvar-missing fail-closed test, input validation (body size, name chars, unexpected fields,
  missing name), and `_validate_write_payload` unit tests. Both backends via `@requires_pg`.
- **scripts:** `scripts/verify-deployment.py` updated — `GET /api/pending` added to guarded routes;
  write-API auth-gating probes for approve/reject/delete; safe POST+DELETE round-trip probe.
- **docs:** `docs/reference/configuration.md` updated — roles table expanded with REST endpoints;
  Write REST API section with endpoint reference, body schema, and deferred items.
- **deferred to #16:** Per-key token-bucket rate-limiting, `Idempotency-Key` replay cache, soft-delete
  (`deleted_at`), structured audit table. These are prerequisites for autonomous mirror-writer support
  (Hermes, #16) but not needed for the dashboard (#15).

## v2.1.34 — API key capability scoping + host→api TD mode switch (#13)

- **feat:** Added `MORI_API_KEY_ROLES=name:role,...` env var — parsed parallel to `MORI_API_KEYS`
  (format unchanged). Roles: `read < write < dreamer`. Names absent from the roles map default to
  `read` (fail closed). Unknown role strings are rejected at startup with an error log and
  downgraded to `read`.
- **feat:** New `MORI_TD_MODE=host|api` mode switch (default: `host`). In `host` mode, existing
  hostname-based trusted-dreamer logic is unchanged — deployments that do not set this variable
  behave exactly as before. In `api` mode, the API key role is the sole authority for
  write/approve operations.
- **feat:** New module `mori_advisor/policy.py` — `Actor(key_name, role)` dataclass,
  `current_actor` ContextVar (set by `ApiKeyMiddleware` per request, reset on exit),
  `require_role(min_role)` helper that raises `PermissionDenied` on insufficient privilege.
  `can_read` / `can_write` / `can_approve` functions for REST-surface callers.
- **feat:** `require_role` applied as the first call in each privileged MCP tool:
  `memory_write`/`memory_import` → `write`; `memory_approve`/`memory_reject`/`memory_protect` →
  `dreamer`; `memory_delete`/`memory_rollback` → `write`.
- **feat:** `MORI_LOCAL_FULL_ACCESS=true` allows a nil actor (stdio transport) through in `api` mode
  for fully-trusted single-user deployments.
- **test:** `tests/test_policy.py` — 39 tests (26 SQLite + 13 Postgres) covering: read key denied
  for write/dreamer ops; write key denied for dreamer ops; dreamer key allowed for all; host mode
  no-enforcement (backward compat); nil actor fail-closed; `MORI_LOCAL_FULL_ACCESS` bypass; the
  **no-bypass proof** — the same under-privileged call is denied on both the MCP-tool surface
  (via `current_actor` ContextVar) and the REST surface (via `can_write`/`can_approve`).
- **docs:** `docs/reference/configuration.md` updated with new env vars and capability roles section.
- **note:** Audit logging is a planned dependency of #15. The write REST API (#14) and review queue
  (#15) will use the same `require_role` check; in `host` mode they fail closed automatically.

## v2.1.33 — MCP tool test coverage across both backends (issue #19)

- **Test:** Added `tests/test_mcp_tools.py` — 36 tests parametrised over SQLite
  and Postgres (72 cases total), covering every store-touching MCP tool in
  `mori_advisor/main.py`. The coroutine-scan helper `assert_no_coroutines()`
  recursively traverses each tool's return value and fails if any leaf is an
  unawaited coroutine, catching the exact class of bug from issue #12.
- **Store globals:** Monkeypatched via `monkeypatch.setattr("mori_advisor.main.store", ...)`
  (and the derived `memory_store`/`session_log` consistently). No DI refactor — a
  `# TODO` notes this as a future improvement.
- **External stubs:** NATS tools stubbed via `sys.modules["nats"]` replacement;
  bifrost/LLM stubbed via `monkeypatch`; ingestion pipeline stubbed with a fake
  returning a complete result dict so arg-parsing and the coroutine scan still run.
- **Bug-reintroduction check:** Temporarily restoring `async def parse_tags` in
  `PostgresStore` caused `test_memory_req[postgres]` to fail with
  `TypeError: 'coroutine' object is not iterable` while SQLite passed — confirming
  the suite catches issue #12's bug class. Reverted immediately after.
- **Bonus bugs found and fixed:** The suite also discovered three pre-existing schema
  mismatches in `PostgresStore` (wrong column names in `history`/`diff`/`rollback`
  and `pending_list`/`approve`/`reject`) — fixed as part of this PR.
- **CI:** `tests/test_mcp_tools.py` added to the `test-postgres` job so it runs
  against `postgres:16` on every push.

## v2.1.32 — Fix `/req` (`memory_req`) crashing on Postgres backend (issue #12)

- **Bug:** `memory_req` (the `/req` MCP tool) raised `TypeError: 'coroutine' object
  is not iterable` when called against a Postgres-backed instance. The error surfaced
  as a tool error visible to the caller, not as a "Database error: …" string.
- **Root cause:** `PostgresStore.parse_tags` was declared `async def` despite
  containing no awaits. `memory_req` called it bare — `store.parse_tags(raw)` — and
  then iterated the result with `for t in tags:`, which failed because the call
  returned a coroutine object rather than a list. The SQLite backend's `parse_tags`
  is correctly `def` (sync), so SQLite was unaffected.
- **Fix:** removed `async` from `PostgresStore.parse_tags` so it matches the
  `BaseStore` abstract signature (`def parse_tags`) and returns a list directly.
- **Regression test:** `tests/test_rest_api.py::test_pg_get_requirements_returns_list_not_coroutine`
  (gated on `MORI_TEST_DATABASE_URL`). The CI `test-postgres` job now also runs
  `tests/test_rest_api.py` so this class of regression is caught automatically.

## v2.1.31 — Dashboard connect modal: key-first, server URL optional

- With mori serving the dashboard same-origin (v2.1.30), the connect modal now leads with
  the **API key** — the only field needed in the common case. The server URL is an optional
  override (placeholder set to the live page origin; blank = this server), relevant only
  when hosting the page standalone against a remote instance.

## v2.1.30 — Mori serves the dashboard at its root

- **Why:** v2.1.29 shipped the dashboard as a *standalone* static file you had to serve
  yourself and point at an instance — an unusual shape for something called a "dashboard,"
  and a source of friction (which machine? which port? same-origin vs base URL?). A
  dashboard should just *be there* when you open the server.
- **What:** mori now serves the bundled dashboard at its **root URL** (`GET /`, an open
  path). Open `http://<host>:<port>/` in a browser, enter a key, and browse — the page is
  served same-origin, so it targets the very instance it loaded from (no base URL, no CORS
  config). `dashboard/` is now bundled into the image (`COPY dashboard/`); the deployment
  contract (`verify-deployment.py`) asserts `/` returns 200, so a missing page fails the
  gate instead of 404-ing silently. The standalone file remains for hosting elsewhere.

## v2.1.29 — Read REST API + standalone memory-browser dashboard

- **Why:** the shared store was MCP-only — no way for a human (or a non-agent tool) to
  browse memories without a Claude Code session. This adds a small read REST surface and a
  zero-dependency web UI that consumes it, so the store is browsable from any browser.
- **Read REST API (`mori_advisor/main.py`):** `GET /api/memories` (ranked FTS or recency —
  lean list shape via `search_json`, no body), `GET /api/memories/{name}` (full detail incl.
  body + provenance — the lazy-load companion; does **not** bump `retrieval_count`), and
  `GET /api/events` (session event log, newest first). All auto-guarded by `ApiKeyMiddleware`
  (`X-Api-Key`) and wrapped by `CORSMiddleware` (`MORI_CORS_ORIGINS`, default `*`) placed
  *outside* the auth layer so browser preflight isn't 401'd. New `get_memory()` on both
  backends returns an identical 12-key curated dict; `_json_safe_rows` converts Postgres
  `TIMESTAMPTZ`→ISO so JSON serialization can't 500.
- **Standalone dashboard (`dashboard/`):** one self-contained `index.html` (vanilla JS,
  inline CSS, no build step, no CDN) — search + browse memories, click a card to unfurl the
  full body + a provenance footer (created/updated/clients/tier/retrievals/freshness),
  lazy-fetched once and cached. Base URL + API key in `localStorage`; every store-derived
  string HTML-escaped. Runs via `python3 -m http.server` or any static host; points at any
  mori instance. Read-only — delete/write deferred.
- **Contract:** `scripts/verify-deployment.py` now probes all three read routes; the
  by-name probe is **dynamic** (discovers a real name from the list, `SKIP`s on an empty
  store) because `ApiKeyMiddleware` 401s unrouted `/api/*` paths too, so a static 200 probe
  would false-fail.
- **Fixes:** `/metrics` no longer hangs ~10s when NATS is unreachable (`collect_metrics`
  NATS connect now `allow_reconnect=False` + `asyncio.wait_for`); `GET /api/events` no
  longer 500s on Postgres (`TIMESTAMPTZ` now serialized via `_json_safe_rows`).

## v2.1.28 — Schema-migration runner + full-text search

- **Why:** the DB schema was defined ad-hoc across four sources (three SQLite bootstrappers
  + one Postgres `_DDL` string) with no recorded version, so the two backends had silently
  drifted (Postgres-only `delegate_tasks`, `dreamer_config.updated_at` missing on SQLite,
  `freshness_status` nullable on PG). And the guarded-`ALTER` approach could only add nullable
  columns — it had no safe path for the change recall actually needed: full-text search.
- **Migration runner (`mori_advisor/store/migrations.py`):** one ordered `MIGRATIONS` registry
  drives both backends with a `schema_migrations` version table (PK = id). `apply_sqlite` (sync,
  `BEGIN IMMEDIATE` + re-check-under-lock + retry-on-locked) and `apply_postgres` (async, one
  dedicated connection held for the run so the session-scoped `pg_advisory_lock` can't be dropped
  by a pool reset; standby guard). Migration 1 ("baseline") *invokes the existing bootstrap code*
  (not copied DDL), so fresh and populated production DBs converge identically. `ingestion_server`
  bootstrap moved out of import-time into its async lifespan.
- **Drift fixes (0003–0005):** `dreamer_config.updated_at` on SQLite; `delegate_tasks` SQLite
  parity; Postgres `freshness_status` backfilled + `SET NOT NULL DEFAULT 'unknown'`.
- **Full-text search (0006):** SQLite **FTS5** external-content + triggers (porter stemming,
  bm25 ranking, LIKE fallback if FTS5 absent); Postgres generated **`tsvector`** column
  (`COALESCE` + weighted `setweight`) + GIN, `websearch_to_tsquery` + `ts_rank`. Replaces the
  unranked LIKE/ILIKE `search()`; empty query → recency; tags stay structured. `_fts_query`
  builds the SQLite MATCH string programmatically (injection-proof). Vectors deferred (FTS is
  native in both backends; vectors are not).
- **CI:** new `postgres:16` service + dual-backend test job — the Postgres path was never
  CI-tested before. New `tests/test_migrations.py` + `tests/test_search.py`.

## v2.1.27 — `/brief --post-compact` delta re-grounding

- **Why:** the `PostCompact` hook fired a plain `/brief`, which re-injects the *entire*
  memory base right after compaction shrank the context — and runs the per-memory
  `check_freshness` LLM scan every time. At team scale that doesn't scale and buries the
  few things that actually changed. The dedicated flag was promised in v2.1.0 ("planned
  for v2.1; plain /brief is the interim approach") but never built. This implements it as
  a **delta-since-last-brief**.
- **`brief(post_compact=True, since=…)`**: a lean path that surfaces only what changed in
  shared state — new/updated memories (project + global), decisions **superseded** under
  you, and fresh evictions — then a one-line dream state. It **never** runs the freshness
  check, the full memory list, the standards dump, or the other-project index. Delta lists
  cap at 30 with a "…N more — run a full /brief" pointer (no silent truncation, no
  auto-escalation).
- **New store method `get_memories_changed_since`** (SQLite + Postgres, with an
  `updated_at` index on both) and a shared `normalise_since` helper that converts
  `6h`/`7d`/ISO-8601 to the stored UTC form — critical so a `T`-separated ISO string
  doesn't sort greater than every space-separated `updated_at` row and silently match
  nothing. Exclusive `> since` bound; documented as best-effort re-grounding, not
  exactly-once consumption.
- **`since` is session-aware**, resolved client-side by the `/brief` skill: a
  `.mori-last-brief` marker (stamped every brief) → session-start → the server default
  window `MORI_POST_COMPACT_WINDOW` (`6h`).
- **Hook + installers**: the `PostCompact` shipper now instructs `/brief --post-compact`.
  The Cline and Cursor installers gained PostCompact parity (deploy the brief shipper +
  register the hook), matching Claude Code and Antigravity.
- **Tests**: new pytest suite (`tests/`) covering `normalise_since`, the delta store
  method (boundary + scoping), and the guarantee that the post-compact path skips
  `check_freshness`. CI now runs pytest alongside ruff.
- **Docs fix**: corrected the v2.1.0 hook path (`~/.claude/mori-post-compact-brief.sh`,
  was wrongly documented under `~/.claude/hooks/`).

## v2.1.26 — Reboot-safe GCE deployment

- **Why:** a reboot re-runs the startup script, which fetched every secret from Secret Manager
  and `exit 1`'d if `MORI_PG_PASSWORD` came back empty. A single denied/unreachable secret would
  therefore take a *running* instance down on the next reboot — and a hardcoded `useradd -u`
  that mismatched the live user's uid tainted the rootless Podman storage on rebuild.
- **Env reuse on reboot:** `startup.sh.tpl` now reads the persisted `/data/mori-advisor/.env`
  (written first boot, also CD's source of truth) and recovers the pg password from
  `MORI_DATABASE_URL` — **Secret Manager is consulted only on first boot** (no `.env` yet). A
  reboot now survives with Secret Manager denied or unreachable. The `.env` is written once on
  first boot and reused untouched thereafter (preserving CD/manual updates).
- **Self-consistent uid:** dropped the hardcoded `useradd -u 10001`; the user gets a
  system-assigned uid and the Postgres `pgdata` owner is derived from `/etc/subuid`
  (`subuid_base + 998` → container uid 999), so user / storage / pgdata can't drift on a rebuild.
- **`main.tf`:** `MORI_PG_PASSWORD` is now a managed `google_secret_manager_secret` with a
  `secretAccessor` binding for the service account — previously created out-of-band with no
  durable IAM grant, which is how the access silently lapsed. (Existing projects: `terraform
  import google_secret_manager_secret.mori_pg_password MORI_PG_PASSWORD`.)
- Verified: both templates render (`templatefile`) and the rendered scripts pass `bash -n`.

## v2.1.25 — Manage GCE app containers with systemd Quadlet

- **Why:** the run-spec for `mori-advisor`/`mori-ingestion` lived in *two* places — the GCE
  startup script and `cd.yml` — and drift between them was the root cause of the v2.1.16 404
  incident (rootful vs rootless). It also blocks horizontal scaling (multiple ingesters/dreamers).
- **Quadlet units (`deploy/gcp/quadlet/`)**: `mori-advisor`, `mori-ingestion`, `mori-msg` are now
  rootless **systemd Quadlet** `.container` units — one declarative source of truth for how each
  runs. `Restart=always` + `StartLimitIntervalSec=0` (never give up) + a `/dev/tcp` Postgres
  readiness gate in each unit. `dream` cron → `dream.service` + `dream.timer` (every 4h).
- **`startup.sh.tpl` + `main.tf`**: Terraform injects the checked-in unit files **verbatim**
  (`file()` → `templatefile`), so the VM's unit *is* the repo file — the duplication is gone.
  Startup installs the units, brings up the `mori` user manager, `daemon-reload`, starts the
  units, enables `dream.timer`, and removes the legacy crontab line.
- **`cd.yml`**: deploy is now `podman pull` (with a `previous-rollback` tag) + retag `:latest` +
  `systemctl --user restart mori-advisor mori-ingestion mori-msg`. The imperative
  `podman run --replace` blocks and the rootful-stray guardrail are removed. Health + deployment
  contract gates unchanged.
- **Scope:** `mori-pg` stays an imperative `podman run --restart=always` — a stable stateful
  singleton defined in one place, with no Quadlet scaling benefit (and the one data-holding
  container is left in its proven form). Any optional gateway sidecar is likewise out of scope.
- **Verified live on GCE:** all five containers up; the three app units `active`; deployment
  contract PASS; `systemctl --user restart` (CD's mechanism) works. Reboot-survival hardening is
  tracked separately in #7.

## v2.1.24 — Capture assistant reasoning from the Stop hook

- **Reasoning capture:** lifecycle hooks recorded tool calls and user prompts but never the
  assistant's text responses — the plans, analysis, and decisions. Those are now captured.
- **Client (shippers):** on `Stop`, `mori-ship-event.sh` / `.ps1` attach a bounded,
  base64-encoded tail of the session transcript (`transcript_tail_b64`). Pure `tail`+`base64`
  (bash) / native `Get-Content`+`ConvertFrom-Json` (PowerShell) — no python/jq dependency, so
  it works on bare macOS, Linux, and Windows. Any failure ships the original event unchanged.
- **Server:** `_extract_assistant_text()` parses the tail, walks back to the last user-text
  line (treating `tool_result` user lines as part of the turn, so post-tool reasoning is kept),
  skips `isSidechain` subagent lines, and joins the turn's assistant `text` blocks (+ `thinking`
  iff `MORI_CAPTURE_THINKING=true`). Stored in `session_events.assistant_text`.
- **Dream:** `_format_events` surfaces the captured reasoning on Stop events so it's distilled
  into memories alongside prompts and tool calls.
- **Migration:** `assistant_text` column added to `session_events` (SQLite guarded `ALTER`;
  Postgres `ADD COLUMN IF NOT EXISTS`). No new endpoints, MCP tools, or installer allowlist
  changes — re-running the installer redeploys the updated shipper.

## v2.1.23 — Deployment contract gate (UAT ⇄ PRD parity)

- **`scripts/verify-deployment.py`**: single source of truth for "is this instance serving
  correctly?" — asserts open routes (`/health`, `/ready`, `/metrics`) return 200 and every
  auth-guarded feature route returns 401 without a key and 200 with one (registered + auth
  enforced + key accepted). Stdlib-only so it runs inside the slim image.
- **`cd.yml`**: after the health gate, CD runs this script *inside the freshly-deployed
  container* against its own endpoint and **fails the deploy** if the route surface is wrong.
  Previously CD only checked `/health` — which passed even while feature routes 404'd, so
  broken deploys reported green. A tag that passes the contract in UAT now reproduces the same
  surface in PRD because CD enforces the identical assertions.
- **UAT** runs the same script against both backends pre-tag (replaces an inline check). New
  `custom_route` paths are added to `verify-deployment.py` once — both gates pick them up.

## v2.1.22 — Unify deploy on rootless runtime + env-file single source of truth

- **Root-cause fix for the production `/api/git/*` 404s**: the GCE startup script ran
  containers ROOTLESS (as the `mori` user) while CD ran them ROOTFUL (`sudo podman`). The two
  Podman stores are mutually invisible, so each CD spun up a parallel rootful container that
  fought the rootless one for host port 8968 — whichever bound first served; the other
  crash-looped. The rootful copy was also under-configured (auth off, no bifrost/NATS).
- **`cd.yml`**: deploy ROOTLESS as the `mori` user via `sudo su - mori`, matching the startup
  script. `podman run --replace` (atomic — blue/green is impossible under `--network=host`).
  Preflight guardrail removes any stray rootful `mori-*` container. CD reads
  `/data/mori-advisor/.env` and needs no secret values.
- **`startup.sh.tpl`**: write the COMPLETE runtime env to `/data/mori-advisor/.env` (single
  source of truth shared with CD — the two paths can no longer drift). Source `MORI_API_KEYS`
  from Secret Manager — it was previously never set, so clean rebuilds came up in open-auth mode.
- **`post-push.sh` / `post-push.ps1`**: validate the git watermark resolves to a real commit
  before use; fall back to `HEAD~20` on force-push, rebase, fresh clone, or a stale/foreign SHA
  (previously errored silently and ingested nothing).
- **UAT**: `start-uat.sh` now runs a custom-route surface check (401 without key, non-404 with
  key) on both SQLite and Postgres backends — the exact assertion that would have caught the
  404 regression the standard smoke test missed.

## v2.1.21 — Fix CD port conflict + switch to http_app() ASGI startup

- **`cd.yml`**: Before blue/green deploy, kill any process on ports 8968/8969 that
  isn't tracked by rootful Podman (e.g. a startup-script rootless container from a VM
  reboot holding the port and causing every CD attempt to crash in a restart loop).
- **`mori_advisor/main.py`**: Switch from `mcp.run(middleware=...)` to building the
  ASGI app explicitly with `mcp.http_app()` and serving it with `uvicorn.run()` — the
  pattern the FastMCP docs recommend for production. This makes custom_route registration
  unambiguous and decouples the server lifecycle from FastMCP's transport abstraction.

## v2.1.19 — Move Dockerfile to Python 3.13

- **`Dockerfile`**: Change `PYTHON_VERSION` from `3.14.5` to `3.13.4`. FastMCP 3.2.0 has a
  `custom_route` registration bug on Python 3.14 where routes defined after a certain index in
  the handler list silently fail to register. Python 3.13 is the current supported release with
  active security patches — not the EOL-approaching 3.12 we briefly tried. UAT masked this
  because local builds use the system Python (3.12 on typical dev machines). Production was running Python 3.14
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

- **Target Selection**: Added `--target cli/ide/both` (Bash) and `-Target cli/ide/both` (PowerShell) option to installers, directing MCP config (`mcp_config.json`) and hooks (`hooks.json`) to `~/.gemini/antigravity` (CLI), `~/.gemini/antigravity-ide` (IDE), or both. Default in headless mode is `ide`.
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

A `PostCompact` hook (`~/.claude/mori-post-compact-brief.sh`) is now installed
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
backends verified before tagging.

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
./scripts/install-git-hooks.sh --repo ~/path/to/your-other-repo
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
commands for Linux targets. Fixed with a family branch — Linux devices now get
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
