# Agent-Memory Governance — design of record

> Status: **DESIGN / PROPOSAL** (drafted 2026-06-06, ~0800, after a long live-debug
> session). Not yet built. Reviewed by Fred (architect/dreamer) in conversation; this
> file is the spec to build from, not a chat reconstruction.

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
