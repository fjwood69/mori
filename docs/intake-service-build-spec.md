# Intake Service — build spec (Slice 1)

> Executable spec derived from [agent-memory-governance.md](agent-memory-governance.md) and the
> two `/consult` outputs (policy `consult-b1000adc9f89`, DB `consult-cfaebbeeffb8`).
> This is the **frozen contract** for Slice 1. Build to it exactly; deviations get noted, not silently taken.
> Branch: `feat/intake-service`. **No push, no deploy, no prod touch.**

## Why this exists (one paragraph)

An autonomous agent (hermes) must not write into mori's canon. It writes to a **physically
separate intake store**; only a deliberate promotion path moves anything into mori canon. The
trust boundary is a **data boundary**. Slice 1 delivers the keystone safety property:
**agent writes land in a separate Postgres as deduped `pending` candidates, never touching
mori's `memories` table.** Promotion, embeddings, trust-decay, and the provider repoint are
later slices (scoped at the bottom).

## Ground truth from the mori repo (verified, with file refs — do not re-assume)

- **Framework precedent**: `mori_advisor/ingestion_server.py` is a standalone **FastAPI + uvicorn**
  service on its own port — mirror this pattern for the intake service. (The MCP server in
  `main.py` is FastMCP; we do NOT use FastMCP here — intake is a plain HTTP ingest service.)
- **Auth**: reuse `mori_advisor.auth.check_key(provided) -> name|None` and
  `mori_advisor.policy` (roles `read<write<dreamer`, `ROLE_LEVELS`). Same `MORI_API_KEYS` /
  `MORI_API_KEY_ROLES` registry — agents already hold **write**-role keys. Header is `X-Api-Key`.
  `check_key` does `hmac.compare_digest(provided, secret)` against the **bare secret** (never
  `name:secret`). Intake requires role **>= write**.
- **Postgres**: `asyncpg` (see `mori_advisor/store/postgres_store.py:264-283`) —
  `asyncpg.create_pool(dsn, min_size=2, max_size=10, statement_cache_size=0, ssl=False)`.
- **Migrations**: mori uses an ordered migration registry (`mori_advisor/store/migrations.py`,
  `apply_postgres`), **not** bare `CREATE TABLE IF NOT EXISTS`. The intake service MUST use a
  migration registry too. (Rationale: the v0.2.0 provider outbox shipped `CREATE IF NOT EXISTS`
  with no migration and silently failed to add an `op` column on upgrade — `project-mori-provider-schema-migration-risk`. Do not repeat it.)
- **CORRECTED INVARIANT — read carefully.** The design doc claims intake must hash with "the
  IDENTICAL normalisation mori canon uses (NFKC + whitespace collapse)." **This premise is
  false.** mori canon computes `sha256(body.strip())[:16]` at `main.py:2733` and that hash is
  **audit-only** (logged, never compared for dedup). There is nothing in canon to match. Intake
  therefore defines its **own** self-consistent normalisation, documented below as the cross-
  system hash contract. NFKC+ws-collapse is the right independent choice; we are not mirroring
  mori, and the provider's reconcile is internally consistent regardless.
- **Name collision**: mori already has `ingestion_server.py`, `ingestion.py`, and an
  `ingestion_log` table (bulk memory import — unrelated). This service is namespaced **intake**
  everywhere (`mori_intake/`, `/intake/...`, `intake_*` tables) to avoid confusion.
- **Tests**: `tests/` + a one-line `conftest.py` (temp dir, pops `MORI_DATABASE_URL`). No
  `requires_pg` marker today; Postgres tests run when a DSN env is set (CI provides one).

## Placement & process

