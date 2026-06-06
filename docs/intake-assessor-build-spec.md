# Intake Assessor — build spec (Stream B)

> Executable spec for **Stream B** of the agent-memory-governance system — the canon-aware
> back end. Pairs with [intake-service-build-spec.md](intake-service-build-spec.md) (Stream A,
> the front door) and [agent-memory-governance.md](agent-memory-governance.md). This is the
> **frozen contract** for the A↔B seam; build to it.
> Branch: `feat/intake-assessor`, **branched off `feat/intake-service`** (so A's `mori_intake/`
> schema + `normalize.py` are present). **No push, no deploy, no prod touch.**
> ⚠️ Stream B touches the **existing dream pipeline** → run `gitnexus_impact` on every existing
> symbol you edit BEFORE editing, and report HIGH/CRITICAL blast radius before proceeding
> (CLAUDE.md mandate).

## What Stream B is (the cheap-model back end)

Stream A leaves deduped, eligible **`pending`** candidates in intake. Stream B does the rest of
the pipeline using the **FAST (cheap) model — never the dream — for dedup**, and the dream only
for distillation of genuine novelty:

- **Step 2 — fast vs-canon dedup** (cheap model): for each `pending` intake candidate, check it
  against mori canon. Already-known → reinforce canon + retire the candidate. Novel → forward.
- **Step 3 — dream distils survivors** → the single canon writer commits to canon with lineage.

The economic invariant (from Fred): **dream spend scales with novelty, not agent volume.** A
burst of repeated agent claims is absorbed by Step 1 (A) + Step 2 (fast model) and never reaches
the dream.

## The A↔B seam (FROZEN — both streams build to this)

### 1. Candidate lifecycle (`intake_candidates.status`)
```
 pending        ← written by Stream A (front door), after intra-pile dedup
   │  (Step 2 assessor picks it up)
   ├─ rejected      ← duplicate-of-canon (SUPERSEDES/RELATED) OR rejected by assessment
   └─ under_review  ← novel; eligible for distillation
        │  (Step 3 dream distils → enqueues promotion_queue)
        └─ promoted  ← canon writer committed it; promoted_canon_name + promoted_at set
```
- **Stream A only ever writes `pending`.** Stream B owns every transition out of `pending`.
- All transitions set `updated_at = now()`. `rejected` sets `rejection_reason`. `promoted` sets
  `promoted_canon_name`, `promoted_at`.

### 2. `promotion_queue` drain contract (the single canon writer)
- The dream (Step 3) **enqueues** a row per survivor: `(candidate_id, status='queued')`.
- **One** process — the canon writer — drains it. It is the **sole holder of mori canon write
  creds**. Poll with `SELECT ... FOR UPDATE SKIP LOCKED` (Postgres) ordered by `created_at`.
- Per row: write the canon memory → write mori-side `memory_intake_lineage` → write intake
  `intake_promotion_map` → set candidate `promoted` → set queue row `committed`. On failure:
  `attempt_count++`, `error_message`, leave `queued`/`failed` for retry.
- **At-least-once + idempotent**: before writing canon, check `intake_promotion_map` for the
  `candidate_id`; if present, the canon write already happened — skip to marking `committed`.
  **NOT** XA/2PC — never couple canon availability to intake.

### 3. New mori-side table — `memory_intake_lineage` (new migration in `mori_advisor/store/migrations.py`)
```
memory_intake_lineage(
  canon_name            varchar(128) PRIMARY KEY,   -- the mori memory it traces to
  intake_candidate_id   uuid NOT NULL,
  intake_submission_ids uuid[] NOT NULL,
  trust_snapshot        jsonb NOT NULL,             -- reinforcement_count, corroborating agent_ids, scores
  promoted_at           timestamptz NOT NULL DEFAULT now()
)
```
- On promotion the canon writer also populates mori's existing `memories.origin_clients` with the
  distinct corroborating `agent_id`s from `intake_corroborations`.
- Add it via the migration **registry** (ordered, ledgered) — NOT bare CREATE IF NOT EXISTS.

## Step 2 — the fast vs-canon assessor

