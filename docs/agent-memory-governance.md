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

### Stage 1 — Intake & Triage (new service)
Fast, horizontally scalable, **separate from `mori-dream`**, its own store/schema.
Absorbs burst volume so it never back-pressures the agent *or* the dream.
- fast **similarity / dedup** vs existing canon+working+pending (near-neighbour → bump a
  reinforcement counter instead of creating a new proposal);
- **eligibility** gate (namespace prefixes + a cheap proposition classifier — reject
  chatter/scratch);
- **buffer/debounce/rate-limit**; emits vetted **proposals**.

### Stage 2 — Dream (existing, deliberate, unchanged cadence)
Consumes already-filtered/deduped proposals. Final distillation + assessment + **promotion**
(contradiction vs canon, supersession, trust curve). Stays batch because Stage 1 absorbed
the volume.

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
