![Mori Roadmap](https://raw.githubusercontent.com/fjwood69/mori/07780a6477fd5a2dd0ad693ed3ad237c30a8bda4/docs/assets/roadmap-banner.svg)

This file tracks what has shipped, what is in progress, and what is planned.
Updated with each release.

---

## Shipped

### v1.0.0
- Dream pipeline — event capture, LLM distillation, memory write, watermark advance
- PreCompact hook — synchronous dream run before context compression
- Three-tier memory lifecycle — ephemeral, working, canonical
- Freshness check on fast VK
- NATS cross-device messaging — publish session summaries, replay last 7 days
- Git push NATS notification
- AGPL-3.0 licence + commercial licence option (`COMMERCIAL.md`)
- GCP Terraform deployment — e2-small, ~$12/month
- Docker Compose homelab deployment — Linux, macOS, Windows
- Slash commands — `/brief`, `/dream`, `/consult`, `/pensieve`, `/req`, `/nats`, `/update`, `/wrap`
- Installer scripts — Claude Code, Cursor, Antigravity, Cline (Linux, macOS, Windows)
- OTel metrics — `/health`, `/ready`, `/metrics`

### v1.1.0
- `mori-msg` — inter-agent messaging via NATS JetStream
  - Seven-field typed message schema (`task`, `decision`, `question`, `reply`, `ack`, `done`, `broadcast`)
  - MCP tools: `msg_send`, `msg_recv`, `msg_thread`
  - `mori-msg` daemon pod — continuous NATS listener, processes messages server-side without a human session
  - `decision` messages written directly to memory store on receipt
  - `/brief` surfaces pending messages at session start
  - `/wrap` broadcasts session summaries to `mori.msg.broadcast`
  - Headless CC support — opt-in via `MORI_MSG_HEADLESS_ENABLED`
- Installer allow lists updated for new MCP tools
- Documentation pass — `/msg` slash command reference, env vars, for-teams guide
- moriapp.dev landing page — Cloudflare Pages, static, no build step

### v2.0.0
- Dual-backend store layer — SQLite (solo) or PostgreSQL (team), selected via `MORI_DATABASE_URL`
  - `BaseStore` ABC, `SQLiteStore` delegation wrapper, `PostgresStore` (asyncpg pool)
  - `get_store()` factory — zero breaking change for existing SQLite deployments
  - `mori export` / `mori import` CLI tools — SQLite → Postgres migration, idempotent
  - pgBouncer in session mode (asyncpg prepared statement compatibility)
  - Streaming replica on local host — `mori-pg-replica` on port 5435, lag=0
- WAL-G replacing Litestream — daily pg_dump to GCS, 14-day lifecycle policy
  - GCS metadata server auth — no credentials in env vars
  - RPO/RTO explicitly defined and tested
- `deploy/solo/` — SQLite + Litestream sidecar (replaces `deploy/homelab/`)
- `deploy/team/` — Postgres + pgBouncer + WAL-G sidecar

### v2.0.1
- `pending_writes` DDL fix — allow NULL `reviewed_by` in Postgres (pre-existing SQLite data quality gap)
- asyncpg dependency uncommented in `requirements.txt`
- Backup script updated — `pg_dump` path for Postgres backend, shell retention removed
- Streaming replication documented in `docs/reference/team-configuration.md`

### v2.2.0
- **Cross-tool plugin distribution** — unified `plugins/mori/` package for Claude Code (complete + marketplace-ready), Cursor, and Antigravity; `SessionStart` re-ground hook replacing PostCompact additionalContext (closes #17); bespoke Claude installer moved to `scripts/legacy/`; see #24

### v2.2.1
- **Cursor & Antigravity hook layers** — per-client Node hooks over a shared `lib/` (canonical-event normalizer, fail-open, conversation-keyed throttle); installed via the documented standalone hooks configs; client events normalized to Mori's schema before POST. **Multi-client tidy-upper** (`scripts/legacy/tidy-up.mjs`) — dry-run-default cleanup of bespoke installs across all three clients, exact-signature matching with backups. Plugin v0.1.1.

---

## v2.0 — In progress

Core hardening. Remaining items before v2.0 is complete. Target: one shared instance, 2–10 devs, zero ops overhead.

- PostgreSQL migration
  - Column encryption via GCP KMS envelope encryption — no keys in env vars
  - TLS on all Postgres connections
- WAL-G — continuous GCS backup, explicit RPO < 5min / RTO < 30min
- REST API
  - `GET /api/memories?query=...` — ✅ shipped v2.1.29 (+ `GET /api/memories/{name}`)
  - `POST /api/memories` — write path, deferred
  - `GET /api/events` — ✅ shipped v2.1.29
  - Webhook support — push notifications on significant memory writes
  - OpenAPI spec
  - Foundation for dashboard and third-party integrations — ✅ dashboard shipped v2.1.29
- API rate limiting — per-key limits
- Headless CC cost guards — per-message spend caps

---

## v2.1 — In progress

Small team coherence. Requires v2.0 foundation.

**Shipped:**
- v2.1.0: Named API key authentication — `MORI_API_KEYS=name:secret,...`, ASGI middleware covering all MCP tools and HTTP endpoints, backward compat with `MORI_ADVISOR_API_KEY`
- v2.1.0: PostCompact re-grounding hook — `~/.claude/mori-post-compact-brief.sh`, opt-out via `MORI_POST_COMPACT_BRIEF=false`
- v2.1.6: Postgres dream transaction poisoning fix — `INSERT … ON CONFLICT DO UPDATE` upsert; origin array merging, canonical tier and protection flag preservation on update
- v2.1.8: Async Postgres ingestion pipeline; MCP endpoint now requires API key (removed from `OPEN_PATHS`)
- v2.1.9: Postgres `brief()` interface fixes — `get_memories_by_project()` and `check_freshness()` corrected for asyncpg
- v2.1.10: Antigravity installer profile parity (`--target cli/ide/both`); PostCompact hook for Antigravity; robust YAML frontmatter parsing in PowerShell skill installer
- v2.1.11: Postgres savepoint isolation in dream pipeline; asyncpg datetime fix; `APP_PORT` configurable via env var; smoke-test robustness improvements
- v2.1.12: Session-based auth bypass for SSE POST/DELETE — fixes 401 on IDE restart for clients that don't propagate custom headers
- v2.1.13: Native Prometheus `/metrics` exposition — `text/plain; version=0.0.4`, direct scraping without OTel bridge; pluggable store count and filter methods
- v2.1.14: Windows installer PostToolUse `matcher` field fix; session auth bypass improvement
- v2.1.15: Postgres-first GCP deployment — persistent-disk bind mount, `pg_isready` gate, `MORI_REQUIRE_POSTGRES`, pg_dump backup cron, Tailscale and SSH host key persistence across VM rebuilds
- v2.1.16–v2.1.19: Git commit ingestion + consult capture — `POST /api/git/ingest`, `GET /api/git/watermark`, `MORI_CONSULT_CAPTURE`; pinned FastMCP==3.2.0 + Python 3.12 to fix `custom_route` silent failure on Python 3.14
- v2.1.20–v2.1.23: Deploy unified on rootless `--env-file --replace`; deployment contract gate (`scripts/verify-deployment.py`) shared by UAT + CD
- v2.1.24: Assistant reasoning capture — Stop hook ships a bounded transcript tail; server extracts the turn's assistant text into `session_events.assistant_text`; dream distills it
- v2.1.25: GCE app containers (`mori-advisor`/`ingestion`/`msg`) managed by rootless **systemd Quadlet** — one declarative source of truth (units injected verbatim by Terraform), `dream` cron → `dream.timer`, CD switched to `podman pull` + `systemctl --user restart`; `mori-pg` stays imperative. Lays the substrate for horizontal worker scaling (template units)
- v2.1.26: Reboot-safe GCE deployment — startup reuses the persisted `/data/mori-advisor/.env` on reboot (Secret Manager consulted only on first boot, so a denied/unreachable secret can't take a running instance down); system-assigned mori uid with `pgdata` ownership derived from `/etc/subuid`; `MORI_PG_PASSWORD` now a Terraform-managed secret with a durable `secretAccessor` grant
- v2.1.27: `/brief --post-compact` delta re-grounding — `brief(post_compact=True, since=…)` surfaces only changed/superseded/evicted memories since the last brief, skipping the full base + standards + freshness scan; `get_memories_changed_since` (SQLite + Postgres, `updated_at` index); session-aware `since` (`.mori-last-brief` marker → session start → `MORI_POST_COMPACT_WINDOW`); Cline + Cursor installer parity; pytest suite + CI
- v2.1.28: Schema-migration runner + full-text search — one ordered `MIGRATIONS` registry with a `schema_migrations` version table drives both backends (single source of truth; baseline invokes the existing bootstrap); drift fixes bring SQLite/Postgres back into parity; ranked FTS replaces unranked LIKE/ILIKE (SQLite **FTS5** + triggers, Postgres generated **`tsvector`** + GIN); Postgres now exercised in CI via a service container. Vectors deferred (FTS is symmetric across both backends)
- v2.1.29: Read REST API + standalone web dashboard — `GET /api/memories` (ranked FTS/recency), `GET /api/memories/{name}` (full detail + provenance), `GET /api/events`; `CORSMiddleware` (`MORI_CORS_ORIGINS`) for cross-origin browser access; a dependency-free static `dashboard/` to search, browse, and unfurl memories. Read-only — authenticated by the same `MORI_API_KEYS`. Delete/write + OpenAPI + rate-limiting deferred
- v2.1.30: mori serves the dashboard at its root URL (`GET /`, bundled into the image; the deployment contract asserts `/` returns 200 so a missing page fails the gate)
- v2.1.31: dashboard connect modal — API key first, server URL an optional override (placeholder = the live page origin)
- v2.1.32: fix `/req` (`memory_req`) crashing on Postgres — `PostgresStore.parse_tags` was `async` with no awaits, so the result was an un-awaited coroutine
- v2.1.33: dual-backend **MCP-tool test suite** — exercises every MCP tool on SQLite *and* Postgres (the gap that let the #12 crash ship); caught + fixed five further Postgres-only crashes (history/diff/rollback/pending/approve/reject column mismatches)
- v2.1.34: **API key capability scoping** — `read`/`write`/`dreamer` roles (`MORI_API_KEY_ROLES`) + `MORI_TD_MODE` host→api trusted-dreamer switch; a unified `Policy` + `ContextVar` enforces identically on the REST and MCP surfaces (no bypass). Foundation for the governed write API + review queue

**Remaining:**

| Priority | Feature |
|----------|---------|
| P0 | **Multi-project `/brief`** — `--project api --project frontend` — small teams wear many hats |
| P0 | **Demo video** — ships the week v2.1 does. Unblocks Product Hunt and HN Show HN. |
| P1 | **URL ingestion** — bootstrap context from docs, RFCs, public pages without copy-paste |
| P1 | **GitHub inbound ingestion** — issues, PR comments, commit history → memory context |
| P1 | **Path-aware memory surfacing** — memories tagged to file/directory paths surface in `/brief` when working in that context |
| P1 | **Streaming SSE progress** on ingest |

---

## v2.2 — Workflow fit

Target: fits into existing small-team workflows. Requires v2.1.

| Priority | Feature |
|----------|---------|
| P0 | **Slack inbound ingestion** — `/ingest --source #dev-channel` (read-only, no write-back) |
| P0 | **File upload via dashboard / API** — drag-and-drop PDFs, images |
| P1 | **Linear / GitHub Issues connector** — inbound only, issues → `/req` |
| P1 | **`/reflect` command** — on-demand targeted dream |
| P1 | **Improved installer UX** — one-liner `curl | bash` with interactive provider selection |

---

## v3.0 — Enterprise platform

Built for organisations with compliance requirements, multiple teams, and production scale.

- Memory namespacing + COIN identity scoping + row-level security
- SSO / SAML / SCIM / LDAP
- Admin dashboard — user management, audit log, governance UI
- Multi-tenant isolation
- Distributed dream pipeline
  - Phase 1 (fast VK): cluster events by semantic proximity, assign non-overlapping slices
  - Phase 2 (parallel dream): stateless dream pods, each claims a cluster from `dream_jobs` table
  - Phase 3 (reconcile): `mori-reconcile` pod resolves cross-cluster dependencies
  - Scale by adding pods — Postgres coordinates via `dream_jobs`
- Memory poisoning guardrails — Lakera / NeMo on dream pipeline LLM calls
- Advanced K8s operator — HA, rolling updates, federation
- Bidirectional project intelligence connectors — JIRA, Azure DevOps, ServiceNow write-back
- Advanced analytics — usage per namespace, dream efficiency per team

---

## Experiments / Research

- Memory merge strategy across independent stores
- Bifrost composite routing metric — throughput-weighted VK scoring
- Bifrost custom provider pricing accuracy
- `/reflect` as a first-class on-demand dream operation (may promote to v2.2)

---

## Open core model

Mori follows an open core model. The core engine is and will remain open-source
under AGPL-3.0. Enterprise-specific features are developed separately under a
commercial licence.

**Open (AGPL-3.0) — always:**
- Dream pipeline, event capture, memory distillation
- NATS messaging and mori-msg inter-agent layer
- REST API + OpenAPI spec
- JWT / API key auth
- Docker Compose and GCP Terraform deployment
- All slash commands and MCP tools
- Simple web dashboard (v2.1)
- All inbound ingestion connectors (v2.1/v2.2)

**Commercial — enterprise tier:**
- SSO / SAML / SCIM / LDAP
- Memory namespacing and COIN identity scoping
- Advanced compliance logging (SOC2, HIPAA audit trails)
- Bidirectional project intelligence connectors (JIRA write-back, Azure DevOps, ServiceNow)
- Multi-tenant isolation
- Enterprise support SLA
- Advanced K8s operator

Enterprise features are developed in a private `mori-enterprise` repository and
never appear in the public core. See `COMMERCIAL.md` for licensing terms.

---

## Not planned

- Mid-session push to active agent sessions — latency model is session-to-session by design
- Message encryption between agents — all agents share NATS credential by design; revisit when namespacing ships
- Multi-hop message routing — point-to-point and broadcast only
- Split-brain / cross-region merge logic in core — advanced self-hosting pattern, not a product feature

---

*Last updated: v2.1.16*
