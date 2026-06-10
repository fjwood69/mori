"""Schema migration runner — the single source of truth for mori's DB schema.

One ordered ``MIGRATIONS`` registry drives BOTH backends:

  * SQLite (sync, ``sqlite3``)   via :func:`apply_sqlite`
  * Postgres (async, ``asyncpg``) via :func:`apply_postgres`

A migration is *data*, not code: a tuple of SQLite statements and/or a Postgres
SQL string, plus optional backfill callables for the rare imperative step. The
applied version is recorded in a ``schema_migrations`` table (PK = migration id,
so double-application is impossible at the DB level).

Migration ``1`` ("baseline") does NOT carry copied DDL — it *invokes the existing
legacy bootstrap code* (``MemoryStore.bootstrap_schema`` etc. / the Postgres
``_DDL`` constant). The baseline is therefore byte-identical to whatever shipped
before the runner existed: a fresh DB and a populated production DB converge to
the same state (everything is ``IF NOT EXISTS`` / guarded), then both get stamped.
This avoids any synthetic-DDL transcription drift.

Rules for every NEW migration (convention, not enforced):
  * Forward-only and ADDITIVE — no ``DROP`` / ``RENAME`` — so a plain code
    rollback (redeploy the previous image) is always safe; old code simply
    ignores the new table/column.
  * Parameterised steps must be a single statement or use ``*_fn`` (asyncpg
    rejects multi-statement + params; on SQLite we run statements individually
    rather than via ``executescript``, whose implicit COMMIT would break the
    surrounding transaction).
  * A step that cannot run inside a transaction (PG ``CREATE INDEX CONCURRENTLY``,
    ``ADD COLUMN ... GENERATED``) sets ``transactional=False``.

SQLite uses TWO files (``memories.db`` + ``msg.db``); ``Migration.target`` selects
the file, and each file gets its own ``schema_migrations`` table. Postgres keeps
everything in one database, so it applies every migration regardless of target —
which is why migration ids must be globally unique.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Arbitrary stable key for pg_advisory_lock — serialises concurrent bootstraps
# (main server + ingestion server + dream_job can all start near-simultaneously).
_PG_ADVISORY_LOCK_KEY = 0x4D4F524921  # "MORI!" as a bigint

# Per-file locks serialise apply_sqlite across THREADS in this process (deterministic,
# no SQLite write contention). Cross-PROCESS contention is handled by busy_timeout +
# the retry-on-locked loop inside apply_sqlite.
_SQLITE_APPLY_LOCKS: dict[str, threading.Lock] = {}
_SQLITE_APPLY_LOCKS_GUARD = threading.Lock()


def _apply_lock_for(db_path: Path) -> threading.Lock:
    key = str(db_path.resolve())
    with _SQLITE_APPLY_LOCKS_GUARD:
        lock = _SQLITE_APPLY_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _SQLITE_APPLY_LOCKS[key] = lock
        return lock


# Callable signatures. sqlite_fn gets (conn, db_path) so backfills can use the
# runner connection while the baseline can open the legacy bootstrappers' own
# connections via db_path. postgres_fn gets the dedicated asyncpg connection.
SqliteFn = Callable[[sqlite3.Connection, Path], None]
PostgresFn = Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class Migration:
    id: int
    name: str
    sqlite_sql: Optional[tuple[str, ...]] = None
    postgres_sql: Optional[str] = None
    sqlite_fn: Optional[SqliteFn] = None
    postgres_fn: Optional[PostgresFn] = None
    target: str = "memories"  # "memories" | "msg" — SQLite file selector; PG ignores
    transactional: bool = True

    def checksum(self, dialect: str) -> str:
        """Stable hash of this migration's definition for the given dialect.

        Used only to warn if an already-applied migration's text later changes.
        Function-based steps hash by name (their behaviour isn't a string).
        """
        if dialect == "sqlite":
            body = "\n".join(self.sqlite_sql) if self.sqlite_sql else f"fn:{self.name}"
        else:
            body = self.postgres_sql or f"fn:{self.name}"
        return hashlib.sha256(f"{self.id}:{self.name}:{body}".encode()).hexdigest()[:16]


# ──────────────────────────────────────────────────────────────────────────────
# Migration 1 — baseline: invoke the existing proven bootstrap, then stamp.
# ──────────────────────────────────────────────────────────────────────────────


def _baseline_sqlite_memories(conn: sqlite3.Connection, db_path: Path) -> None:
    # Run the existing, proven bootstrap for the memories.db tables verbatim.
    # These open their own short-lived connections to the same file — which is
    # why the baseline migration is transactional=False (the runner must NOT be
    # holding a write lock while the legacy code writes through another handle).
    from mori_advisor.memory_store import MemoryStore
    from mori_advisor.session_log import SessionLog

    MemoryStore.bootstrap_schema(db_path)
    SessionLog.bootstrap_schema(db_path)


def _baseline_sqlite_msg(conn: sqlite3.Connection, db_path: Path) -> None:
    # MsgStore.__init__ bootstraps msg.db; constructing it is the bootstrap.
    from mori_advisor.msg_store import MsgStore

    MsgStore(db_path)


async def _baseline_postgres(conn) -> None:
    # The existing _DDL constant creates every Postgres table (including msg_log
    # and delegate_tasks) — already fully IF NOT EXISTS guarded. Deferred import
    # so SQLite-only deployments never import postgres_store/asyncpg.
    from mori_advisor.store.postgres_store import _DDL

    await conn.execute(_DDL)


def _fts_sqlite(conn: sqlite3.Connection, db_path: Path) -> None:
    # External-content FTS5 over memories(name,title,description,body). The index
    # references memories by rowid=id (no duplicated body). Triggers keep it in
    # sync on every INSERT/UPDATE/DELETE — so write()/delete() need no changes.
    # porter+unicode61 gives stemming, aligning with Postgres' 'english' config.
    # If FTS5 isn't compiled into this SQLite build, stamp anyway and let search()
    # fall back to LIKE (probed via the memories_fts table's existence).
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5("
            "name, title, description, body, "
            "content='memories', content_rowid='id', tokenize='porter unicode61')"
        )
    except sqlite3.OperationalError as e:
        if "fts5" in str(e).lower() or "no such module" in str(e).lower():
            logger.warning("FTS5 unavailable in this SQLite build — search() uses LIKE fallback")
            return
        raise
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS memories_fts_ai AFTER INSERT ON memories BEGIN "
        "INSERT INTO memories_fts(rowid, name, title, description, body) "
        "VALUES (new.id, new.name, new.title, new.description, new.body); END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS memories_fts_ad AFTER DELETE ON memories BEGIN "
        "INSERT INTO memories_fts(memories_fts, rowid, name, title, description, body) "
        "VALUES ('delete', old.id, old.name, old.title, old.description, old.body); END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS memories_fts_au AFTER UPDATE ON memories BEGIN "
        "INSERT INTO memories_fts(memories_fts, rowid, name, title, description, body) "
        "VALUES ('delete', old.id, old.name, old.title, old.description, old.body); "
        "INSERT INTO memories_fts(rowid, name, title, description, body) "
        "VALUES (new.id, new.name, new.title, new.description, new.body); END"
    )
    # Backfill / reconcile from the content table — idempotent FTS5 'rebuild'.
    conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")


def _soft_delete_sqlite(conn: sqlite3.Connection, db_path: Path) -> None:
    """Migration 9 — soft-delete: add deleted_at + replace inline UNIQUE(name).

    SQLite has no DROP CONSTRAINT, so we recreate the memories table without the
    inline UNIQUE(name) and add deleted_at TEXT (NULL = active).  A partial unique
    index replaces the old constraint.  The runner holds BEGIN IMMEDIATE before
    calling us, so the entire sequence is atomic — crash-safe.

    FTS5 triggers must be rebuilt after the rename; the old triggers reference the
    table by name and are dropped with the original table.
    """
    cur = conn.execute("PRAGMA table_info(memories)")
    existing_cols = {row[1] for row in cur.fetchall()}
    if "deleted_at" in existing_cols:
        return  # idempotent — already migrated

    # 1. Drop FTS triggers + virtual table before dropping memories.
    for trig in ("memories_fts_ai", "memories_fts_ad", "memories_fts_au"):
        conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
    conn.execute("DROP TABLE IF EXISTS memories_fts")

    # 2. Create replacement table: identical schema, no inline UNIQUE(name), + deleted_at.
    conn.execute(
        "CREATE TABLE memories_new ("
        "  id                   INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  name                 TEXT NOT NULL,"
        "  title                TEXT NOT NULL DEFAULT '',"
        "  description          TEXT NOT NULL DEFAULT '',"
        "  type                 TEXT NOT NULL DEFAULT 'project',"
        "  body                 TEXT NOT NULL DEFAULT '',"
        "  tags                 TEXT NOT NULL DEFAULT '[]',"
        "  origin_session_id    TEXT,"
        "  created_at           TEXT NOT NULL DEFAULT (datetime('now')),"
        "  updated_at           TEXT NOT NULL DEFAULT (datetime('now')),"
        "  origin_session_ids   TEXT NOT NULL DEFAULT '[]',"
        "  origin_clients       TEXT NOT NULL DEFAULT '[]',"
        "  protected            INTEGER NOT NULL DEFAULT 0,"
        "  protected_domains    TEXT NOT NULL DEFAULT '[]',"
        "  tier                 TEXT NOT NULL DEFAULT 'working',"
        "  last_retrieved_at    TEXT,"
        "  retrieval_count      INTEGER NOT NULL DEFAULT 0,"
        "  freshness_status     TEXT NOT NULL DEFAULT 'unknown',"
        "  freshness_checked_at TEXT,"
        "  superseded_by        TEXT,"
        "  deleted_at           TEXT"
        ")"
    )

    # 3. Copy rows — only columns that exist in both tables (handles schema drift).
    new_cols = {row[1] for row in conn.execute("PRAGMA table_info(memories_new)").fetchall()}
    common = [c for c in existing_cols if c in new_cols]
    col_list = ", ".join(common)
    conn.execute(f"INSERT INTO memories_new ({col_list}) SELECT {col_list} FROM memories")

    # 4. Swap tables.
    conn.execute("DROP TABLE memories")
    conn.execute("ALTER TABLE memories_new RENAME TO memories")

    # 5. Recreate indexes.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_name_active "
        "ON memories (name) WHERE deleted_at IS NULL"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_updated_at ON memories (updated_at)")

    # 6. Recreate FTS5 virtual table + triggers + index rebuild (mirrors migration 6).
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5("
            "name, title, description, body, "
            "content='memories', content_rowid='id', tokenize='porter unicode61')"
        )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS memories_fts_ai AFTER INSERT ON memories BEGIN "
            "INSERT INTO memories_fts(rowid, name, title, description, body) "
            "VALUES (new.id, new.name, new.title, new.description, new.body); END"
        )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS memories_fts_ad AFTER DELETE ON memories BEGIN "
            "INSERT INTO memories_fts(memories_fts, rowid, name, title, description, body) "
            "VALUES ('delete', old.id, old.name, old.title, old.description, old.body); END"
        )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS memories_fts_au AFTER UPDATE ON memories BEGIN "
            "INSERT INTO memories_fts(memories_fts, rowid, name, title, description, body) "
            "VALUES ('delete', old.id, old.name, old.title, old.description, old.body); "
            "INSERT INTO memories_fts(rowid, name, title, description, body) "
            "VALUES (new.id, new.name, new.title, new.description, new.body); END"
        )
        conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
    except sqlite3.OperationalError as exc:
        if "fts5" in str(exc).lower() or "no such module" in str(exc).lower():
            logger.warning("FTS5 unavailable — search uses LIKE fallback after migration 9")
        else:
            raise


def _pending_writes_td_sqlite(conn: sqlite3.Connection, db_path: Path) -> None:
    """Add TD-enrichment columns to pending_writes (SQLite).

    Each column is guarded: ALTER TABLE only runs if the column is absent,
    so this function is safe to call multiple times (idempotent). Existing
    pending rows keep their NULL values for the new columns — backward compat.

    Also adds a UNIQUE index on (memory_name) for pending-only rows so that
    queue_pending_write can use INSERT OR REPLACE to suppress duplicates.
    The index is partial (WHERE status='pending') so resolved rows remain.
    """
    cur = conn.execute("PRAGMA table_info(pending_writes)")
    existing_cols = {row[1] for row in cur.fetchall()}

    new_cols = [
        ("source", "TEXT"),
        ("provenance", "TEXT"),
        ("confidence", "REAL"),
        ("focus_mode", "TEXT"),
        ("existing_body", "TEXT"),
        ("tier", "TEXT"),
        ("created_at", "TEXT DEFAULT (datetime('now'))"),
    ]
    for col_name, col_def in new_cols:
        if col_name not in existing_cols:
            conn.execute(f"ALTER TABLE pending_writes ADD COLUMN {col_name} {col_def}")

    # Partial unique index for duplicate-suppression in queue_pending_write.
    # Idempotent: CREATE INDEX IF NOT EXISTS.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_writes_name_pending "
        "ON pending_writes (memory_name) WHERE status = 'pending'"
    )


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        id=1,
        name="baseline",
        target="memories",
        transactional=False,  # legacy bootstrap manages its own connections/txns
        sqlite_fn=_baseline_sqlite_memories,
        postgres_fn=_baseline_postgres,
    ),
    Migration(
        id=2,
        name="baseline_msg",
        target="msg",
        transactional=False,
        sqlite_fn=_baseline_sqlite_msg,
        # Postgres: msg_log is already created by migration 1's _DDL → records-only.
    ),
    # ── Stage B: drift fixes (bring the two backends back into parity) ──────
    Migration(
        id=3,
        name="dreamer_config_updated_at",
        target="memories",
        # Postgres already has dreamer_config.updated_at (in _DDL) → records-only.
        # SQLite ADD COLUMN defaults must be constant (no datetime('now')), so add
        # with a constant default and backfill existing rows.
        sqlite_sql=(
            "ALTER TABLE dreamer_config ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''",
            "UPDATE dreamer_config SET updated_at = datetime('now') WHERE updated_at = ''",
        ),
    ),
    Migration(
        id=4,
        name="delegate_tasks_sqlite_parity",
        target="memories",
        # Postgres already has delegate_tasks (in _DDL) → records-only. Create the
        # SQLite analogue (TEXT timestamps) so the delegate feature can persist on
        # the solo/dev backend too.
        sqlite_sql=(
            """CREATE TABLE IF NOT EXISTS delegate_tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id     TEXT NOT NULL UNIQUE,
                from_host   TEXT NOT NULL,
                to_host     TEXT NOT NULL,
                description TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending',
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
                result      TEXT
            )""",
        ),
    ),
    Migration(
        id=5,
        name="freshness_status_not_null",
        target="memories",
        # SQLite already has freshness_status NOT NULL DEFAULT 'unknown' → records-only.
        # Bring Postgres to the same contract: backfill NULLs first, then constrain.
        postgres_sql=(
            "UPDATE memories SET freshness_status = 'unknown' WHERE freshness_status IS NULL; "
            "ALTER TABLE memories ALTER COLUMN freshness_status SET DEFAULT 'unknown'; "
            "ALTER TABLE memories ALTER COLUMN freshness_status SET NOT NULL"
        ),
    ),
    # ── Stage C: full-text search ──────────────────────────────────────────
    Migration(
        id=6,
        name="fts_memories",
        target="memories",
        # SQLite: external-content FTS5 + triggers (see _fts_sqlite). Postgres:
        # a generated STORED tsvector with weighted lexemes (name/title > desc >
        # body) + GIN. COALESCE every column — strict concat of a NULL yields NULL
        # → an empty tsvector → an invisible row. The column add rewrites the
        # (small) memories table under ACCESS EXCLUSIVE; schedule low-traffic.
        sqlite_fn=_fts_sqlite,
        postgres_sql=(
            "ALTER TABLE memories ADD COLUMN IF NOT EXISTS search_tsv tsvector "
            "GENERATED ALWAYS AS ("
            "setweight(to_tsvector('english', coalesce(name, '') || ' ' || coalesce(title, '')), 'A') || "
            "setweight(to_tsvector('english', coalesce(description, '')), 'B') || "
            "setweight(to_tsvector('english', coalesce(body, '')), 'D')"
            ") STORED; "
            "CREATE INDEX IF NOT EXISTS idx_memories_search_tsv ON memories USING GIN (search_tsv)"
        ),
    ),
    # ── Stage D: TD review queue enrichment (#15) ─────────────────────────
    Migration(
        id=7,
        name="pending_writes_td_enrichment",
        target="memories",
        # Add provenance / curation columns to pending_writes.
        # Each ALTER TABLE is guarded with a column-exists check in the
        # sqlite_fn so re-running (idempotency) is safe. Existing pending
        # rows are unaffected — all new columns are nullable.
        #
        # Postgres: ADD COLUMN IF NOT EXISTS is idempotent natively.
        # A PARTIAL unique index (memory_name WHERE status='pending') matches the
        # SQLite partial index: at most one pending proposal per name, while
        # approved/rejected rows stay unconstrained so a memory can be re-proposed
        # and re-approved over its life without a UNIQUE violation. The ON CONFLICT
        # in queue_pending_write() infers this partial index.
        sqlite_fn=_pending_writes_td_sqlite,
        postgres_sql=(
            "ALTER TABLE pending_writes ADD COLUMN IF NOT EXISTS source TEXT; "
            "ALTER TABLE pending_writes ADD COLUMN IF NOT EXISTS provenance TEXT; "
            "ALTER TABLE pending_writes ADD COLUMN IF NOT EXISTS confidence REAL; "
            "ALTER TABLE pending_writes ADD COLUMN IF NOT EXISTS focus_mode TEXT; "
            "ALTER TABLE pending_writes ADD COLUMN IF NOT EXISTS existing_body TEXT; "
            "ALTER TABLE pending_writes ADD COLUMN IF NOT EXISTS tier TEXT; "
            "ALTER TABLE pending_writes ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW(); "
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_writes_name_pending "
            "ON pending_writes (memory_name) WHERE status = 'pending'"
        ),
    ),
    # ── Stage E: persistent audit trail + soft-delete (#23 A+B) ──────────
    Migration(
        id=8,
        name="write_audit_table",
        target="memories",
        # Persistent audit trail for governed writes — closes the log-only gap.
        # Purely additive on both backends (CREATE TABLE IF NOT EXISTS).
        sqlite_sql=(
            "CREATE TABLE IF NOT EXISTS write_audit ("
            "  id             INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  ts             TEXT    NOT NULL DEFAULT (datetime('now')),"
            "  actor_key_name TEXT    NOT NULL DEFAULT '',"
            "  op             TEXT    NOT NULL,"
            "  memory_name    TEXT    NOT NULL DEFAULT '',"
            "  content_hash   TEXT    NOT NULL DEFAULT '',"
            "  detail         TEXT    NOT NULL DEFAULT ''"
            ")",
            "CREATE INDEX IF NOT EXISTS idx_write_audit_ts   ON write_audit (ts)",
            "CREATE INDEX IF NOT EXISTS idx_write_audit_name ON write_audit (memory_name)",
        ),
        postgres_sql=(
            "CREATE TABLE IF NOT EXISTS write_audit ("
            "  id             BIGSERIAL   PRIMARY KEY,"
            "  ts             TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            "  actor_key_name TEXT        NOT NULL DEFAULT '',"
            "  op             TEXT        NOT NULL,"
            "  memory_name    TEXT        NOT NULL DEFAULT '',"
            "  content_hash   TEXT        NOT NULL DEFAULT '',"
            "  detail         TEXT        NOT NULL DEFAULT ''"
            "); "
            "CREATE INDEX IF NOT EXISTS idx_write_audit_ts   ON write_audit (ts); "
            "CREATE INDEX IF NOT EXISTS idx_write_audit_name ON write_audit (memory_name)"
        ),
    ),
    Migration(
        id=9,
        name="soft_delete",
        target="memories",
        # Add deleted_at + replace inline UNIQUE(name) with a partial unique index
        # UNIQUE(name) WHERE deleted_at IS NULL on both backends.
        #
        # SQLite: table recreation required (no DROP CONSTRAINT).  The sqlite_fn
        # recreates memories without the inline UNIQUE, copies rows, swaps tables,
        # and rebuilds the partial unique index + FTS triggers.  Runner holds
        # BEGIN IMMEDIATE so the whole sequence is atomic.
        #
        # Postgres: ADD COLUMN + DROP CONSTRAINT + partial unique index in one
        # transaction (no CONCURRENTLY — table is small, atomicity wins).
        sqlite_fn=_soft_delete_sqlite,
        postgres_sql=(
            "ALTER TABLE memories ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ; "
            # DROP CASCADE: memory_versions_memory_name_fkey references memories_name_key
            # via the unique index. Partial indexes cannot be FK targets, so after we
            # replace the full UNIQUE with a partial unique index the old FK is no longer
            # valid anyway. hard_delete() now explicitly deletes memory_versions rows
            # before removing the memory row — preserving the same semantic (no orphans).
            "ALTER TABLE memories DROP CONSTRAINT IF EXISTS memories_name_key CASCADE; "
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_name_active "
            "ON memories (name) WHERE deleted_at IS NULL"
        ),
    ),
    # ── Stage F: agent-memory governance lineage (Stream B) ───────────────
    Migration(
        id=10,
        name="memory_intake_lineage",
        target="memories",
        # New mori-side table that records every canon memory that arrived via
        # the intake → promotion pipeline.  Purely additive — no existing
        # table or column is altered.
        #
        # SQLite: lightweight analogue (TEXT timestamps, no JSONB/arrays —
        # intake is Postgres-only in production, but the table must exist on
        # SQLite so mori servers that run SQLite can run migrations without
        # errors; the canon writer will only be called from a Postgres context).
        # trust_snapshot and intake_submission_ids are stored as JSON TEXT on
        # SQLite.
        #
        # Postgres: intake_candidate_id / intake_submission_ids are UUIDs;
        # trust_snapshot is JSONB; promoted_at is TIMESTAMPTZ.
        sqlite_sql=(
            "CREATE TABLE IF NOT EXISTS memory_intake_lineage ("
            "  canon_name             TEXT        PRIMARY KEY,"
            "  intake_candidate_id    TEXT        NOT NULL,"
            "  intake_submission_ids  TEXT        NOT NULL DEFAULT '[]',"
            "  trust_snapshot         TEXT        NOT NULL DEFAULT '{}',"
            "  promoted_at            TEXT        NOT NULL DEFAULT (datetime('now'))"
            ")",
        ),
        postgres_sql=(
            "CREATE TABLE IF NOT EXISTS memory_intake_lineage ("
            "  canon_name             VARCHAR(128) PRIMARY KEY,"
            "  intake_candidate_id    UUID         NOT NULL,"
            "  intake_submission_ids  UUID[]       NOT NULL DEFAULT '{}',"
            "  trust_snapshot         JSONB        NOT NULL DEFAULT '{}',"
            "  promoted_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW()"
            ")"
        ),
    ),
    # ── Stage G: human-review gate — bridge-owned promotion tickets ─────────
    Migration(
        id=11,
        name="intake_promotion_tickets",
        target="memories",
        # Mori-side table written ONLY by the CLI bridge (which holds both DSNs)
        # when it surfaces an assessed intake candidate into the dreamer review
        # queue. It is the TRUSTED carrier of the intake identifiers + the
        # body-hash across the boundary: mori-advisor's approve()/finalize never
        # trusts the pending_write.provenance JSON (forgeable) — it reads the
        # ids from here, keyed by the opaque ticket_uuid stored in provenance.
        # Purely additive.
        sqlite_sql=(
            "CREATE TABLE IF NOT EXISTS intake_promotion_tickets ("
            "  ticket_uuid    TEXT NOT NULL PRIMARY KEY,"
            "  canon_name     TEXT NOT NULL,"
            "  candidate_id   TEXT NOT NULL,"
            "  submission_ids TEXT NOT NULL DEFAULT '[]',"
            "  trust_snapshot TEXT NOT NULL DEFAULT '{}',"
            "  body_hash      TEXT NOT NULL,"
            "  created_at     TEXT NOT NULL DEFAULT (datetime('now'))"
            ")",
        ),
        postgres_sql=(
            "CREATE TABLE IF NOT EXISTS intake_promotion_tickets ("
            "  ticket_uuid    UUID         NOT NULL PRIMARY KEY,"
            "  canon_name     VARCHAR(128) NOT NULL,"
            "  candidate_id   UUID         NOT NULL,"
            "  submission_ids UUID[]       NOT NULL DEFAULT '{}',"
            "  trust_snapshot JSONB        NOT NULL DEFAULT '{}',"
            "  body_hash      TEXT         NOT NULL,"
            "  created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()"
            ")"
        ),
    ),
    Migration(
        id=12,
        name="ingestion_log_shape",
        # Ingest-shape instrument (measurement layer): candidate volume + the
        # convention/occurrence ratio + anchorability per ingest. SQLite is handled
        # by the legacy bootstrap's guarded ALTERs (memory_store); this covers
        # existing Postgres DBs (fresh PG gets the columns from _DDL). Additive, nullable.
        postgres_sql=(
            "ALTER TABLE ingestion_log ADD COLUMN IF NOT EXISTS candidates_total INTEGER; "
            "ALTER TABLE ingestion_log ADD COLUMN IF NOT EXISTS convention_ratio REAL; "
            "ALTER TABLE ingestion_log ADD COLUMN IF NOT EXISTS anchorable_pct REAL"
        ),
    ),
)


# ──────────────────────────────────────────────────────────────────────────────
# SQLite engine (sync)
# ──────────────────────────────────────────────────────────────────────────────

_SQLITE_SCHEMA_MIGRATIONS = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "  id         INTEGER PRIMARY KEY,"
    "  name       TEXT NOT NULL,"
    "  checksum   TEXT,"
    "  applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
    ")"
)


def _open_sqlite(db_path: Path) -> sqlite3.Connection:
    # isolation_level=None → autocommit, so we control BEGIN IMMEDIATE / COMMIT.
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def apply_sqlite(
    db_path: str | Path, migrations: tuple[Migration, ...], *, max_attempts: int = 15
) -> None:
    """Apply all pending migrations (already filtered to this file's target) to a
    SQLite database file, idempotently and safe under concurrent threads/processes.

    A per-file thread lock serialises concurrent callers in this process. The whole
    apply is wrapped in a retry-on-locked loop so a transient "database is locked"
    from any step (incl. the schema_migrations create) under cross-process contention
    re-opens and retries from scratch — every step is idempotent, so re-running just
    skips what's already applied.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _apply_lock_for(db_path):
        for attempt in range(1, max_attempts + 1):
            conn = _open_sqlite(db_path)
            try:
                conn.execute(_SQLITE_SCHEMA_MIGRATIONS)
                applied = _sqlite_applied(conn, migrations)
                for m in sorted(migrations, key=lambda x: x.id):
                    if m.id in applied:
                        continue
                    _apply_one_sqlite(conn, db_path, m)
                return
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower():
                    raise
                logger.info(
                    "apply_sqlite(%s) — db locked (attempt %d/%d), retrying",
                    db_path.name,
                    attempt,
                    max_attempts,
                )
                time.sleep(min(0.05 * attempt, 1.0))
            finally:
                conn.close()
        raise sqlite3.OperationalError(
            f"apply_sqlite({db_path}): database locked after {max_attempts} attempts"
        )


def _sqlite_applied(conn: sqlite3.Connection, migrations: tuple[Migration, ...]) -> set[int]:
    rows = list(conn.execute("SELECT id, name, checksum FROM schema_migrations"))
    by_id = {m.id: m for m in migrations}
    for mid, name, checksum in rows:
        m = by_id.get(mid)
        if m and checksum and checksum != m.checksum("sqlite"):
            logger.warning(
                "schema_migrations: applied migration %d (%s) checksum drift "
                "(recorded %s, current %s) — an applied migration was edited",
                mid,
                name,
                checksum,
                m.checksum("sqlite"),
            )
    return {r[0] for r in rows}


def _run_one_sqlite(conn: sqlite3.Connection, db_path: Path, m: Migration) -> None:
    """Execute one migration's body + stamp. Raises on failure; the caller retries."""
    # INSERT OR IGNORE so a concurrent duplicate stamp is harmless (PK on id).
    stamp = (
        "INSERT OR IGNORE INTO schema_migrations (id, name, checksum) VALUES (?, ?, ?)",
        (m.id, m.name, m.checksum("sqlite")),
    )
    if m.transactional:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-check under the write lock: another process may have applied this
            # migration between our applied-set read and acquiring the lock
            # (closes the TOCTOU — body must not run twice).
            if conn.execute("SELECT 1 FROM schema_migrations WHERE id=?", (m.id,)).fetchone():
                conn.execute("COMMIT")
                return
            for stmt in m.sqlite_sql or ():
                conn.execute(stmt)
            if m.sqlite_fn:
                m.sqlite_fn(conn, db_path)
            conn.execute(*stamp)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    else:
        # Non-transactional (e.g. baseline): the body MUST be idempotent. No write
        # lock is held, so concurrent runners may both execute it; that is safe for
        # IF-NOT-EXISTS / guarded DDL, and the stamp is OR IGNORE.
        for stmt in m.sqlite_sql or ():
            conn.execute(stmt)
        if m.sqlite_fn:
            m.sqlite_fn(conn, db_path)
        conn.execute(*stamp)


def _apply_one_sqlite(
    conn: sqlite3.Connection, db_path: Path, m: Migration, *, max_attempts: int = 12
) -> None:
    """Apply one migration, retrying transient 'database is locked' contention.

    Several processes (main server + ingestion server + dream_job) can bootstrap
    near-simultaneously. busy_timeout serialises most of it; the rare lock that
    still escapes is retried with backoff rather than abandoned (the previous
    'return on locked' left the idempotent baseline half-applied).
    """
    t0 = time.monotonic()
    for attempt in range(1, max_attempts + 1):
        if conn.execute("SELECT 1 FROM schema_migrations WHERE id=?", (m.id,)).fetchone():
            return  # applied by a concurrent runner
        try:
            _run_one_sqlite(conn, db_path, m)
            logger.info(
                "sqlite migration %d (%s) applied in %.3fs", m.id, m.name, time.monotonic() - t0
            )
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower():
                raise
            logger.info(
                "sqlite migration %d (%s) — db locked (attempt %d/%d), retrying",
                m.id,
                m.name,
                attempt,
                max_attempts,
            )
            time.sleep(min(0.05 * attempt, 1.0))
    if conn.execute("SELECT 1 FROM schema_migrations WHERE id=?", (m.id,)).fetchone():
        return
    raise sqlite3.OperationalError(
        f"migration {m.id} ({m.name}): database locked after {max_attempts} attempts"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Postgres engine (async)
# ──────────────────────────────────────────────────────────────────────────────

_PG_SCHEMA_MIGRATIONS = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "  id         BIGINT PRIMARY KEY,"
    "  name       TEXT NOT NULL,"
    "  checksum   TEXT,"
    "  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
    ")"
)


async def apply_postgres(store, migrations: tuple[Migration, ...]) -> None:
    """Apply all pending migrations to Postgres.

    Holds ONE dedicated pooled connection for the entire run — ``pg_advisory_lock``
    is session-scoped, so returning the connection to the pool between migrations
    could silently drop the lock. Skips DDL on a read-only standby.
    """
    await store.connect()
    conn = await store.pool.acquire()
    try:
        if await conn.fetchval("SELECT pg_is_in_recovery()"):
            logger.info("PostgresStore connected to read-only standby — skipping migrations")
            return
        await conn.execute(_PG_SCHEMA_MIGRATIONS)
        await conn.execute("SELECT pg_advisory_lock($1)", _PG_ADVISORY_LOCK_KEY)
        try:
            applied = await _pg_applied(conn, migrations)
            for m in sorted(migrations, key=lambda x: x.id):
                if m.id in applied:
                    continue
                await _apply_one_postgres(conn, m)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _PG_ADVISORY_LOCK_KEY)
    finally:
        await store.pool.release(conn)