- New package: **`mori_intake/`** (sibling to `mori_advisor/`).
  - `mori_intake/__init__.py`
  - `mori_intake/app.py` — FastAPI app factory + routes (mirror `ingestion_server.py` shape).
  - `mori_intake/__main__.py` — `uvicorn.run(...)` entry (`python -m mori_intake`).
  - `mori_intake/db.py` — asyncpg pool create/close; reads `MORI_INTAKE_DATABASE_URL`.
  - `mori_intake/migrations.py` — ordered migration registry + `apply(pool)`; baseline = the DDL below.
  - `mori_intake/normalize.py` — the hash contract (pure, no I/O).
  - `mori_intake/eligibility.py` — the gate (pure, no I/O).
  - `mori_intake/worker.py` — the async drain worker.
  - `mori_intake/config.py` — env reads in one place.
- Port: `MORI_INTAKE_PORT` (default **8971**). DB: `MORI_INTAKE_DATABASE_URL` (asyncpg DSN).
  **Guard**: if `MORI_INTAKE_DATABASE_URL == MORI_DATABASE_URL`, refuse to start (log + exit) —
  enforces the data boundary at the config layer.
- Open (no-auth) paths: `/health`, `/ready`. Everything under `/intake/` requires write role.

## Hash contract (`normalize.py`) — the cross-system invariant

```python
import hashlib, unicodedata

def canonical_body(text: str) -> str:
    """Deterministic canonical form for dedup. NFKC, strip, collapse internal whitespace."""
    return " ".join(unicodedata.normalize("NFKC", text).split())

def content_hash(text: str) -> str:
    """Hex SHA-256 of the canonical body. Stable across processes/languages."""
    return hashlib.sha256(canonical_body(text).encode("utf-8")).hexdigest()
```

- Store the **full 64-char hex** in `intake_candidates.content_hash` (the DDL uses `bytea`; either
  store `digest()` bytes or switch the column to `text` hex — pick one, document it, be consistent).
  Recommended: column `text`, store hex — simpler to debug, no encoding ambiguity.
- This function is the single source of truth. Any future provider-side hash MUST import/replicate it.

## Eligibility gate (`eligibility.py`) — default-deny, pure function

Signature: `evaluate(target: str, action: str, stable_key: str, body: str) -> Decision`
where `Decision = (eligible: bool, reason: str)`. Policy (from `consult-b1000adc9f89`):

1. **Namespace gate** on `target` + `stable_key` prefix:
   - `target == "memory"`: allow `learned-*`, `fact-*`. Reject `session-*`, `scratch-*`, `temp-*`.
   - `target == "user"`: allow ONLY `preference-*`, `accessibility-*`. Reject everything else
     (never accept inferred `psychology-*`/`health-*`/`mood-*` — hard deny).
   - Unknown prefix → **deny** (default-deny, reason `"namespace-not-allowlisted"`).
2. **Action gate**: `add`/`replace` proceed to the proposition check. `remove` is **never**
   auto-eligible → deny with reason `"retraction-requires-human"` (Slice 1 surfaces it; a later
   slice routes it to a human-review flag).
3. **Proposition classifier** (cheap heuristic, NOT an LLM in Slice 1): reject if body is empty,
   `< 12` non-whitespace chars, ends with `?` (question), or is a single bare imperative
   fragment (`< 3` whitespace-separated tokens). Accept otherwise. Reason on reject:
   `"not-a-proposition"`. Keep the heuristic in one place, easy to swap for a classifier later.

Return the reason on every deny so the endpoint can echo it (422) and the worker never sees
ineligible rows. **All policy is enforced server-side** — never trust an agent-supplied "this is
durable" flag (the invented `durability` contract is why; do not reintroduce it).

## Endpoint contract (FROZEN — the provider repoint depends on this)

### `POST /intake/submissions`  (requires `X-Api-Key`, write role)

Request JSON:
```json
{
  "session_id": "str (required)",
  "agent_id":   "str (required, e.g. 'hermes')",
  "target":     "memory | user (required)",
  "action":     "add | replace | remove (required)",
  "stable_key": "str (required, namespaced e.g. 'learned-...' — drives eligibility + idempotency)",
  "content":    "str (required for add/replace; the raw learning text)",
  "provenance": { "free": "jsonb object, optional (source turn, model, ts, etc.)" }
}
```

