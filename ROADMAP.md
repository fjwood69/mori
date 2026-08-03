![Mori Roadmap](https://raw.githubusercontent.com/fjwood69/mori/12c16127afff279df6a4b4b9c6ccdd71b6b78f80/docs/assets/roadmap-banner.svg)

> **Make the agents better, don't care which ones.**

That's the company thesis. It's the position Devin can't occupy and the platform vendors won't: agent-neutral by design. Memory features will commoditise into every harness; governed, portable, self-hosted institutional memory that *outlives the agent* will not.

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
- OTel metrics — `/health`, `/metrics`

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
- Streaming read replica — `mori-pg-replica`, lag=0
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
- Human-review gate (Full two-phase B): UNRELATED candidates are surfaced as mori pending_writes + bridge-owned `intake_promotion_tickets`; Trusted-Dreamer approve = a vote (`human_approved`); the bridge finalizer re-runs GOV-002 against the live candidate before writing canon + lineage. Default routing; unattended auto-promotion is opt-in behind the flag. Provenance carries only an opaque ticket_uuid (no trusted ids); three forgery guards + idempotency via `intake_promotion_map`.
- Security hardening: AUTH-001 (file read via path traversal), PERF-003 (freshness thundering herd), PERF-004 (limit=0 full-table fetch)
- `agent-working-practices.md` — injected at session start via `/brief`
- Provenance scope routing (v2.2.22) — `MORI_BRIEF_SCOPE` (default `safe`): the brief withholds cross-project origin-bound canon (explicit `scope:global` only; `type` no longer auto-globalises) + zero-knowledge out-of-scope. Eliminates cross-repo retrieval interference: phantom-API attempts 0/20 (scoped) vs 20/20 (unscoped), replicated across two frontier-class models (Fisher p≈0). See [benchmarks](docs/benchmarks/README.md).
- **Cross-vehicle adherence benchmark (job-lesson delivery)** — eight coding agents, six model families, three arms (placebo / knowledge / prescriptive) against the §2 conflict fixture. **Two universals:** unguided, every vehicle breaks or games the hidden check; a prescriptive directive yields correct, safe completion universally. Surfaced knowledge eliminates blind harm everywhere but on the highest-drive model relocates it to coordinated circumvention (32/50, replicated n=50, Fisher p=2.5e-4, all contained by the read-only lock). **Establishes:** the directive binds where bare knowledge doesn't, and the deterministic gate is load-bearing under conflict — direct evidence for "the gate is the product." Conflict — not stakes or non-locality — is the moderator. See [Medium](https://medium.com/@fjwood/everyone-says-memory-makes-ai-coding-agents-smarter-07e9820b7d4e) / whitepaper.
- Pre-dream events normaliser (v2.2.23) — `normalise_events_text()` strips `Tool:` and `Stopped:` scaffolding lines before the dream LLM sees `events_text`. Line-anchored, case-sensitive, lossless-on-signal (FAILURE, Assistant prose, CWD, Prompt, Session headers preserved). Idempotent. Volume lever is compression, not censorship.

### v2.3.0 — identity-aware chokepoint: universal audit + tier/anatomy enforcement
The `store.write` chokepoint becomes a single audited authorization pipeline. **All enforcement ships default-OFF (audit-mode)** — a zero-behaviour-change deploy that starts the soak; the flip is per-actor, metric-gated, and later.
- **Universal in-transaction audit (Phase 1)** — every write carries structured `Provenance` (actor + actor_detail + source + op) and lands one `write_audit` row atomically with the write, the dreamer included. Closes per-caller audit drift.
- **Tier-capability enforcement (Phase 2)** — `MORI_TIER_ENFORCE` (audit | enforce | enforce:actor): an unauthorised tier target is rejected on both backends (canonical restricted to governed-promotion/init/import/system). `mori_tier_decisions_total{actor,intended_tier,decision,mode}` sizes the flip.
- **Anatomy enforcement** — `MORI_ANATOMY_ENFORCE`: a failed completeness verdict downgrades to pending; the `_skip_protection` trapdoor closes under enforce. `mori_anatomy_decisions_total{actor,code,mode}`.
- **SQLite off the event loop** — `AsyncStore` facade off-loads synchronous SQLite work to a dedicated single-thread executor (`run_in_txn`), removing the single-worker self-host stall.
- **Dream watermark fix** — a valid-empty batch now advances the watermark, so a low-signal batch can't permanently stall the dreamer.

---

## Horizon 1 — Governance: the human gate is the product

The differentiator is the gate itself — *machine proposes, human promotes* — and it's shipped and live: human-review surfacing (v2.2.16) plus the review roll-up (v2.2.18). **Unattended promotion (`MORI_INTAKE_PROMOTION_ENABLED`) is demoted to indefinitely-opt-in** — not the destination it was once framed as. The benchmark puts the value in the human gate (~22% → ~51% discovery-cost cut), so the active H1 work is the **measurement layer + curation throughput**, not the Stage-2 concurrency machinery (frozen — see below).

### Active
- ✅ **Structured-output assessor verdicts** (shipped v2.2.14) — strict `json_schema` + Pydantic validation replaces free-text parsing in B2; fail-closed to NEEDS_REVIEW on any malformed output
- **Measurement layer + curation throughput** — ✅ passive instruments shipped v2.2.19 (ingest-shape, canon-mortality, TD-reason/coverage, net-canon-growth, the Postgres retrieval-count fix); they ride `/metrics` and flag *when* to re-benchmark. Next: the published compounding curve from the accruing data.
- **Agent retrieval excludes WORKING/agent-intake tier** — blast-radius protector: pending intake candidates never pollute `/brief` (maintained regardless of promotion mode)
- **Bifrost circuit-breaker in assessor** — fast-model VK failures don't stall the assessment pipeline (matters in the human-gated path too)

### Frozen — unattended-promotion machinery (opt-in only)
Unattended promotion stays opt-in behind `MORI_INTAKE_PROMOTION_ENABLED`; these are frozen until the human-reviewed queue is consistently pristine (per the 2026-06 architecture review):
- Atomic assessment state machine — `ASSESSING` lease preventing concurrent B2 workers racing on a candidate
- Dream-concurrency guard OPS-002 — dream lease vs B3 promotion worker on the canon write connection
- E2E A→B1→B2→B3 test — full unattended round-trip before enabling auto-promotion

### Policy-as-config seam — 🚧 seam built + parity-tested, cutover pending
Simple declarative ruleset + tiny evaluator now. OPA/Rego as the enterprise evolution of the same seam — embedded in mori, not a separate engine. The pitch: regulated industries already maintain policy definitions; Mori makes those policies agent-aware without a separate governance committee. Roadmap OPA explicitly; build the seam, not the engine.

In progress: a `PolicyEvaluator` interface (`TinyEvaluator` now, `OpaEvaluator` later) over declarative rule-sets, with the tier-capability matrix and the GOV-001 eligibility pipeline expressed as config and **parity-tested against the live code** (config-eval ≡ code-eval across the full matrix). Predicates are a closed structured vocabulary dispatched to native Python — no `eval`, no free-form regex. The **cutover** — routing the live decisions through the evaluator instead of hardcoded Python — is a separate, post-soak, board-gated step (the parity test is its safety guard).

### TD review roll-up — ✅ shipped v2.2.18
Near-duplicate review candidates are grouped by convention (deterministic, embedding-free
suffix key) so the Trusted-Dreamer disposes of a convention once, not N times. Surfaced on
both review queues (`/api/pending/json`, `/intake/candidates`); review-side presentation
only — never drops at generation, never auto-merges. Embeddings deferred until the lexical
floor proves too coarse.

### Externalised distillation prompts — ✅ shipped v2.2.17
Dreamer and archivist prompts moved to editable files (`mori_advisor/prompts/*.txt`,
overridable via `MORI_PROMPTS_DIR`). Prompt rewrite: unit-of-output = the convention,
not the occurrence; dreamer `action` field dropped (it guessed set-relationships against
a canon it can't see); ingest output-contract no longer buried by tier/tags. See CHANGELOG.

### Human-review surfacing — ✅ shipped v2.2.16
Intake candidates → dreamer review UI + approve/reject → promote, now explicit and
human-gated (Full two-phase B). The default path: the bridge surfaces a pending_write
+ trusted ticket, a TD votes, the finalizer re-runs GOV-002 against the live candidate
before canon. Unattended auto-promotion remains opt-in behind `MORI_INTAKE_PROMOTION_ENABLED`.

### Additional governance hardening (general — independent of promotion mode)
- Candidate-body immutability — Postgres trigger rejecting `UPDATE` of `canonicalized_body`/`content_hash` once a candidate is `under_review` (defence-in-depth; the finalizer's `body_hash` pin already rejects a mutated body, so this hardens earlier in the lifecycle)
- Intake backpressure — 503 when candidate depth > N; prevents unbounded queue growth

Frozen with the unattended-promotion machinery above (opt-in only):
- Finalizer/drainer advisory lock — a Postgres advisory lock around `finalize_once`/`drain_once` so a future multi-worker bridge can't race a double canon write (today a single drainer + `unique(memories.name)` upsert makes this latent)
- Tailscale ACL `tag:intake←tag:hermes` — network-level isolation before any unattended promotion
- Flip sequence: operator-CLI promotion first → dream B3 second

---

## Horizon 2 — Sharpen the "earned memory" core

- **Generic `scope` metadata** — replaces "path-aware memory surfacing": JSONB scope map + client-side CWD→tags resolver kept out of core; index + inject on `/brief` in-context. Path-agnostic, more general.
- **Mid-session provenance re-grounding** — on a context shift (compaction, repo/worktree crossing), re-surface scoped canon *through the existing provenance gate* so out-of-scope memory is dropped and in-scope canon refreshed mid-session, not only at SessionStart. Framed as **harm-avoidance** (the same cross-contamination mechanism the scope router addresses) and **measure-before-ship** — explicitly *not* the speculative "just-in-time productivity pull," which was cut for want of evidence. (A conflict-fixture test design now exists — see the cross-vehicle adherence benchmark under Shipped — so "measure-before-ship" has an instrument.)
- **Event-log surface** — expose existing `/api/events` cleanly; webhook sidecar later, never outbound HTTP in core
- **Plugin-registration hooks in core** — the seam future policy packs and connectors slot into
- **`/reflect` command** — on-demand targeted dream; distil a specific topic now, not on the next cron cycle
- **URL ingestion** — fetch and ingest remote docs, RFCs, public pages without copy-paste
- **`curl mori.sh | sh` — explicitly NOT doing this** — Homebrew is the frictionless path

---

## Horizon 3 — Adoption & positioning

Near-zero coupling to core. Can ship any time.

- ✅ **README "Why use mori?" section** (shipped) — inverted to lead with the gate + the benchmark table (incl. the auto-extraction≈CLAUDE.md row); the dream pipeline demoted to the proposal half. Second pass added the **"What you get" on-ramp**.
- ✅ **Medium: "Everyone says memory makes AI coding agents smarter — nobody's showing the receipts"** (shipped 2026-06-21) — positioning piece built on the cross-vehicle benchmark + the curation null; argues "a note for the small stuff, a hard stop for the dangerous stuff." [link](https://medium.com/@fjwood/everyone-says-memory-makes-ai-coding-agents-smarter-07e9820b7d4e)
- **`docs/concepts/claude-md-vs-mori.md`** — the unconditional floor vs the governed layer above it; CLAUDE.md and Mori are complementary, not competing (canon *compounding* is a stated design thesis, not asserted)
- **Demo video** — cheap, high-leverage; unblocks Product Hunt, HN Show HN, enterprise eval cycles. Still not shipped.
- **Public roadmap page** — `moriapp.dev/roadmap` with feedback form; buried markdown helps nobody
- **Bifrost interface contract** — OpenAPI + contract test; publish/document as standalone OSS. The real "extraction" — not a code fork, a documented interface
- **Docker Compose as canonical self-host** — polish; Homebrew as frictionless install
- **Vertical packs (FSI first)** — OPA policy pack + conventions standards + migration runbooks as one commercial artifact, riding existing seams (`MORI_STANDARDS_DIR`, `/consult`, `/pensieve`). Engine stays AGPL; packs are the paid wedge into regulated industries.
- **Standards attestation** — pack/standards imports enter through the review gate (TD signs in, versioned, `write_audit` logged), not filesystem trust. Small change; it's the entire vendor-review answer.
- **Positioning: agent-neutral by design** — "bring your own agent; the knowledge outlives it." Memory features will commoditise into every harness; governed, portable, self-hosted institutional memory won't. Every roadmap and README sentence sells the second thing, never the first.

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
- **Team scope & identity model** — per-user memory ownership + `scope:user` privacy + tenant isolation, on a `Principal` seam (API-key identity today → SSO/SAML/OIDC) with store-enforced row-level security. Ownership is *orthogonal* to the open-core epistemic scoping (`project`/`global`): the open core routes *where a memory is valid*; the enterprise tier adds *who owns it and who may read it*. COIN identity scoping + namespacing + RLS for multi-tenant isolation. Specced; built to land enterprise deals, not on spec.
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

*Last updated: v2.3.7 — 2026-08-03*
