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
  - NUC streaming replica via Tailscale — `mori-pg-replica` on port 5435, lag=0
- WAL-G replacing Litestream — daily pg_dump to GCS, 14-day lifecycle policy
  - GCS metadata server auth — no credentials in env vars
  - RPO/RTO explicitly defined and tested on NUC
- `deploy/solo/` — SQLite + Litestream sidecar (replaces `deploy/homelab/`)
- `deploy/team/` — Postgres + pgBouncer + WAL-G sidecar

### v2.0.1
- `pending_writes` DDL fix — allow NULL `reviewed_by` in Postgres (pre-existing SQLite data quality gap)
- asyncpg dependency uncommented in `requirements.txt`
- Backup script updated — `pg_dump` path for Postgres backend, shell retention removed
- Streaming replication documented in `docs/reference/team-configuration.md`

---

## v2.0 — In progress

Core hardening. Remaining items before v2.0 is complete. Target: one shared instance, 2–10 devs, zero ops overhead.

- PostgreSQL migration
  - Column encryption via GCP KMS envelope encryption — no keys in env vars
  - TLS on all Postgres connections
- WAL-G — continuous GCS backup, explicit RPO < 5min / RTO < 30min
- REST API
  - `GET /api/memories?query=...`
  - `POST /api/memories`
  - `GET /api/events`
  - Webhook support — push notifications on significant memory writes
  - OpenAPI spec
  - Foundation for dashboard and third-party integrations
- API rate limiting — per-key limits
- Headless CC cost guards — per-message spend caps

---

## v2.1 — In progress

Small team coherence. Requires v2.0 foundation.

**Shipped:**
- v2.1.0: Named API key authentication — `MORI_API_KEYS=name:secret,...`, ASGI middleware covering all MCP tools and HTTP endpoints, backward compat with `MORI_ADVISOR_API_KEY`
- v2.1.0: PostCompact re-grounding hook — `~/.claude/hooks/post-compact-brief.sh`, opt-out via `MORI_POST_COMPACT_BRIEF=false`
- v2.1.6: Postgres dream transaction poisoning fix — `INSERT … ON CONFLICT DO UPDATE` upsert; origin array merging, canonical tier and protection flag preservation on update
- v2.1.8: Async Postgres ingestion pipeline; MCP endpoint now requires API key (removed from `OPEN_PATHS`)
- v2.1.9: Postgres `brief()` interface fixes — `get_memories_by_project()` and `check_freshness()` corrected for asyncpg
- v2.1.10: Antigravity installer profile parity (`--target cli/ide/both`); PostCompact hook for Antigravity; robust YAML frontmatter parsing in PowerShell skill installer
- v2.1.11: Postgres savepoint isolation in dream pipeline; asyncpg datetime fix; `APP_PORT` configurable via env var; smoke-test robustness improvements
- v2.1.12: Session-based auth bypass for SSE POST/DELETE — fixes 401 on IDE restart for clients that don't propagate custom headers
- v2.1.13: Native Prometheus `/metrics` exposition — `text/plain; version=0.0.4`, direct scraping without OTel bridge; pluggable store count and filter methods
- v2.1.14: Windows installer PostToolUse `matcher` field fix; session auth bypass improvement
- v2.1.15: Postgres-first GCP deployment — persistent-disk bind mount, `pg_isready` gate, `MORI_REQUIRE_POSTGRES`, pg_dump backup cron, Tailscale and SSH host key persistence across VM rebuilds
- v2.1.16: Git commit ingestion — `POST /api/git/ingest`, `GET /api/git/watermark`, per-`(repo, ref)` watermarks; consult output auto-capture (`MORI_CONSULT_CAPTURE`); post-push hooks source API key from `~/.claude/.secrets`

**Remaining:**

| Priority | Feature |
|----------|---------|
| P0 | **Multi-project `/brief`** — `--project api --project frontend` — small teams wear many hats |
| P0 | **Simple web dashboard** — memory browser, search, delete. No RBAC. One admin password. Teams need to see what's in the store. |
| P0 | **Demo video** — ships the week v2.1 does. Unblocks Product Hunt and HN Show HN. |
| P1 | **URL ingestion** — bootstrap context from docs, RFCs, public pages without copy-paste |
| P1 | **GitHub inbound ingestion** — issues, PR comments, commit history → memory context |
| P1 | **Path-aware memory surfacing** — memories tagged to file/directory paths surface in `/brief` when working in that context |
| P1 | **Streaming SSE progress** on ingest |
| P1 | **`/brief --post-compact`** — lightweight re-grounding after context compression; auto-invoked via `PostCompact` hook in all installer scripts |

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