Responses:
- **202 Accepted** `{ "status": "accepted", "submission_id": "<uuid>", "duplicate": false }` —
  eligible, inserted into `intake_submissions`. On idempotency hit (existing
  `(session_id, stable_key)`) return 202 with `"duplicate": true` and the existing id (idempotent,
  not an error).
- **422 Unprocessable** `{ "status": "rejected", "reason": "<eligibility reason>" }` — failed the gate.
- **400** malformed/missing fields. **401** bad/absent key. **403** key role < write.
- The handler does **validate + eligibility + insert only**. It does NOT hash, embed, dedup, or
  touch candidates — that's the worker. Return 202 fast.

### `GET /intake/candidates`  (requires write role)  — minimal read for verification
Query: `?status=pending&limit=50`. Returns `[{id, canonicalized_body, content_hash, status,
reinforcement_count, created_at, promoted_canon_name}]`. Read-only; for dreamer/operator
inspection and the live milestone check. (No agent-facing read API by design — agents read mori
canon, not intake.)

### `GET /health` → `{"status":"ok"}`. `GET /ready` → 200 iff the pool connects + migrations applied.

## Schema (`migrations.py`) — Slice 1 baseline

Use the five-table DDL from `consult-cfaebbeeffb8` **with these Slice-1 simplifications**:
- `pgvector` is **optional**. At migration time, attempt `CREATE EXTENSION IF NOT EXISTS vector`;
  if it fails (no pgvector), **skip** the `embedding` column and the HNSW index and set a runtime
  flag `EMBEDDINGS_ENABLED = False`. Slice 1 runs **hash-only dedup** regardless. Do NOT make the
  service hard-depend on pgvector or sentence-transformers.
- Keep `intake_submissions`, `intake_candidates`, `intake_corroborations`, `promotion_queue`,
  `intake_promotion_map` all created (full schema, so later slices need no migration churn) — but
  Slice 1 only WRITES to submissions, candidates, corroborations. `promotion_queue` /
  `intake_promotion_map` stay empty until the promotion slice.
- `content_hash`: store as **`text`** (hex), not `bytea` (see hash contract note). Unique on
  `intake_candidates.content_hash`. Plain btree index is enough for Slice 1 (no HNSW without pgvector).
- No declarative partitioning in Slice 1.
- Migration registry pattern: an ordered list `[(id, sql_or_callable), ...]`, an
  `intake_schema_migrations(id PK, applied_at)` ledger, advisory-lock around apply (mirror
  `mori_advisor/store/migrations.py`). Idempotent + forward-only.

## Worker (`worker.py`) — single async drain loop (Slice 1)

- One in-process `asyncio` task started on app startup (FastAPI lifespan), stopped on shutdown.
- **Drain query** (idempotent, no extra "processed" column needed): submissions with **no**
  corroboration row yet —
  `SELECT s.* FROM intake_submissions s LEFT JOIN intake_corroborations c ON c.submission_id = s.id WHERE c.id IS NULL ORDER BY s.received_at LIMIT N`.
- Per submission, in one transaction:
  1. `h = content_hash(s.raw_source_text)`; `body = canonical_body(s.raw_source_text)`.
  2. `SELECT id FROM intake_candidates WHERE content_hash = h`:
     - **hit** → `UPDATE ... SET reinforcement_count = reinforcement_count + 1, updated_at = now()`;
       use that candidate id.
     - **miss** → `INSERT INTO intake_candidates (canonicalized_body, content_hash, status)
       VALUES (body, h, 'pending')` → new candidate id.
  3. `INSERT INTO intake_corroborations (candidate_id, submission_id, agent_id, source_weight)
     VALUES (..., 1.0) ON CONFLICT (candidate_id, submission_id) DO NOTHING`. This both records the
     trust ledger AND marks the submission drained (the LEFT JOIN excludes it next pass).