### `/assess` capability (mori-side, stateless, read-only)
A new mori endpoint OR an internal callable — stateless, reads canon, runs the FAST model.
- Input: `{ body: str, content_hash: str }` (candidate's canonicalised body + its hash, using
  A's `mori_intake.normalize.content_hash` — import it; do NOT reinvent the hash).
- Behaviour: retrieve top-k canon neighbours (text search / existing mori search in MVV; canon
  embeddings later) → ask the FAST model (same cheap contradiction model the dream uses, via
  Bifrost) to classify the candidate vs each: `RELATED | SUPERSEDES | UNRELATED`.
- Output: `{ verdict: RELATED|SUPERSEDES|UNRELATED, matched_canon_name: str|null, score: float }`.
- **Stateless** — `/assess` writes nothing. It is the shared *capability*; the data stays in
  intake. (This is what keeps "shared model, separate data" true.)

### The assessor worker (Stream B's Step-2 loop)
Reads intake `intake_candidates WHERE status='pending'`, and for each:
- call `/assess`;
- `SUPERSEDES`/`RELATED` (≥ threshold) → **reinforce canon** (MVV: bump the matched canon
  memory's retrieval/reinforcement signal + log; richer trust curve is Slice 3) and set the
  candidate `rejected` with `rejection_reason='duplicate-of-canon:<matched_canon_name>'`;
- `UNRELATED` (novel) → set candidate `under_review` (hand-off to Step 3).
- Idempotent, restartable; one bad candidate must not stall the loop (attempt cap → log+skip).

## Step 3 — dream distils survivors

- Extend the dream so that, in addition to its existing event-distillation, it consumes intake
  candidates in `under_review`, distils each into a final canon-ready memory (title/body/tags/
  type), and **enqueues** `promotion_queue` (does NOT write canon directly — the canon writer
  does). Reuse the dream's existing distillation/LLM path; this is new *input + output plumbing*,
  not a new model.
- **MVV allowance** (from `consult-cfaebbeeffb8` §6): if standing up a separate canon-writer
  process is too much for the first cut, the dream MAY insert into canon directly in MVV —
  but it MUST still write `memory_intake_lineage` + `intake_promotion_map`, and this is flagged
  as a **known single-writer violation to refactor before multi-agent scale**. Prefer the real
  single-writer if the lift is small.
- Run `gitnexus_impact` on `DreamPipeline.run` and anything you touch in `dream.py` first;
  report the blast radius. Do not change the dream's existing cadence or event-watermark logic.

## Tests
- **Pure-logic / unit (always run)**: the `/assess` verdict→action mapping (SUPERSEDES/RELATED→
  reject+reinforce; UNRELATED→under_review); the canon-writer idempotency guard (second drain of
  an already-promoted candidate is a no-op); the promotion_queue state machine.
- **Postgres-integration (gated on `MORI_INTAKE_TEST_DATABASE_URL` + mori's PG test DSN)**:
  seed a `pending` candidate → assessor (with a STUBBED `/assess` returning a fixed verdict, no
  real LLM) transitions it correctly; an `under_review` candidate → dream enqueues → canon
  writer commits → assert: a canon memory exists, `memory_intake_lineage` row exists,
  `intake_promotion_map` row exists, candidate is `promoted`, queue row `committed`; re-run the
  drain → no duplicate canon row (idempotent). **Stub the FAST model** — deterministic, no
  network, no real sleeps.
- `ruff format` + `ruff check` clean (pre-commit ruff hook silently blocks unformatted commits).

## Definition of done (Stream B)
`gitnexus_impact` reported for every touched existing symbol; unit tests green unconditionally;
PG-integration green against throwaway Postgres with a stubbed fast model; the A→B→canon path
demonstrably moves a seeded `pending` candidate to a real canon memory with lineage, and is
idempotent on redrive; reviewer (Opus) reads the diff + the impact report before any commit;
commit on `feat/intake-assessor` only — **no push**.

## Out of scope for Stream B (later)
Embeddings/pgvector similarity in `/assess` (MVV uses text-search top-k); trust curve + decay;
PII quarantine; consent bit for `target=user`; retention/partitioning. These are Slice 3.
