# Agent-Memory Governance — design of record

> Status: **DESIGN / PROPOSAL** (drafted 2026-06-06, ~0800, after a long live-debug
> session). Not yet built. Reviewed by Fred (architect/dreamer) in conversation; this
> file is the spec to build from, not a chat reconstruction.

## Status (v2.2.12)

The pipeline (Stream A through B3) is **SHIPPED to `main`**. As of v2.2.12, **Stage 1
(write-only intake) is ENABLED in production**: the intake service runs on GCE and Hermes
mirror-writes land as `pending` intake candidates. **Promotion to canon remains OFF** — the
running service starts only the dedup/TTL worker and never invokes the canon writer;
`MORI_INTAKE_PROMOTION_ENABLED` is off and promotion is reachable solely via the manual
`python -m mori_intake.cli` trigger (operator-run, Stage 2). See the **Stage-1 enablement
runbook** at the end of this file.

The three security/perf criticals (AUTH-001, PERF-003, PERF-004) are active.

**Pre-enable gate — the following must be completed before enabling unattended promotion:**
1. Structured-output verdict schema in the B2 assessor (removes free-text parsing).
2. Private-IP SSRF guard on `MORI_INTAKE_URL` at startup (server-side complement to the
   provider-side no-redirect opener already shipped).
3. Human-review gate / trust curve (Slice-3) — agent memories must not self-promote to
   canonical without a trust threshold or human approval.
4. Dream-concurrency guard OPS-002 — B3 promotion and the standard dream run must not
   race on the same canon write connection.
5. End-to-end pipeline test (A → B1 → B2 → B3 → canon) in CI.

See ROADMAP.md for the full pre-enable tracking table.

## Problem

mori is a **governed** memory store built for human / Claude-Code-instance contributions,
distilled periodically by the `dream` pipeline. An **autonomous agent** (NousResearch
hermes-agent, via our `hermes-mori-provider`) is a *different* writer: continuous, high
volume, untrusted, non-human. Letting it write into mori the way a CC instance does breaks
the assumptions mori's governance was built on. We need governance **policy** and a
**pipeline** for agent-originated memory.

## Keystone realisation

**mori is the canon-of-record an agent READS and EARNS its way into. A separate INTAKE
service is the untrusted front door it WRITES to.** The agent must **not** write into
mori's core memory table — that makes the trust boundary a mere `source:hermes` tag in a
table of vetted canon. The trust boundary must be a **data boundary**.

Corollary: this dissolves the "agent write lands directly in `working` tier" problem we
hit — that only exists *because* the provider POSTs to mori's `/api/memories`. If it
writes to intake instead, there is no direct-to-canon path; **promotion is the only door**
into mori's canon.

## Architecture — asymmetric read / write / promote

> **Backend: Postgres-only.** This pipeline requires concurrent writers + the async
> store and is **unavailable on a SQLite mori** (the solo/dev base mode). SQLite is the
> zero-config default; Postgres is mandatory for team/multi-agent/production use. See the
> README "Backend requirement" note. We do not build this capability for both backends.

```
  AGENT (hermes)                STAGE 1: INTAKE (new, separate, scalable)        mori (Stage 2 + canon)
  ┌───────────┐  write          ┌─────────────────────────────────────┐         ┌──────────────────┐
  │ memory    │ ───────────────▶│ • fast similarity check vs canon/    │  promote│ dream pipeline   │
  │ tool      │  (NOT mori's    │   working/pending  → DEDUP           │ ───────▶│ (deliberate):    │
  │           │   /api/memories)│ • eligibility gate (namespace +      │         │  assess + promote│
  │ provider  │                 │   proposition classifier)           │         │  pending→working │
  │  + LWM    │ ◀───────────────│ • buffer / debounce / rate-limit     │         │  →canonical      │
  └───────────┘  read canon     │ • own store (raw src, session_id,    │         ├──────────────────┤
       ▲         (recall)        │   sim scores, trust ledger, prov.)   │         │ CANON memory     │
       └──────────── read ───────┼──────────────────────────────────────┼─────────│ table (trusted;  │
                  mori canon     └─────────────────────────────────────┘         │ promoted-only)   │
                                                                                  └──────────────────┘
```