- Poll interval env `MORI_INTAKE_WORKER_INTERVAL` (default 2s). Errors: log + continue (one bad
  row must not stall the loop); a row that errors stays undrained and is retried next pass — keep a
  per-row attempt cap (log-and-skip after N) so a poison row can't hot-loop.
- Worker computes NO embeddings in Slice 1 (`EMBEDDINGS_ENABLED` reserved for the next slice).

## Tests (`tests/test_intake_*.py`)

- **Pure-logic, no DB (always run)**:
  - `normalize`: NFKC folds (e.g. `"ﬁ"`→`"fi"`), whitespace collapse, stable hash, hash equality
    across equivalent-but-differently-spaced inputs.
  - `eligibility`: table-driven — `learned-*`/`fact-*` accept; `session-*`/`scratch-*`/unknown
    deny; `user` accepts only `preference-*`/`accessibility-*` and hard-denies `health-*`;
    `remove` denies; question/short/fragment bodies deny; valid proposition accepts.
- **Postgres integration (gated)**: skip with a clear message unless
  `MORI_INTAKE_TEST_DATABASE_URL` is set (mirror how mori gates PG). Cover: migrations apply
  idempotently (run twice); POST eligible → 202 + a submissions row; POST ineligible → 422 + NO
  row; duplicate `(session_id, stable_key)` → 202 `duplicate:true`, one row; worker drains a
  submission → creates a `pending` candidate + a corroboration; a second submission with identical
  body → reinforcement_count==2, still one candidate; `GET /intake/candidates?status=pending`
  returns it. Deterministic — no real sleeps (drive the worker step directly, don't wait on the loop).
- Add `pytest`/`ruff` clean. Match repo style; `ruff check` must pass (there's a pre-commit
  ruff-format hook — run `ruff format` before committing or the commit is silently blocked,
  `workflow-git-pre-commit-silent-block`).

## What Slice 1 deliberately does NOT build (later slices)

- **Promotion seam** (Slice 2): dream evaluates candidates → writes `promotion_queue`; a single
  mori-side canon writer polls (`FOR UPDATE SKIP LOCKED`), inserts canon + a new mori
  `memory_intake_lineage` row, populates `origin_clients`, writes `intake_promotion_map`,
  at-least-once + idempotent (check the map before re-insert). Tables already exist from Slice 1.
- **Embeddings + similarity dedup** (Slice 2): pgvector, embedding compute in the worker, HNSW/
  IVFFlat near-neighbour → reinforce instead of new candidate above a similarity threshold (>0.92).
- **Trust curve + decay** (Slice 3): asymmetric promotion (agent starts pending, climbs steeper),
  `TierDecayScheduler` (unread/unreinforced 30d → demote), consent bit for `target=user`,
  PII quarantine, tombstone/retraction authority.
- **Provider repoint** (separate task, after this contract is merged): point
  `hermes-mori-provider`'s outbox drain at `POST /intake/submissions` instead of mori's
  `/api/memories`. LWM + outbox machinery unchanged; only the endpoint + payload shape change to
  the contract above. **This is the clean hand-off seam** — droppable into a parallel session.
- **Retention/partitioning cron** (Slice 3): nightly delete of old submissions + rejected/decayed
  candidates + vacuum; monthly partitioning of submissions.

## Definition of done (Slice 1)

`ruff check` + `ruff format` clean; pure-logic tests green unconditionally; PG integration tests
green against a throwaway Postgres; `python -m mori_intake` boots, `/ready` goes 200 after
migrations; a `curl` POST of an eligible learning returns 202 and, after the worker ticks, shows as
a `pending` candidate via `GET /intake/candidates`; an ineligible one returns 422 with a reason and
creates no row; the service refuses to start if pointed at mori's own DB. Reviewer (Opus) reads the
diff before any commit; `git` commit on `feat/intake-service` only — **no push**.