async def _pg_applied(conn, migrations: tuple[Migration, ...]) -> set[int]:
    rows = await conn.fetch("SELECT id, name, checksum FROM schema_migrations")
    by_id = {m.id: m for m in migrations}
    for r in rows:
        m = by_id.get(r["id"])
        if m and r["checksum"] and r["checksum"] != m.checksum("postgres"):
            logger.warning(
                "schema_migrations: applied migration %d (%s) checksum drift "
                "(recorded %s, current %s) — an applied migration was edited",
                r["id"],
                r["name"],
                r["checksum"],
                m.checksum("postgres"),
            )
    return {r["id"] for r in rows}


async def _apply_one_postgres(conn, m: Migration) -> None:
    t0 = time.monotonic()

    async def _body():
        if m.postgres_sql:
            await conn.execute(m.postgres_sql)
        if m.postgres_fn:
            await m.postgres_fn(conn)
        await conn.execute(
            "INSERT INTO schema_migrations (id, name, checksum) VALUES ($1, $2, $3) "
            "ON CONFLICT (id) DO NOTHING",
            m.id,
            m.name,
            m.checksum("postgres"),
        )

    if m.transactional:
        async with conn.transaction():
            await _body()
    else:
        await _body()
    logger.info("postgres migration %d (%s) applied in %.3fs", m.id, m.name, time.monotonic() - t0)
