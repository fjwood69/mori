![Mori Roadmap](https://raw.githubusercontent.com/fjwood69/mori/12c16127afff279df6a4b4b9c6ccdd71b6b78f80/docs/assets/roadmap-banner.svg)

This file tracks what has shipped, what is in progress, and what is planned.
Updated with each release.

**North star:** the reference architecture for agent-memory governance. Thin, opinionated core you run at scale; revenue from making that easier, never from a heavier core. Governance is the thesis; everything else is implementation.

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
- `mori-msg` daemon pod — sole writer to `msg.db`
- `/brief` surfaces pending messages at session start
- `/wrap` broadcasts session summaries to `mori.msg.broadcast`
- Headless CC support — opt-in via `MORI_MSG_HEADLESS_ENABLED`
- moriapp.dev landing page — Cloudflare Pages, static, no build step

### v2.0.x
- Dual-backend store layer — SQLite (solo) or PostgreSQL (team), `MORI_DATABASE_URL`
- `BaseStore` ABC, `SQLiteStore` delegation wrapper, `PostgresStore` (asyncpg pool)
- `mori export` / `mori import` CLI tools — idempotent SQLite → Postgres migration
- NUC streaming replica via Tailscale — `mori-pg-replica` on port 5435, lag=0
- Daily pg_dump to GCS, 14-day lifecycle, GCS metadata server auth
- `deploy/solo/` (SQLite) and `deploy/team/` (Postgres + pgBouncer + WAL-G)

### v2.1.x
- Named API key authentication — `MORI_API_KEYS=name:secret,...`, ASGI middleware, capability scoping (`read < write < dreamer`), `policy.py` with `require_role()`
- PostCompact + SessionStart re-grounding hooks
- `/brief --post-compact` — lightweight delta re-grounding after compaction
- Async Postgres pipeline — ingestion, dream, contradiction scan all native asyncpg
- Postgres savepoint isolation in dream pipeline
- Native Prometheus `/metrics` exposition — direct GCO scraping, no OTel bridge
- Full-text search — FTS5 (SQLite) + tsvector GIN (Postgres)
- Schema migration runner — versioned, idempotent, dual-backend
- Assistant reasoning capture — Stop hook captures transcript tail
- Git commit ingestion — `POST /api/ingest/git`, watermark per repo, post-push hook
- Deployment contract — `scripts/verify-deployment.py` gates UAT pre-tag and CD post-deploy
- Systemd Quadlet migration — one orchestrator owns all GCE containers
- Postgres-first GCP startup script — bind mount, `pg_isready` gate, `MORI_REQUIRE_POSTGRES`, pg_dump cron
- UAT environment — `start-uat.sh` / `stop-uat.sh`, dual-backend smoke test pre-tag

### v2.2.x
- Plugin system — `plugins/mori/` for Claude Code, Cursor, Antigravity; marketplace-ready (`/plugin marketplace add fjwood69/mori`)
- One-click deploy — Railway, Render, Fly.io, Cloud Run buttons; free Postgres via Neon/Supabase
- Homebrew tap — `brew install fjwood69/mori/mori`; `mori-setup.sh` config wizard
- Web dashboard — memory browser at mori root URL; search, browse, unfurl; read-only REST API
- Trusted-dreamer review queue — pending proposals → dreamer review UI → approve/reject → canon
- Persistent audit trail — `write_audit` table, every governed operation logged
- Soft-delete with tombstone filtering — partial unique index, name reuse, restore
- Agent-memory governance pipeline (Stage 1 live, promotion dormant):
  - Physically separate intake service (`:8971`), separate Postgres, least-privilege `intake_app` role
  - Stream A: eligibility gate, proposition classifier, GOV-001 deny list, per-key rate limit
  - Stream B1: intra-pile dedup worker, content_hash coalescing
  - Stream B2: fast-model vs-canon assessor, fail-closed (NEEDS_REVIEW stays pending)
  - Stream B3: dream-trigger promotion, feature-flagged (`MORI_INTAKE_PROMOTION_ENABLED=false`)
  - Hermes v0.3.0: writes to intake, not canon; fail-closed if `MORI_INTAKE_URL` unset
- Security hardening: AUTH-001 (file read via path traversal), PERF-003 (freshness thundering herd), PERF-004 (limit=0 full-table fetch)
- `agent-working-practices.md` — injected at session start via `/ready` and `/brief`

---

## Horizon 1 — Finish governance to "promotable"

The differentiator. Stage 1 write-only intake is live. Path: policy + human review → flip `MORI_INTAKE_PROMOTION_ENABLED`.

### P0 gate items (blocking Stage 2)
- Structured-output assessor verdicts — removes free-text parsing from B2; deterministic verdict schema
- Agent retrieval excludes WORKING/agent-intake tier — prevents intake candidates polluting `/brief`
- Atomic assessment state machine — `ASSESSING` lease prevents concurrent B2 workers racing on the same candidate
- Bifrost circuit-breaker in assessor — fast model VK failures don't stall the assessment pipeline
- Dream-concurrency guard OPS-002 — dream lease and B3 promotion worker must not race on the same canon write connection
- E2E A→B1→B2→B3 test — full pipeline round-trip test before enabling unattended promotion

### Policy-as-config seam
Simple declarative ruleset + tiny evaluator now. OPA/Rego as the enterprise evolution of the same seam — embedded in mori, not a separate engine. The pitch: regulated industries already maintain policy definitions; Mori makes those policies agent-aware without a separate governance committee. Roadmap OPA explicitly; build the seam, not the engine.

### Human-review surfacing
Intake candidates → dreamer review UI + approve/reject → promote. The gap between "candidate assessed as UNRELATED" and "TD has reviewed and approved" must be explicit and human-gated.

### Additional governance hardening
- Intake backpressure — 503 when candidate depth > N; prevents unbounded queue growth
- Tailscale ACL `tag:intake←tag:hermes` before unattended promotion — network-level isolation
- Flip sequence: operator-CLI promotion first → dream B3 second

---

## Horizon 2 — Sharpen the "earned memory" core

- **Generic `scope` metadata** — replaces "path-aware memory surfacing": JSONB scope map + client-side CWD→tags resolver kept out of core; index + inject on `/brief` in-context. Path-agnostic, more general.
- **Event-log surface** — expose existing `/api/events` cleanly; webhook sidecar later, never outbound HTTP in core
- **Plugin-registration hooks in core** — the seam future policy packs and connectors slot into
- **`/reflect` command** — on-demand targeted dream; distil a specific topic now, not on the next cron cycle
- **URL ingestion** — fetch and ingest remote docs, RFCs, public pages without copy-paste
- **`curl mori.sh | sh` — explicitly NOT doing this** — Homebrew is the frictionless path

---

## Horizon 3 — Adoption & positioning

Near-zero coupling to core. Can ship any time.

- **README "Why use mori?" section** — right after the banner; the elevator pitch in the repo
- **`docs/concepts/claude-md-vs-mori.md`** — the unconditional floor vs compounding layer framing; CLAUDE.md and Mori are complementary, not competing
- **Demo video** — cheap, high-leverage; unblocks Product Hunt, HN Show HN, enterprise eval cycles. Still not shipped.
- **Public roadmap page** — `moriapp.dev/roadmap` with feedback form; buried markdown helps nobody
- **Bifrost interface contract** — OpenAPI + contract test; publish/document as standalone OSS. The real "extraction" — not a code fork, a documented interface
- **Docker Compose as canonical self-host** — polish; Homebrew as frictionless install

---

## Commercial perimeter

Decided on paper now. Open core stays thin.

### Open (AGPL-3.0) — always
- Dream pipeline, event capture, memory distillation
- Governance pipeline core (intake, assessor, promotion queue)
- Simple declarative policy ruleset + evaluator seam
- Named API key auth + capability scoping
- Read-only ingestion connectors
- REST API + OpenAPI spec
- Web dashboard (read + TD review)
- Docker Compose and GCP Terraform deployment
- All slash commands and MCP tools
- Proxy-auth-ready (`X-Forwarded-User`) — enterprise SSO slots in without touching core

### Commercial — enterprise tier
- **OPA/Rego policy packs** — PII classification, memory poisoning rules, compliance frameworks (FSI, healthcare). The "automate the governance committee" pitch for regulated industries. Embedded in mori, not a separate engine.
- **Managed hosting** — mori instance in the user's cloud account, managed by Anthropic/mori team
- **Bidirectional project intelligence connectors** — JIRA write-back, Azure DevOps, ServiceNow (read-only ingestion is open; write-back is commercial)
- **Advanced compliance logging** — SOC2, HIPAA audit trails with certified output
- **Distributed dream pipeline** — parallel dream pods, reconcile pod, `dream_jobs` coordination
- **K8s operator** — HA, rolling updates, federation
- **SSO / SAML / SCIM / LDAP** — via proxy-auth in open core; full IdP integration is commercial
- **Memory namespacing + COIN identity scoping + row-level security** — multi-tenant isolation
- **Memory poisoning guardrails** — Lakera / NeMo on dream pipeline LLM calls

---

## Parked (with reasons)

| Item | Reason |
|------|--------|
| Helm chart | No demand yet; Docker Compose covers the self-host case |
| Offline brief cache | Consensus risk — use WAL→intake replay if ever needed |
| `curl \| sh` installer | Anti-pattern; Homebrew is the frictionless path |
| Native SAML in core | Proxy-auth (`X-Forwarded-User`) is the right seam; full IdP is commercial |
| Split-brain / cross-region merge | Advanced self-hosting pattern, not a product feature |
| Mid-session push to active agents | Session-to-session latency model is by design |
| Message encryption between agents | All agents share NATS credential by design; revisit with namespacing |

---

*Last updated: v2.2.13 — 2026-06-07*