- **Read:** agents recall from mori's **canon** (shared, promoted memory). That is the
  cross-device value — shared *canon*, not shared scratch.
- **Write:** agents write to the **intake service** (Stage 1's own store), never mori's
  core write API. Plus the local **LWM** (Local Working Memory) overlay for the agent's own
  read-your-writes.
- **Promote:** Stage 1 triage → Stage 2 dream → *that* is the only writer into mori canon.

### Stage 1 — Intake & Triage (new service) — *all dedup, cheap model only*
Fast, horizontally scalable, **separate from `mori-dream`**, its own store/schema. Absorbs
burst volume so it never back-pressures the agent *or* the dream. **Owns dedup end-to-end so
the expensive dream never pays to deduplicate:**
- **Step 1 — dedup the agent pile (intra-intake):** exact `content_hash` now, vector
  near-neighbour later, against intake's *own* pending candidates → coalesce repeats into one
  candidate + bump a reinforcement counter. Fully local; **no canon access**.
- **Step 2 — dedup the survivors vs canon (FAST model, NOT the dream):** what's left is
  checked against mori canon with the **cheap fast model** + canon embeddings — mori exposes a
  *stateless* assess capability (the model is a shared service; the data stays separate).
  Already-known → reinforce canon / drop the candidate; genuinely novel → forward to Stage 2.
  Spending dream money to do this would be waste — the fast model does it.
- **eligibility** gate (namespace prefixes + a cheap proposition classifier — reject
  chatter/scratch), **buffer/debounce/rate-limit**; emits only **novel, vetted** proposals.

### Stage 2 — Dream (existing, deliberate) — *distil only, no dedup*
Consumes only the **genuinely-novel** survivors of Stage 1 (dedup already done, cheaply). Does
the expensive work it is actually for: final **distillation** + **promotion** into canon
(supersession, trust curve) via the single canon writer. **Dream spend now scales with
*novelty*, not agent volume** — a burst of repeated agent claims costs the fast model, not the
dream.

They **share a capability (FAST model + embeddings) but not a workload** — different SLAs,
different scaling → different services. The interface between them is just the proposal
queue + assessment metadata. The agent never talks to the dream.

## Submission policy (from /consult, architecture focus)

Principle: **treat the agent as an untrusted high-volume proposer; enforce all policy
server-side.** Do not rely on the agent to classify its own output (this is why the
invented `durability` flag was doomed — selection can't live in the thing being governed).

1. **Eligibility — default-deny.** Namespace gate on `target` + `stable_key`
   (`learned-*`/`fact-*` eligible; `session-*`/`scratch-*` rejected; for `user`, only
   allow-listed `preference-*`/`accessibility-*`, never inferred `psychology`/`health`) +
   a proposition classifier (real subject-predicate-object claim). `remove` always → human.
2. **Assessment — auto-assess the safe majority, escalate the rest.** Reuse dream
   RELATED/SUPERSEDES/UNRELATED; contradiction blocks auto-promotion; novel/UNRELATED gets a
   cold-start penalty (longer pending + human eyes); PII → quarantine. Run **async** after
   landing in pending, never on the write path.
3. **Promotion — asymmetric trust curve.** Agent writes **start at pending** and climb
   steeper than human writes. pending→working: clean auto-assessment + time-in-tier;
   working→canonical: human approval **or** cross-source corroboration. Agent memories
   **decay** (unread/unreinforced 30d → demoted); canon does not. Agent claims must *earn*
   persistence.
4. **Volume — buffer/debounce/dedup at the boundary** (5-min coalescing per `source:hermes`,
   ~20/hr rate limit, embedding similarity gate >0.92 → reinforce not duplicate).
5. **Scope/safety — user assertions are high-stakes.** Namespace ACL (allow-listed user
   categories only), consent bit before `target=user` promotes, PII hard-stop, retraction =
   tombstone with `SUPERSEDES` authority, 90-day provenance audit.

## Required mori-server capabilities (a roadmap, NOT build-it-all)
(1) route agent writes to intake (not canon); (2) IngestionPolicyEngine (namespace registry
+ proposition classifier); (3) PII classifier in the dream path; (4) promotion scoring +
quarantine state; (5) trust ledger + TierDecayScheduler; (6) ProposalBuffer (debounce, rate
limit, pre-submit dedup); (7) ConsentRegistry + namespace ACL; (8) tombstone/retraction.

### MVP first slice (makes the agent safe-by-default)
- the **separate intake store + endpoint** (agent writes here, never mori canon);
- a **basic eligibility gate** (namespace + proposition);
- **asymmetric trust** (agent starts pending; needs approval/corroboration to reach
  working).
Everything else is progressive hardening as volume justifies it.

## What this means for `hermes-mori-provider` (already shipped, v0.2.0)
- The provider's **write target is wrong for this design**: it POSTs to mori's
  `/api/memories` (canon write path). It should POST to the **intake service**. mori's core
  write API reverts to trusted/promoted writes only.
- The **LWM + outbox** machinery survives unchanged; only the endpoint + the store on the
  other end change.
- v0.2.0 write-path is **verified working** (a `hermes-memory-*` memory landed in mori,
  hyphen name, `source:hermes`, searchable) — but it landed **working-tier direct**, which
  this design replaces with intake→promote.

## Decisions on record (Fred)
- Agent writes must go through the **same rigour** (review + assessment), not direct writes.
- **Two stages**: (1) fast similarity/dedup + propose; (2) dream final distillation.
- Stage 1 must be **very separated from `mori-dream` and scalable**.
- Hermes should **not share a mori DB table** — separate intake store; mori canon receives
  promoted-only.

## Related open item
This converges with the **dream→canon governance gap** found earlier (the dream distilled
an agent's summary straight to canon, ungated; a phantom-bug memory was deleted). Same root:
**agent-sourced writes skipping the review chokepoint.** One principle fixes both — make the
review/promotion path the single gate for anything an agent originates (Hermes's writes *and*
the dream's agent-distillations).

> **Update (v2.3.0):** `store.write` is now a single audited authorization chokepoint. Every write
> — agent, REST, the dreamer, imports — lands a `write_audit` row in the same transaction (the
> audit half of this principle, always on). **Tier-capability enforcement** (`MORI_TIER_ENFORCE`)
> can then reject a direct agent/dreamer write to `canonical`, forcing it through the governed
> intake/promotion path; a **completeness gate** (`MORI_ANATOMY_ENFORCE`) downgrades warrant-less
> writes to review. Both ship **audit-mode (default-OFF)** with `mori_tier_decisions_total` /
> `mori_anatomy_decisions_total` to size the flip. See
> [Write chokepoint](reference/configuration.md#write-chokepoint--audit--tieranatomy-enforcement).

## DB / storage layer (from /consult, 2026-06-06)

**Topology:** intake is a **physically separate Postgres** (own cluster/DB, pools, WAL,
backup) — NOT mori's SQLite/Postgres. SQLite is disqualifying for intake (file-lock
serialises concurrent agent writes). **No external queue in MVV** — the agent's outbox is
the first buffer; the `intake_submissions` table IS the ingestion log, drained by async
workers. Add NATS JetStream/Redis only if a single Postgres node is proven the bottleneck
(>1–2k/sec).

**Five tables + pgvector** (separate raw / dedup-candidate / trust / seam):
- `intake_submissions` — immutable raw firehose (session_id, agent_id, target_name, action,
  raw_source_text, stable_key, provenance jsonb, content_hash). UNIQUE(session_id, stable_key).
- `intake_candidates` — dedup+similarity layer (canonicalized_body, content_hash UNIQUE,
  embedding vector(768), status[pending|under_review|promoted|rejected|decayed], trust_score,
  reinforcement_count, decay_score, promoted_canon_name). HNSW index on embedding; hash index.
- `intake_corroborations` — trust ledger (candidate_id, submission_id, agent_id,
  source_weight). UNIQUE(candidate_id, submission_id).
- `promotion_queue` — the seam (candidate_id, status[queued|processing|committed|failed],
  canon_name, attempt_count).
- `intake_promotion_map` — cross-system lineage (canon_name PK, candidate_id, submission_ids[],
  provenance_snapshot). Lives in intake (mori canon must not learn intake's schema).

**The seam — single canon writer:** the dream evaluates candidates → writes decisions into
`promotion_queue`. **Only mori's governance ingester** holds canon write creds; it polls
the queue (`FOR UPDATE SKIP LOCKED` on PG, single conn on SQLite), writes the canon memory
(populating `origin_clients` with corroborating agent_ids + a new mori `memory_intake_lineage`
table), then marks `committed`. **At-least-once + idempotent** (check `intake_promotion_map`
before re-insert) — NOT XA/2PC (don't couple canon availability to intake).

**Scalability:** HTTP handler ONLY validates + inserts to `intake_submissions` → 202; never
computes embeddings on the request path. Embedding/dedup/trust in **horizontal workers**
polling submissions. Partition submissions by month; archive rejected/decayed >90d.
Back-pressure is fine — agent reads its own writes from LWM, so intake can lag seconds/minutes.

**Consistency:** read-your-writes = the provider's **LWM** (not intake). Intake→canon is
eventual, reconciled by the promotion queue + the provider's hash compare vs canon.
**CRITICAL invariant:** intake must compute `content_hash` with the IDENTICAL normalisation
mori canon uses (NFKC + whitespace collapse) or the provider sees false hash mismatches.

**MVV:** one dedicated Postgres for intake; mori stays as-is; the 5 tables (no partitioning);
pgvector IVFFlat if HNSW unavailable, or hash-only dedup if no pgvector; one async worker;
dream→promotion_queue→a lightweight mori poll-and-insert (or dream inserts directly in MVV,
refactor to single-writer before multi-agent). Nightly delete of old submissions + vacuum.

Full DDL sketch is in the consult log (2026-06-06).

## PostgreSQL dedup precision (Slice-3)

On the PostgreSQL backend the Step-2 vs-canon assessor (``assessor.py`` /
``canon_writer.py``) queries the mori canon store via ``search_json``, which
uses ``websearch_to_tsquery('english', …)`` — an AND over every lexeme in the
query.  Because the full candidate body is passed as the query, all terms must
be present in a canon document for that document to surface as a neighbour.

This means the text-search dedup is **near-exact-only by design**: a canon
memory that paraphrases the candidate body with different vocabulary will not
be returned, and the candidate will proceed to promotion as if it were novel.
The assessor will therefore generate false negatives (missed duplicates) for
semantically similar but differently-worded content.

**Current behaviour is acceptable for the MVV slice and is flag-gated** —
the feature is only active when ``MORI_INTAKE_PROMOTION_ENABLED=true``.
However, precision must be monitored in production because undetected
paraphrase duplicates inflate canon volume and degrade recall quality over
time.

**The real fix is embedding similarity (Slice-3):** replace or supplement the
``search_json`` vs-canon step with a vector-nearest-neighbour query (pgvector
HNSW index on canon embeddings) to catch semantic duplicates regardless of
surface wording.  Until Slice-3 ships, do **not** remove the flag gate or rely
on the assessor to catch anything other than near-exact canon matches.

## Known trust-boundary gap (Slice-3)

Promoted memories land with `tier='working'` and `type='agent-intake'` in mori canon.
Any consumer that trusts the `working` tier **implicitly trusts agent-intake memories**
until the Slice-3 trust-curve filters are in place.

**The trust-curve MUST key off `type` and lineage, NOT tier alone.**  Specifically:

- `type='agent-intake'` identifies promoted agent memories at the row level; this
  survives any tier promotion (e.g. working → canonical).
- The `memory_intake_lineage` table carries the full provenance chain
  (candidate id, submission ids, trust snapshot) so filters can inspect the
  reinforcement count, corroborating agent ids, and promotion timestamp.
- Slice-3 will add trust-curve filters that gate on `type='agent-intake'` and/or
  the presence of a `memory_intake_lineage` row before treating a memory as
  equivalent to a human-authored one.

**Until Slice-3 ships:** any code that searches or reads `tier='working'` memories
without a `type` filter will receive a mix of human-authored and agent-promoted
memories.  Callers that require only human-authored memories should add
`WHERE type != 'agent-intake'` (or equivalent filter on the store's search API)
as a temporary guard.  This is a known, accepted gap for the MVV slice.

---

## Stage-1 enablement runbook (v2.2.12, single-operator GCE)

Stage 1 = **write-only intake**. Hermes mirror-writes → intake service → `pending`
candidates (eligibility-gated + deduped). **Nothing promotes.** This was enabled after a
deep `/consult` (architecture focus) that hardened the plan; the decisions are baked into
`deploy/gcp/provision-intake.sh` and `quadlet/mori-intake.container`.

### Topology
- **Intake service**: GCE VM (`mori-vm`, Tailscale `<vm-host>`), rootless
  Quadlet `mori-intake.service`, port **8971**, image `ghcr.io/fjwood69/mori:latest`.
- **Postgres**: shared `mori-pg` container. Separate `intake` database owned by a
  least-privilege `intake_app` role; `CONNECT ON DATABASE mori` revoked from `PUBLIC` so
  `intake_app` is **kernel-blocked** from canon (`mori` is a superuser and is unaffected).
- **Hermes**: host container `hermes`; provider v0.3.0 with
  `MORI_INTAKE_URL=http://<vm-host>:8971` + the `intake-hermes` write key. Fails closed
  (queues, never writes canon) if the URL is unset/unreachable.

### Provision (first time, or after a fresh data disk)
```bash
# As the mori user on the VM. Idempotent; verifies the boundary before starting.
ssh mori@<vm-host> 'bash -s' < deploy/gcp/provision-intake.sh
```
It creates the role + `intake` DB, writes `/data/mori-intake/.env` (secrets), installs the
quadlet, starts the unit, and asserts `intake_app` is REFUSED on `mori` but accepted on
`intake`. Secrets + DB persist on `/data`; `startup.sh.tpl` re-starts the unit on every boot.

### Verify
```bash
ssh mori@<vm-host> 'curl -s localhost:8971/ready'           # {"status":"ok",...}
ssh mori@<vm-host> 'curl -s -H "X-Api-Key: <write-key>" \
    "localhost:8971/intake/candidates?status=pending&limit=20"'  # operator view
```

### Validate the loop (no canon write)
1. Note the canon memory count: `mori_advisor` `SELECT COUNT(*) FROM memories`.
2. Have Hermes write one durable learning (fires `on_memory_write` → outbox → intake).
3. Confirm it appears under `GET /intake/candidates?status=pending` and that the canon
   count is **unchanged**.

### Rollback
- **Soft**: unset `MORI_INTAKE_URL` on hermes + restart → provider fails closed into its
  bounded outbox (no data loss; rows drain when re-pointed).
- **Hard**: `systemctl --user stop mori-intake` on the VM. Pending candidates persist in the
  `intake` DB; canon is untouched throughout (Stage 1 never writes canon).

### Canon-read SLO (shared-Postgres guard)
Intake is high-churn (submissions/dedup/purge) and shares `mori-pg` with low-churn,
high-value canon. The guard rails:
- Intake pool capped (`MORI_INTAKE_POOL_MAX=4`); container capped
  (`MemoryMax=256M`, `CPUQuota=50%`) so intake can never OOM-kill or starve canon.
- **SLO**: canon read (`mori-advisor` `/api/memories?limit=20`) p99 **< 250 ms**. If
  violated, first lower `MORI_INTAKE_POOL_MAX`/raise the worker interval; escalate to a
  dedicated Postgres instance/tablespace only if the SLO stays breached. Enable
  `pg_stat_statements` to attribute load.

### Known Stage-1 residuals (tracked for Stage 2)
- **Tailscale ACL** `tag:intake ← tag:hermes` not yet wired (single-operator tailnet). Until
  then network reachability of `:8971` is any tailnet peer + the write key. Blast radius is
  bounded — a peer could inject *junk pending candidates* (never canon; eligibility-gated,
  deduped, TTL-purged). **Wire before Stage 2.**
- Stage-2 blockers remain: structured-output assessor verdicts; Hermes retrieval must
  exclude the WORKING/agent-intake tier by default (reflexive contamination); Bifrost
  circuit-breaker; atomic assessment state machine. See mori #16.
