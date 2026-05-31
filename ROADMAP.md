# Mori Roadmap

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

---

## In progress

### v2.0
- moriapp.dev landing page — Cloudflare Pages, static, no build step
- PostgreSQL migration
  - asyncpg replacing sqlite3 throughout
  - pgBouncer connection pooling
  - Column encryption via `pg_crypto`
  - TLS on all Postgres connections
  - NUC read replica via Tailscale
- JWT / API key auth — replace hostname-based trust (`MORI_TRUSTED_DREAMERS`)

---

## Planned

### v2.x — high value, lower effort (do next)

- Ingestion from URL — fetch and ingest remote documents
- Multi-project `/brief` — `--project mori --project bifrost`
- Streaming upload progress (SSE)
- Demo video — unblocks Product Hunt and HN Show HN

### v2.x — high value, higher effort (plan in)

- Dashboard UI — lightweight web interface for memory management
- Memory namespacing — personal vs shared, team namespace, COIN identity scoping for enterprise
- Slack / Notion / Confluence ingestion parsers
- Memory poisoning guardrails — Lakera / NeMo on dream pipeline LLM calls

### v2.x — lower priority

- Scheduled / recurring ingestion
- Cross-project memory auto-tagging (`scope:cross-project`)
- Per-project DB isolation
- HN Show HN — karma-gated, will happen when ready
- "What I learned building Mori with AI" — engineering article

### Experiments

- Split-brain homelab vs GCE Mori instances — independent stores, diverging memory
- Memory merge strategy across independent stores
- Parallel ingestion workers — unlocked by Postgres concurrency
- Bifrost composite routing metric — throughput-weighted VK scoring
- Bifrost custom provider pricing accuracy

---

## Not planned

- Mid-session push to active agent sessions — latency model is session-to-session by design
- Message encryption between agents — all agents share NATS credential by design
- Multi-hop message routing — point-to-point and broadcast only

---

*Last updated: v1.1.0*
