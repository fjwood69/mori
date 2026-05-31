![mori Roadmap](https://raw.githubusercontent.com/fjwood69/mori/07780a6477fd5a2dd0ad693ed3ad237c30a8bda4/docs/assets/roadmap-banner.svg)


This file tracks what has shipped, what is in progress, and what is planned.
It is updated with each release.

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

---

## v2.0 — In progress

Core infrastructure hardening. No new features until the foundation is solid.

- PostgreSQL migration
  - asyncpg replacing sqlite3 throughout
  - pgBouncer connection pooling
  - Column encryption via `pg_crypto`
  - TLS on all Postgres connections
- WAL-G replacing Litestream — continuous GCS backup maintained, no gap in DR coverage
- JWT / API key auth — replace hostname-based trust (`MORI_TRUSTED_DREAMERS`)
- REST API
  - `GET /api/memories?query=...` — searchable from anywhere
  - `POST /api/memories` — ingest from external systems
  - `GET /api/events` — event log access
  - Webhook support — push notifications on significant memory writes
  - Foundation for dashboard UI and third-party integrations
- Prometheus native exposition on `/metrics` — homelab Prometheus scrape without OTLP

---

## v2.1 — Planned

Team features and resilience. Requires v2.0 foundation.

- Dashboard UI — consumes REST API, lightweight memory management interface
- Multi-project `/brief` — `--project mori --project bifrost`
- Memory namespacing — personal vs shared, team namespace, COIN identity scoping for enterprise
- Helm chart / K8s deployment — deploy to existing clusters via Helm, not just Docker Compose and GCE Terraform
- Split-brain / Toronto failover architecture
  - NUC as NATS leaf node
  - Async Postgres replica via Tailscale
  - Failover design for transatlantic link (uk-smr-* → ca-ws-* nodes, Q3 2026 relocation)
  - Toronto agents write locally, sync async — no 90ms RTT penalty on every write

---

## v2.2 — Planned

Integrations and distribution. Requires v2.1 team features.

- Ingestion parsers — Slack, Notion, Confluence, Teams, JIRA, GitHub
- URL ingestion — fetch and ingest remote documents
- SSE upload progress — streaming progress on ingest
- Demo video — unblocks Product Hunt and HN Show HN
- Headless CC cost guards — per-message spend caps on `MORI_MSG_HEADLESS_ENABLED`, not just global limits
- Project intelligence connectors — bidirectional integration with issue trackers
  - Inbound: issues → `/req`, sprint goals → `/brief`, PR comments and CI failures → memory context
  - Outbound: auto-tag commits with ticket IDs, auto-comment on tickets, auto-transition status, session summaries as work log entries
  - Connector interface (tracker-agnostic): `get_issues()`, `get_sprints()`, `get_epics()`, `comment()`, `transition()`, `link_commit()`, `close()`
  - Connectors (priority order): GitHub Projects, JIRA, Linear, Notion, Azure DevOps (commercial), Shortcut
  - Community-contributable — enterprise connectors behind commercial licence

---

## v3.0 — Future

Horizontal scale and advanced intelligence. Build when paying customers need it.

- Distributed dream pipeline — three-phase parallel distillation
  - Phase 1 (fast VK): categorise and cluster all events by semantic proximity, assign non-overlapping slices to dreamers
  - Phase 2 (parallel dream): stateless dream pods, each claims a cluster from `dream_jobs` table, dreams independently, guaranteed no contradictions within a slice
  - Phase 3 (reconcile): `mori-reconcile` pod runs after all dreamers complete — lightweight cross-cluster dependency resolution on known edge list from phase 1
  - Fast VK earns its keep twice — freshness check and pre-dream clustering in a single pass
  - Scale by adding dream pods — Postgres coordinates via `dream_jobs` (claim, in-flight, complete)
- Memory poisoning guardrails — Lakera / NeMo on dream pipeline LLM calls (requires multi-tenancy first)
- Bifrost composite routing metric — throughput-weighted VK scoring
- Horizontal scaling — multiple `mori-advisor` instances behind a load balancer

---

## Experiments / Research

- Memory merge strategy — post-Toronto relocation, real split-brain data to work with
- Bifrost custom provider pricing accuracy
- Reflect operation — surface as a first-class `/reflect` command, analogous to dream but on-demand and targeted

---

## Not planned

- Mid-session push to active agent sessions — latency model is session-to-session by design
- Message encryption between agents — all agents share NATS credential by design; revisit when memory namespacing ships and enterprise pilots begin
- Multi-hop message routing — point-to-point and broadcast only

---

*Last updated: v1.1.0*
