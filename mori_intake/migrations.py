"""Schema migration registry for mori-intake.

Mirrors the pattern from mori_advisor/store/migrations.py:
    - Ordered list of (id, name, sql) tuples
    - An ``intake_schema_migrations`` ledger table (PK = id, double-apply impossible)
    - An advisory lock around the apply run so concurrent startups are serialised
    - Forward-only, idempotent

All five Slice-1 tables are created here so later slices need no migration churn.
Only ``intake_submissions``, ``intake_candidates``, and ``intake_corroborations``
are written to by Slice 1; ``promotion_queue`` and ``intake_promotion_map`` remain
empty until the promotion slice.

pgvector is OPTIONAL.  The baseline migration attempts
``CREATE EXTENSION IF NOT EXISTS vector``; if the extension is absent the failure
is caught, a runtime flag ``EMBEDDINGS_ENABLED`` is set to ``False``, and the
``embedding`` column + HNSW index are skipped.  The service runs hash-only dedup.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Shared across concurrent mori-intake instances so only one applies migrations.
# Distinct from mori's own advisory-lock key (0x4D4F524921) to avoid cross-service
# interference if both services share the same Postgres cluster.
_PG_ADVISORY_LOCK_KEY = 0x494E54414B  # "INTAK" as a bigint

# Set to False at runtime if the Postgres server lacks the pgvector extension.
EMBEDDINGS_ENABLED: bool = True

# ── Migration dataclass ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Migration:
    id: int
    name: str
    sql: str  # Postgres SQL; may be multi-statement (we execute via conn.execute)

    def checksum(self) -> str:
        body = f"{self.id}:{self.name}:{self.sql}"
        return hashlib.sha256(body.encode()).hexdigest()[:16]


# ── DDL ───────────────────────────────────────────────────────────────────────

# Ledger table — mirrors schema_migrations in mori but namespaced to intake.
_LEDGER_DDL = (
    "CREATE TABLE IF NOT EXISTS intake_schema_migrations ("
    "  id         BIGINT PRIMARY KEY,"
    "  name       TEXT NOT NULL,"
    "  checksum   TEXT,"
    "  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
    ")"
)

# Baseline DDL split into individual migration steps so each is atomic and
# stamped separately.  Migrations are ordered and forward-only.

_BASELINE_SUBMISSIONS = """
CREATE TABLE IF NOT EXISTS intake_submissions (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id       TEXT        NOT NULL,
    agent_id         TEXT        NOT NULL,
    target_name      TEXT        NOT NULL,
    action           TEXT        NOT NULL,
    stable_key       TEXT        NOT NULL,
    raw_source_text  TEXT        NOT NULL DEFAULT '',
    provenance       JSONB,
    received_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (session_id, stable_key)
)
"""

_BASELINE_CANDIDATES = """
CREATE TABLE IF NOT EXISTS intake_candidates (
    id                   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    canonicalized_body   TEXT        NOT NULL,
    content_hash         TEXT        NOT NULL,
    status               TEXT        NOT NULL DEFAULT 'pending',
    trust_score          REAL        NOT NULL DEFAULT 0.0,
    reinforcement_count  INTEGER     NOT NULL DEFAULT 1,
    decay_score          REAL        NOT NULL DEFAULT 0.0,
    promoted_canon_name  TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (content_hash)
)
"""

_BASELINE_CORROBORATIONS = """
CREATE TABLE IF NOT EXISTS intake_corroborations (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id   UUID        NOT NULL REFERENCES intake_candidates (id) ON DELETE CASCADE,
    submission_id  UUID        NOT NULL REFERENCES intake_submissions (id) ON DELETE CASCADE,
    agent_id       TEXT        NOT NULL,
    source_weight  REAL        NOT NULL DEFAULT 1.0,
    recorded_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (candidate_id, submission_id)
)
"""

_BASELINE_PROMOTION_QUEUE = """
CREATE TABLE IF NOT EXISTS promotion_queue (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id  UUID        NOT NULL REFERENCES intake_candidates (id) ON DELETE CASCADE,
    status        TEXT        NOT NULL DEFAULT 'queued',
    canon_name    TEXT,
    attempt_count INTEGER     NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

_BASELINE_PROMOTION_MAP = """
CREATE TABLE IF NOT EXISTS intake_promotion_map (
    canon_name          TEXT        PRIMARY KEY,
    candidate_id        UUID        NOT NULL REFERENCES intake_candidates (id),
    submission_ids      UUID[]      NOT NULL DEFAULT '{}',
    provenance_snapshot JSONB,
    promoted_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

# Index on content_hash for fast dedup look-ups (plain btree — no pgvector in Slice 1).
_INDEX_CONTENT_HASH = (
    "CREATE INDEX IF NOT EXISTS idx_intake_candidates_content_hash "
    "ON intake_candidates (content_hash)"
)

# ── Migration registry ────────────────────────────────────────────────────────

MIGRATIONS: tuple[_Migration, ...] = (
    _Migration(id=1, name="intake_submissions", sql=_BASELINE_SUBMISSIONS.strip()),
    _Migration(id=2, name="intake_candidates", sql=_BASELINE_CANDIDATES.strip()),
    _Migration(id=3, name="intake_corroborations", sql=_BASELINE_CORROBORATIONS.strip()),
    _Migration(id=4, name="promotion_queue", sql=_BASELINE_PROMOTION_QUEUE.strip()),
    _Migration(id=5, name="intake_promotion_map", sql=_BASELINE_PROMOTION_MAP.strip()),
    _Migration(id=6, name="idx_content_hash", sql=_INDEX_CONTENT_HASH.strip()),
    # Migration 7 — add error_message to promotion_queue (needed by the canon
    # writer to record retry diagnostics without losing the queued row).
    # Additive: existing rows remain unaffected (column is nullable).
    _Migration(
        id=7,
        name="promotion_queue_error_message",
        sql=("ALTER TABLE promotion_queue ADD COLUMN IF NOT EXISTS error_message TEXT"),
    ),
    # Migration 8 — add rejection columns to intake_candidates so the assessor
    # can record which candidate was rejected and why.  Both columns already
    # referenced in the assessor spec; adding them here keeps the schema explicit
    # and avoids the "silent failure on upgrade" anti-pattern.
    # Additive: existing rows keep NULL values for both columns.
    _Migration(
        id=8,
        name="intake_candidates_rejection_columns",
        sql=(
            "ALTER TABLE intake_candidates "
            "ADD COLUMN IF NOT EXISTS rejection_reason TEXT; "
            "ALTER TABLE intake_candidates "
            "ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0"
        ),
    ),
)


# ── apply() ──────────────────────────────────────────────────────────────────


async def apply(pool) -> None:
    """Apply all pending migrations to the intake Postgres.

    Holds ONE dedicated connection for the entire run — pg_advisory_lock is
    session-scoped, so returning the connection to the pool between migrations
    could silently drop the lock.

    Attempts CREATE EXTENSION IF NOT EXISTS vector; if the extension is absent
    sets EMBEDDINGS_ENABLED = False and continues (hash-only dedup).
    """
    global EMBEDDINGS_ENABLED

    conn = await pool.acquire()
    try:
        if await conn.fetchval("SELECT pg_is_in_recovery()"):
            logger.info("intake: connected to read-only standby — skipping migrations")
            return

        # Attempt to enable pgvector (optional).
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            logger.info("intake: pgvector extension available — embeddings enabled")
        except Exception as exc:
            logger.warning(
                "intake: pgvector not available (%s) — running hash-only dedup. "
                "EMBEDDINGS_ENABLED=False",
                exc,
            )
            EMBEDDINGS_ENABLED = False

        # Ensure the ledger table exists.
        await conn.execute(_LEDGER_DDL)

        # Acquire advisory lock to serialise concurrent startups.
        await conn.execute("SELECT pg_advisory_lock($1)", _PG_ADVISORY_LOCK_KEY)
        try:
            applied = await _applied_ids(conn)
            for m in sorted(MIGRATIONS, key=lambda x: x.id):
                if m.id in applied:
                    continue
                await _apply_one(conn, m)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _PG_ADVISORY_LOCK_KEY)

    finally:
        await pool.release(conn)


async def _applied_ids(conn) -> set[int]:
    rows = await conn.fetch("SELECT id, name, checksum FROM intake_schema_migrations")
    by_id = {m.id: m for m in MIGRATIONS}
    for r in rows:
        m = by_id.get(r["id"])
        if m and r["checksum"] and r["checksum"] != m.checksum():
            logger.warning(
                "intake migration %d (%s) checksum drift: recorded=%s current=%s — "
                "an applied migration was edited",
                r["id"],
                r["name"],
                r["checksum"],
                m.checksum(),
            )
    return {r["id"] for r in rows}


async def _apply_one(conn, m: _Migration) -> None:
    t0 = time.monotonic()
    async with conn.transaction():
        await conn.execute(m.sql)
        await conn.execute(
            "INSERT INTO intake_schema_migrations (id, name, checksum) "
            "VALUES ($1, $2, $3) ON CONFLICT (id) DO NOTHING",
            m.id,
            m.name,
            m.checksum(),
        )
    logger.info("intake migration %d (%s) applied in %.3fs", m.id, m.name, time.monotonic() - t0)
