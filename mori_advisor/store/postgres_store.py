"""PostgresStore — asyncpg implementation of BaseStore.

Activated when MORI_DATABASE_URL starts with postgresql:// or postgres://.
All methods are async; callers in main.py use asyncio.run() for the sync
MCP tool surface. Dream pipeline uses async context manager for transactions.

Connection pool: min=2, max=10, statement_cache_size=0 (pgBouncer session mode).
Serialization errors (SQLSTATE 40001) are retried up to 3 times with backoff.

Tags, origin arrays: stored as JSONB.
Timestamps: TIMESTAMPTZ — Python datetime objects passed directly via asyncpg.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mori_advisor.memory_store import (
    _FRESHNESS_CACHE_TTL,
    _IN_FLIGHT_SENTINEL,
    FRESHNESS_CHECK_PROMPT,
    VALID_TIERS,
    _freshness_cache,
    _freshness_cache_lock,
)
from mori_advisor.provenance import (
    LEGACY,
    Provenance,
    authorize_tier,
    content_hash,
    validate_provenance,
)
from mori_advisor.write_result import Disposition, WriteResult, accepted

from .base import BaseStore

logger = logging.getLogger(__name__)

_RETRY_SERIALIZATION = 3
_RETRY_DELAY_BASE = 0.1  # seconds


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _ts(s: str | None) -> datetime | None:
    """Parse an ISO timestamp string → aware datetime, or None."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _tags_json(tags) -> str:
    """Ensure tags is a JSON array string."""
    if tags is None:
        return "[]"
    if isinstance(tags, list):
        return json.dumps(tags)
    if isinstance(tags, str):
        try:
            parsed = json.loads(tags)
            return json.dumps(parsed if isinstance(parsed, list) else [tags])
        except (json.JSONDecodeError, TypeError):
            return json.dumps([tags])
    return "[]"


def _coerce_msg_row(row) -> dict:
    """Convert an asyncpg Record (or dict) from msg_log into a plain dict.

    asyncpg returns TIMESTAMPTZ columns as Python ``datetime`` objects.  The
    ``msg_thread`` formatter (and any other caller) subscripts ``row["ts"]``
    as a string (e.g. ``row["ts"][:16]``).  SQLite stores ts as TEXT so it
    already comes back as ``str``; Postgres does not — this helper normalises
    the contract at the store boundary.

    All datetime-valued fields are coerced to their ISO-8601 string
    representation.  Unknown non-datetime fields are left untouched.
    """
    from datetime import datetime as _DT

    result = dict(row)
    for key, value in result.items():
        if isinstance(value, _DT):
            result[key] = value.isoformat()
    return result


async def _retry(coro_fn, *args, **kwargs):
    """Run coro_fn(*args, **kwargs), retrying on serialization errors."""
    last_exc = None
    for attempt in range(_RETRY_SERIALIZATION):
        try:
            return await coro_fn(*args, **kwargs)
        except Exception as e:
            # asyncpg raises asyncpg.exceptions.SerializationError
            if "40001" in str(e) or "serialization" in str(e).lower():
                last_exc = e
                await asyncio.sleep(_RETRY_DELAY_BASE * (2**attempt))
                continue
            raise
    raise last_exc


# ── DDL ───────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS memories (
    id                  BIGSERIAL PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    title               TEXT NOT NULL DEFAULT '',
    description         TEXT NOT NULL DEFAULT '',
    type                TEXT NOT NULL DEFAULT 'project',
    tier                TEXT NOT NULL DEFAULT 'working',
    body                TEXT NOT NULL DEFAULT '',
    tags                JSONB NOT NULL DEFAULT '[]',
    origin_session_id   TEXT,
    origin_session_ids  JSONB NOT NULL DEFAULT '[]',
    origin_clients      JSONB NOT NULL DEFAULT '[]',
    protected           BOOLEAN NOT NULL DEFAULT FALSE,
    protected_domains   JSONB NOT NULL DEFAULT '[]',
    superseded_by       TEXT,
    retrieval_count     INTEGER NOT NULL DEFAULT 0,
    last_retrieved_at   TIMESTAMPTZ,
    freshness_status    TEXT,
    freshness_checked_at TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_memories_tier   ON memories (tier);
CREATE INDEX IF NOT EXISTS idx_memories_type   ON memories (type);
CREATE INDEX IF NOT EXISTS idx_memories_name   ON memories (name);
CREATE INDEX IF NOT EXISTS idx_memories_tags   ON memories USING gin(tags);
CREATE INDEX IF NOT EXISTS idx_memories_updated_at ON memories (updated_at);

CREATE TABLE IF NOT EXISTS memory_versions (
    version_id          BIGSERIAL PRIMARY KEY,
    memory_name         TEXT NOT NULL REFERENCES memories(name) ON DELETE CASCADE,
    title               TEXT NOT NULL DEFAULT '',
    description         TEXT NOT NULL DEFAULT '',
    type                TEXT NOT NULL DEFAULT 'project',
    body                TEXT NOT NULL DEFAULT '',
    tags                JSONB NOT NULL DEFAULT '[]',
    origin_session_ids  JSONB NOT NULL DEFAULT '[]',
    origin_clients      JSONB NOT NULL DEFAULT '[]',
    version_note        TEXT NOT NULL DEFAULT '',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_memory_versions_name ON memory_versions (memory_name);

CREATE TABLE IF NOT EXISTS pending_writes (
    id                  BIGSERIAL PRIMARY KEY,
    memory_name         TEXT,
    title               TEXT NOT NULL DEFAULT '',
    description         TEXT NOT NULL DEFAULT '',
    type                TEXT NOT NULL DEFAULT 'project',
    body                TEXT NOT NULL DEFAULT '',
    tags                JSONB NOT NULL DEFAULT '[]',
    origin_session_ids  JSONB NOT NULL DEFAULT '[]',
    origin_clients      JSONB NOT NULL DEFAULT '[]',
    proposed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    proposed_by         TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'pending',
    reviewed_at         TIMESTAMPTZ,
    reviewed_by         TEXT,
    review_note         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pending_writes_status ON pending_writes (status);

CREATE TABLE IF NOT EXISTS eviction_queue (
    id          BIGSERIAL PRIMARY KEY,
    memory_name TEXT NOT NULL,
    reason      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved    BOOLEAN NOT NULL DEFAULT FALSE,
    resolved_at TIMESTAMPTZ,
    note        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS ingestion_log (
    id               BIGSERIAL PRIMARY KEY,
    source_path      TEXT NOT NULL,
    source_hash      TEXT NOT NULL,
    ingested_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    memories_written INTEGER NOT NULL DEFAULT 0,
    model            TEXT NOT NULL DEFAULT '',
    focus            TEXT NOT NULL DEFAULT 'all',
    tier             TEXT NOT NULL DEFAULT 'working',
    tags             JSONB NOT NULL DEFAULT '[]',
    dry_run          BOOLEAN NOT NULL DEFAULT FALSE,
    error_count      INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'committed',
    candidates_total INTEGER,
    convention_ratio REAL,
    anchorable_pct   REAL
);
CREATE INDEX IF NOT EXISTS idx_ingestion_log_hash ON ingestion_log (source_hash);

CREATE TABLE IF NOT EXISTS session_events (
    id              BIGSERIAL PRIMARY KEY,
    session_id      TEXT NOT NULL,
    event_name      TEXT NOT NULL,
    client          TEXT NOT NULL DEFAULT '',
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    tool_name       TEXT,
    tool_input      TEXT,
    tool_response   TEXT,
    tool_error      TEXT,
    model           TEXT,
    cwd             TEXT,
    transcript_path TEXT,
    prompt          TEXT,
    stop_reason     TEXT,
    assistant_text  TEXT
);
-- Migration for pre-existing deployments (no-op once the column exists):
ALTER TABLE session_events ADD COLUMN IF NOT EXISTS assistant_text TEXT;
CREATE INDEX IF NOT EXISTS idx_session_events_ts         ON session_events (timestamp);
CREATE INDEX IF NOT EXISTS idx_session_events_session_id ON session_events (session_id);

CREATE TABLE IF NOT EXISTS dream_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dreamer_config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS msg_log (
    id        TEXT PRIMARY KEY,
    from_host TEXT NOT NULL,
    to_host   TEXT NOT NULL,
    type      TEXT NOT NULL,
    ts        TIMESTAMPTZ NOT NULL,
    body      TEXT NOT NULL,
    reply_to  TEXT,
    status    TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_msg_log_to_host  ON msg_log (to_host);
CREATE INDEX IF NOT EXISTS idx_msg_log_status   ON msg_log (status);
CREATE INDEX IF NOT EXISTS idx_msg_log_reply_to ON msg_log (reply_to);

CREATE TABLE IF NOT EXISTS delegate_tasks (
    id          BIGSERIAL PRIMARY KEY,
    task_id     TEXT NOT NULL UNIQUE,
    from_host   TEXT NOT NULL,
    to_host     TEXT NOT NULL,
    description TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    result      TEXT
);
"""


class PostgresStore(BaseStore):
    """asyncpg-backed store. All public methods are async coroutines.

    Callers in the synchronous MCP tool surface wrap with asyncio.run().
    The dream pipeline uses `async with store.begin_transaction()`.
    """

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.pool = None  # initialised by bootstrap() or connect()
        # Strong refs to fire-and-forget retrieval-bump tasks so they aren't GC'd
        # before completion (see _bump_retrieval_bg).
        self._bg_tasks: set = set()

    def _bump_retrieval_bg(self, names: list[str]) -> None:
        """Fire-and-forget retrieval bump for agent-recall paths (list/search/brief).

        ``retrieval_count`` here means "returned in a result set surfaced to the agent"
        (exposure), NOT "used in the final prompt" — a search returning 50 bumps all 50.
        Deferred + batched so it never blocks recall latency (the consult's
        write-amplification guard); best-effort — errors are logged, never raised.
        """
        if not names:
            return

        async def _do() -> None:
            try:
                self._ensure_pool()
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE memories SET retrieval_count = retrieval_count + 1, "
                        "last_retrieved_at = $2 WHERE name = ANY($1::text[]) "
                        "AND deleted_at IS NULL",
                        names,
                        _now_utc(),
                    )
            except Exception as e:  # never let a metrics bump break recall
                logger.debug("retrieval bump failed (non-fatal): %s", e)

        try:
            task = asyncio.create_task(_do())
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        except RuntimeError:
            # No running loop (e.g. sync test context) — skip the bump silently.
            pass

    async def connect(self) -> None:
        """Create the connection pool. Called by bootstrap() and health probes."""
        import asyncpg

        if self.pool is None:
            # ssl=False: asyncpg's SSL probe path triggers a getaddrinfo code
            # branch that fails in some environments (systemd-resolved stub +
            # asyncio thread executor). Postgres on private networks does not
            # need TLS; enable explicitly via ?sslmode=require in the DSN if needed.
            self.pool = await asyncpg.create_pool(
                self.dsn,
                min_size=2,
                max_size=10,
                statement_cache_size=0,  # required for pgBouncer session mode
                ssl=False,
            )

    def _ensure_pool(self):
        if self.pool is None:
            raise RuntimeError(
                "PostgresStore not connected — call await store.connect() or await store.bootstrap() first"
            )

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def bootstrap(self) -> None:
        # Schema is owned by the migration runner. apply_postgres() connects,
        # holds one dedicated connection for the run (pg_advisory_lock is
        # session-scoped), skips DDL on a read-only standby, and applies the
        # baseline (the _DDL constant, via migration 1) + any pending migrations.
        from .migrations import MIGRATIONS, apply_postgres

        await apply_postgres(self, MIGRATIONS)
        logger.info("PostgresStore bootstrap complete")

    async def ping(self) -> None:
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            await conn.execute("SELECT 1")

    @asynccontextmanager
    async def begin_transaction(self):
        """Async context manager — yields asyncpg.Connection inside a transaction."""
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                yield conn

    def get_conn(self):
        raise NotImplementedError(
            "get_conn() is not supported on PostgresStore — use begin_transaction() or specific store methods"
        )

    # ── Memory CRUD ────────────────────────────────────────────────────────

    async def _write(
        self,
        name=None,
        title="",
        description="",
        type="project",
        tier="working",
        body="",
        tags=None,
        origin_session_id=None,
        origin_session_ids=None,
        origin_clients=None,
        client=None,
        provenance: Provenance = LEGACY,
        _skip_protection=False,
        _conn=None,
    ) -> WriteResult:
        self._ensure_pool()
        from mori_advisor.memory_store import _slugify

        if not name and title:
            name = _slugify(title)
        elif not name:
            import time

            name = f"memory-{int(time.time())}"

        # Completeness chokepoint (AUDIT mode — logs, never blocks). Mirrors the SQLite
        # seam in memory_store.write so both backends enforce one anatomy contract.
        from mori_advisor.completeness import audit_completeness

        audit_completeness(body, description, seam="store.write:postgres", name=name, log=logger)

        # Phase 2 authorization pipeline (AUDIT-MODE — observe, never block in this step).
        # stage 1: validate provenance · stage 2: tier-target authorization (may_target).
        # Same single policy authority as SQLite; enforcement lands behind MORI_TIER_ENFORCE.
        validate_provenance(provenance, name, logger)
        _tier_ok, _tier_reason = authorize_tier(provenance, tier)
        if not _tier_ok:
            logger.warning(
                "TIER-AUDIT would-block name=%s: %s (audit-mode — not enforced)",
                name,
                _tier_reason,
            )

        tags_v = _tags_json(tags)
        sess_ids = _tags_json(origin_session_ids)
        clients = _tags_json(origin_clients or ([client] if client else []))
        now = _now_utc()
        # Defensive coalesce: mirror SQLite's _ensure_tier — a None or unrecognised
        # tier must never reach the DB as NULL (violates NOT NULL constraint).
        tier = tier if tier in VALID_TIERS else "working"

        async def _do(conn):
            existing = await conn.fetchrow(
                "SELECT id, protected, protected_domains, tier, origin_session_ids, origin_clients FROM memories WHERE name = $1 AND deleted_at IS NULL",
                name,
            )
            if existing and existing["protected"] and not _skip_protection:
                return WriteResult(
                    memory_name=name,
                    intended_tier=tier,
                    stored_tier="",
                    disposition=Disposition.REJECTED,
                    reason=f"Memory '{name}' is protected — use _skip_protection=True to override",
                )

            # Compute merged origin arrays for upsert (mirrors SQLite's memory_store.py)
            if existing:
                merged_ids = json.dumps(
                    sorted(
                        set(
                            json.loads(sess_ids)
                            + (json.loads(existing["origin_session_ids"] or "[]"))
                        )
                    )
                )
                merged_clients = json.dumps(
                    sorted(
                        set(json.loads(clients) + (json.loads(existing["origin_clients"] or "[]")))
                    )
                )
                # Don't downgrade canonical tier
                if existing.get("tier") == "canonical":
                    result_tier = "canonical"
                else:
                    result_tier = tier
                # Preserve existing protection flags and domains (JSONB → Python objects from asyncpg)
                protect = existing.get("protected", False)
                protect_domains_raw = json.dumps(existing.get("protected_domains", []))
            else:
                merged_ids = sess_ids
                merged_clients = clients
                result_tier = tier
                protect = False
                protect_domains_raw = json.dumps([])

            # Single atomic upsert — no TOCTOU race (matches SQLite ON CONFLICT DO UPDATE)
            await conn.execute(
                """INSERT INTO memories
                   (name, title, description, type, tier, body, tags,
                    origin_session_id, origin_session_ids, origin_clients,
                    protected, protected_domains, created_at, updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9::jsonb,$10::jsonb,$11,$12::jsonb,$13,$13)
                   ON CONFLICT (name) WHERE deleted_at IS NULL DO UPDATE SET
                       title               = EXCLUDED.title,
                       description         = EXCLUDED.description,
                       type                = EXCLUDED.type,
                       tier                = EXCLUDED.tier,
                       body                = EXCLUDED.body,
                       tags                = EXCLUDED.tags,
                       origin_session_id   = COALESCE(EXCLUDED.origin_session_id, memories.origin_session_id),
                       origin_session_ids  = EXCLUDED.origin_session_ids,
                       origin_clients      = EXCLUDED.origin_clients,
                       protected           = EXCLUDED.protected,
                       protected_domains   = EXCLUDED.protected_domains,
                       updated_at          = EXCLUDED.updated_at""",
                name,
                title,
                description,
                type,
                result_tier,
                body,
                tags_v,
                origin_session_id,
                merged_ids,
                merged_clients,
                protect,
                protect_domains_raw,
                now,
            )
            # Universal, in-transaction audit (identity-aware chokepoint, Phase 1):
            # same conn as the upsert = atomic; covers every writer incl. the dreamer.
            # (legacy/unknown-actor warnings fire earlier in validate_provenance.)
            try:
                await conn.execute(
                    "INSERT INTO write_audit "
                    "(actor_key_name, op, memory_name, content_hash, detail) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    provenance.ledger_actor,
                    provenance.op,
                    name,
                    content_hash(body),
                    provenance.source,
                )
            except Exception as ae:  # pre-migration test DB may lack write_audit
                if "does not exist" not in str(ae).lower():
                    raise
            return accepted(name, result_tier)

        if _conn:
            return await _retry(_do, _conn)
        async with self.pool.acquire() as conn:
            return await _retry(_do, conn)

    async def write(
        self,
        name=None,
        title="",
        description="",
        type="project",
        tier="working",
        body="",
        tags=None,
        origin_session_id=None,
        origin_session_ids=None,
        origin_clients=None,
        client=None,
        provenance: Provenance = LEGACY,
        _skip_protection=False,
        _conn=None,
    ) -> str:
        """Legacy string-returning adapter over :meth:`_write` (behaviour-preserving) — the
        same status message as before the split. Callers needing the structured outcome call
        :meth:`_write` and inspect the :class:`WriteResult`."""
        r = await self._write(
            name=name,
            title=title,
            description=description,
            type=type,
            tier=tier,
            body=body,
            tags=tags,
            origin_session_id=origin_session_id,
            origin_session_ids=origin_session_ids,
            origin_clients=origin_clients,
            client=client,
            provenance=provenance,
            _skip_protection=_skip_protection,
            _conn=_conn,
        )
        if r.disposition is Disposition.ACCEPTED:
            return f"Memory '{r.memory_name}' written"
        return r.reason

    async def read(self, name: str) -> str:
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM memories WHERE name = $1 AND deleted_at IS NULL", name
            )
            if not row:
                return f"Memory '{name}' not found"
            await conn.execute(
                "UPDATE memories SET retrieval_count = retrieval_count + 1, last_retrieved_at = $2 WHERE name = $1 AND deleted_at IS NULL",
                name,
                _now_utc(),
            )
        r = dict(row)
        tags = json.loads(r.get("tags") or "[]")
        lines = [
            f"# {r['title']}",
            f"**Name:** {r['name']}  **Type:** {r['type']}  **Tier:** {r['tier']}",
            f"**Tags:** {', '.join(tags) or '—'}",
            f"**Description:** {r['description']}",
            "",
            r["body"] or "",
        ]
        return "\n".join(lines)

    async def export_rows(
        self, tiers: tuple[str, ...] = ("canonical",), type_filter: str = "", limit: int = 200
    ) -> list[dict]:
        """Raw active memory rows for canon export, most-retrieved first.

        Returns full row dicts (the caller sanitises onto the export allowlist). JSONB
        tags are normalised to a list.
        """
        self._ensure_pool()
        clauses = ["deleted_at IS NULL", "tier = ANY($1)"]
        params: list = [list(tiers)]
        if type_filter:
            params.append(type_filter)
            clauses.append(f"type = ${len(params)}")
        params.append(int(limit))
        sql = (
            f"SELECT * FROM memories WHERE {' AND '.join(clauses)} "
            f"ORDER BY retrieval_count DESC, updated_at DESC LIMIT ${len(params)}"
        )
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        def _to_list(val):
            if val is None:
                return []
            if isinstance(val, str):
                try:
                    return json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    return []
            return val if isinstance(val, list) else []

        out = []
        for row in rows:
            d = dict(row)
            d["tags"] = _to_list(d.get("tags"))
            out.append(d)
        return out

    async def get_memory(self, name: str) -> dict | None:
        """Return a curated detail dict for a single memory, or None if not found.

        Does NOT bump retrieval_count (browse/API access, not agent recall).
        Returns exactly the DETAIL_KEYS shape:
          name, title, type, tier, tags, description, body,
          created_at, updated_at, origin_clients, retrieval_count, freshness_status.
        """
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM memories WHERE name = $1 AND deleted_at IS NULL", name
            )
        if not row:
            return None
        r = dict(row)

        # JSONB columns: asyncpg may return a JSON string or already-parsed list/None.
        def _to_list(val):
            if val is None:
                return []
            if isinstance(val, str):
                try:
                    return json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    return []
            # asyncpg decoded JSONB → already a Python list
            if isinstance(val, list):
                return val
            return []

        return {
            "name": r["name"],
            "title": r["title"],
            "type": r["type"],
            "tier": r["tier"],
            "tags": _to_list(r.get("tags")),
            "description": r.get("description") or "",
            "body": r.get("body") or "",
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
            "origin_clients": _to_list(r.get("origin_clients")),
            "retrieval_count": r.get("retrieval_count") or 0,
            "freshness_status": r.get("freshness_status") or "unknown",
        }

    async def list(self, type_filter=None, tag=None, session=None, client=None, limit=50) -> str:
        self._ensure_pool()
        clauses = ["deleted_at IS NULL"]
        params: list[Any] = []
        i = 1
        if type_filter:
            clauses.append(f"type = ${i}")
            params.append(type_filter)
            i += 1
        if tag:
            clauses.append(f"tags @> ${i}::jsonb")
            params.append(json.dumps([tag]))
            i += 1
        if session:
            clauses.append(f"origin_session_id = ${i}")
            params.append(session)
            i += 1
        if client:
            clauses.append(f"origin_clients @> ${i}::jsonb")
            params.append(json.dumps([client]))
            i += 1

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT name, title, type, tier, tags, updated_at FROM memories {where} ORDER BY updated_at DESC LIMIT ${i}",
                *params,
            )
        if not rows:
            return "No memories found."
        self._bump_retrieval_bg([r["name"] for r in rows])
        lines = [f"- **{r['name']}** ({r['type']}/{r['tier']}) — {r['title']}" for r in rows]
        return f"{len(rows)} memories:\n" + "\n".join(lines)

    async def _has_search_tsv(self, conn) -> bool:
        return await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'memories' AND column_name = 'search_tsv')"
        )

    def _build_search_clauses(self, has_tsv, query, type_filter, tag, client, since):
        """Build (clauses, params, order, next_$index) — shared by search()/search_json().

        FTS via the generated search_tsv (ranked) when a query is present + the column
        exists; else ILIKE (pre-migration) or pure recency. websearch_to_tsquery accepts
        raw human input safely.
        """
        clauses: list[str] = ["deleted_at IS NULL"]
        params: list[Any] = []
        i = 1
        order = "updated_at DESC"
        if query and has_tsv:
            clauses.append(f"search_tsv @@ websearch_to_tsquery('english', ${i})")
            params.append(query)
            # Reuse the same param in the rank expression (lower index = query).
            order = (
                f"ts_rank(search_tsv, websearch_to_tsquery('english', ${i})) DESC, updated_at DESC"
            )
            i += 1
        elif query:
            clauses.append(
                f"(title ILIKE ${i} OR description ILIKE ${i} OR body ILIKE ${i} OR name ILIKE ${i})"
            )
            params.append(f"%{query}%")
            i += 1
        if type_filter:
            clauses.append(f"type = ${i}")
            params.append(type_filter)
            i += 1
        if tag:
            clauses.append(f"tags @> ${i}::jsonb")
            params.append(json.dumps([tag]))
            i += 1
        if client:
            clauses.append(f"origin_clients @> ${i}::jsonb")
            params.append(json.dumps([client]))
            i += 1
        if since:
            clauses.append(f"updated_at >= ${i}")
            params.append(_ts(since))
            i += 1
        return clauses, params, order, i

    async def search(
        self, query=None, type_filter=None, tag=None, client=None, since=None, limit=10
    ) -> str:
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            has_tsv = await self._has_search_tsv(conn)
            clauses, params, order, i = self._build_search_clauses(
                has_tsv, query, type_filter, tag, client, since
            )
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            params.append(limit)
            rows = await conn.fetch(
                f"SELECT name, title, type, tier FROM memories {where} ORDER BY {order} LIMIT ${i}",
                *params,
            )
        if not rows:
            return "No results."
        self._bump_retrieval_bg([r["name"] for r in rows])
        lines = [f"- **{r['name']}** ({r['type']}/{r['tier']}) — {r['title']}" for r in rows]
        return "\n".join(lines)

    async def search_json(
        self, query=None, type_filter=None, tag=None, client=None, since=None, limit=50
    ) -> list[dict]:
        """Structured search for the REST API — same shape as MemoryStore.search_json
        (name, title, type, tier, tags, updated_at, description). FTS-ranked when a query
        is given. No retrieval_count bump (API surfacing, not agent recall)."""
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            has_tsv = await self._has_search_tsv(conn)
            clauses, params, order, i = self._build_search_clauses(
                has_tsv, query, type_filter, tag, client, since
            )
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            params.append(limit)
            rows = await conn.fetch(
                "SELECT name, title, type, tier, tags, updated_at, description "
                f"FROM memories {where} ORDER BY {order} LIMIT ${i}",
                *params,
            )
        out = []
        for r in rows:
            d = dict(r)
            tags = d.get("tags")  # JSONB → asyncpg returns a JSON string
            if isinstance(tags, str):
                try:
                    d["tags"] = json.loads(tags)
                except (json.JSONDecodeError, TypeError):
                    d["tags"] = []
            ua = d.get("updated_at")  # TIMESTAMPTZ → datetime; ISO for JSON
            if hasattr(ua, "isoformat"):
                d["updated_at"] = ua.isoformat()
            out.append(d)
        return out

    async def delete(self, name: str) -> str:
        """Soft-delete a memory.  Use hard_delete() for permanent removal."""
        return await self.soft_delete(name)

    async def soft_delete(self, name: str) -> str:
        """Set deleted_at = now on the active row.  Idempotent if already deleted."""
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE memories SET deleted_at = NOW(), updated_at = NOW() "
                "WHERE name = $1 AND deleted_at IS NULL",
                name,
            )
        updated = int(result.split()[-1])
        return f"Memory '{name}' soft-deleted." if updated else f"Memory '{name}' not found."

    async def hard_delete(self, name: str) -> str:
        """Permanently remove a memory row (active or tombstoned) and its versions.

        memory_versions FK was dropped by migration 9 (partial indexes cannot be FK
        targets). Versions are cleaned up here to prevent orphans.
        """
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM memory_versions WHERE memory_name = $1", name)
            result = await conn.execute("DELETE FROM memories WHERE name = $1", name)
        deleted = int(result.split()[-1])
        return f"Memory '{name}' permanently deleted." if deleted else f"Memory '{name}' not found."

    async def restore_memory(self, name: str) -> tuple[str, str]:
        """Restore a soft-deleted memory; rename to {name}_restored_{ts} on collision."""
        self._ensure_pool()
        from datetime import datetime, timezone

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM memories WHERE name = $1 AND deleted_at IS NOT NULL "
                "ORDER BY deleted_at DESC LIMIT 1",
                name,
            )
            if not row:
                return name, f"Memory '{name}' not found or not deleted."

            row_id = row["id"]
            collision = await conn.fetchval(
                "SELECT 1 FROM memories WHERE name = $1 AND deleted_at IS NULL", name
            )

            if collision:
                ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
                final_name = f"{name}_restored_{ts}"
                try:
                    await conn.execute(
                        "UPDATE memories SET name = $1, deleted_at = NULL, updated_at = NOW() "
                        "WHERE id = $2",
                        final_name,
                        row_id,
                    )
                except Exception:
                    return name, "Restore failed: name collision could not be resolved."
                return final_name, f"Restored '{name}' as '{final_name}' (name taken)."
            else:
                await conn.execute(
                    "UPDATE memories SET deleted_at = NULL, updated_at = NOW() WHERE id = $1",
                    row_id,
                )
                return name, f"Memory '{name}' restored."

    async def insert_audit(
        self,
        op: str,
        actor: str,
        name: str,
        content_hash: str,
        detail: str = "",
        reason_code: str = "",
    ) -> None:
        """Insert a row into write_audit.  Silently no-ops if the table/column is absent.

        reason_code is the TD decision taxonomy (measurement layer) on approve/reject.
        """
        self._ensure_pool()
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO write_audit "
                    "(actor_key_name, op, memory_name, content_hash, detail, reason_code) "
                    "VALUES ($1, $2, $3, $4, $5, $6)",
                    actor,
                    op,
                    name,
                    content_hash,
                    detail,
                    reason_code or None,
                )
        except Exception as exc:
            msg = str(exc).lower()
            if "write_audit" in msg or "does not exist" in msg or "reason_code" in msg:
                pass  # migration not yet applied
            else:
                raise

    async def get_audit_log(
        self, memory_name: str = "", actor: str = "", limit: int = 100
    ) -> list[dict]:
        """Return recent write_audit rows, newest first.  Max 500 rows."""
        self._ensure_pool()
        limit = min(max(1, limit), 500)
        clauses = []
        params: list[Any] = []
        i = 1
        if memory_name:
            clauses.append(f"memory_name = ${i}")
            params.append(memory_name)
            i += 1
        if actor:
            clauses.append(f"actor_key_name = ${i}")
            params.append(actor)
            i += 1
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT id, ts, actor_key_name, op, memory_name, content_hash, detail "
                    f"FROM write_audit {where} ORDER BY ts DESC, id DESC LIMIT ${i}",
                    *params,
                )
            return [dict(r) for r in rows]
        except Exception:
            return []  # table not yet created

    async def export(self, name: str, output_path=None) -> str:
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM memories WHERE name = $1", name)
        if not row:
            return f"Memory '{name}' not found"
        r = dict(row)
        tags = json.loads(r.get("tags") or "[]")
        content = (
            f"---\nname: {r['name']}\ntitle: {r['title']}\ntype: {r['type']}\n"
            f"tier: {r['tier']}\ntags: {tags}\n---\n\n{r['body']}"
        )
        if output_path:
            Path(output_path).write_text(content)
            return f"Exported to {output_path}"
        return content

    async def export_all(self, output_dir: str) -> str:
        self._ensure_pool()
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT name, title, type, tier, tags, body FROM memories")
        for row in rows:
            r = dict(row)
            tags = json.loads(r.get("tags") or "[]")
            content = (
                f"---\nname: {r['name']}\ntitle: {r['title']}\ntype: {r['type']}\n"
                f"tier: {r['tier']}\ntags: {tags}\n---\n\n{r['body']}"
            )
            (out / f"{r['name']}.md").write_text(content)
        return f"Exported {len(rows)} memories to {output_dir}"

    async def import_memories(self, source_dir: str) -> str:
        self._ensure_pool()
        src = Path(source_dir)
        files = list(src.glob("*.md"))
        imported = 0
        for f in files:
            text = f.read_text()
            parts = text.split("---\n", 2)
            if len(parts) < 3:
                continue
            import yaml

            meta = yaml.safe_load(parts[1])
            body = parts[2].lstrip("\n")
            await self.write(
                name=meta.get("name"),
                title=meta.get("title", ""),
                type=meta.get("type", "project"),
                tier=meta.get("tier", "working"),
                tags=meta.get("tags", []),
                body=body,
                provenance=Provenance(actor="import", source="store:import_memories", op="import"),
            )
            imported += 1
        return f"Imported {imported} memories from {source_dir}"

    # ── Memory metadata ────────────────────────────────────────────────────

    async def get_memories_by_project(
        self, project: str, include_global: bool = True, strict_global: bool = False
    ) -> dict:
        self._ensure_pool()
        tag_value = f"project:{project}"

        def _row_to_dict(row) -> dict:
            r = dict(row)
            raw = r.get("tags")
            r["tags"] = json.loads(raw or "[]") if isinstance(raw, str) else (raw or [])
            # asyncpg returns TIMESTAMPTZ as datetime; callers (e.g. the brief formatter)
            # subscript updated_at[:10] as a string, and SQLite already returns str —
            # normalise the contract at the store boundary (cf. _coerce_msg_row).
            for k, v in r.items():
                if isinstance(v, datetime):
                    r[k] = v.isoformat()
            return r

        async with self.pool.acquire() as conn:
            project_rows = await conn.fetch(
                """
                SELECT * FROM memories
                WHERE tags @> $1::jsonb
                  AND tier IN ('canonical', 'working')
                  AND (superseded_by IS NULL OR superseded_by = '')
                  AND deleted_at IS NULL
                ORDER BY
                  CASE tier WHEN 'canonical' THEN 0 ELSE 1 END ASC,
                  updated_at DESC,
                  id DESC
                """,
                json.dumps([tag_value]),
            )

            # Provenance (strict_global): in strict mode a memory reaches the cross-project
            # lane ONLY via an explicit scope:global / scope:cross-project tag. The legacy
            # `type IN (profile, pattern)` auto-global is dropped — an origin-bound memory
            # mistyped 'pattern' would otherwise leak into every project's brief.
            global_rows: list = []
            if include_global:
                type_clause = "" if strict_global else "OR type IN ('profile', 'pattern')"
                global_rows = await conn.fetch(
                    f"""
                    SELECT * FROM memories
                    WHERE (
                        tags @> '["scope:global"]'::jsonb
                        OR tags @> '["scope:cross-project"]'::jsonb
                        {type_clause}
                    )
                    AND (superseded_by IS NULL OR superseded_by = '')
                    AND deleted_at IS NULL
                    AND NOT (tags @> $1::jsonb)
                    ORDER BY tier DESC, updated_at DESC, id DESC
                    """,
                    json.dumps([tag_value]),
                )

            # Other-project index: memories that carry any project:* tag except ours
            other_raw = await conn.fetch(
                """
                SELECT tags FROM memories
                WHERE EXISTS (
                    SELECT 1 FROM jsonb_array_elements_text(tags) AS elem
                    WHERE elem LIKE 'project:%'
                )
                AND NOT (tags @> $1::jsonb)
                AND (superseded_by IS NULL OR superseded_by = '')
                AND deleted_at IS NULL
                """,
                json.dumps([tag_value]),
            )

        project_memories = [_row_to_dict(r) for r in project_rows]
        global_memories = [_row_to_dict(r) for r in global_rows]
        # Brief surfaces these bodies to the agent → count as recall (the other-project
        # index is counts only, not bodies, so it is not bumped).
        self._bump_retrieval_bg(
            [m["name"] for m in project_memories] + [m["name"] for m in global_memories]
        )

        other_counts: dict[str, int] = {}
        for row in other_raw:
            raw = row["tags"]
            tags = json.loads(raw or "[]") if isinstance(raw, str) else (raw or [])
            for tag in tags:
                if isinstance(tag, str) and tag.startswith("project:") and tag != tag_value:
                    proj_name = tag[len("project:") :]
                    other_counts[proj_name] = other_counts.get(proj_name, 0) + 1
        other_projects = sorted(other_counts.items(), key=lambda x: x[1], reverse=True)

        return {
            "project_memories": project_memories,
            "global_memories": global_memories,
            "other_projects": other_projects,
        }

    async def filter_by_scope(
        self,
        project: str,
        include_global: bool = True,
        strict_global: bool = False,
    ) -> dict:
        """Postgres twin of MemoryStore.filter_by_scope — the H2 subsumption shim.

        Membership is decided generically (resolver.compile_memory_scope +
        scope.in_scope); the legacy partition, tier asymmetry, ORDER BY and
        other-project index are preserved verbatim so the brief output is
        byte-identical to ``get_memories_by_project`` for legacy (NULL-scope) rows.
        See the SQLite docstring for the full rationale.
        """
        from mori_advisor.resolver import compile_context_tags, compile_memory_scope
        from mori_advisor.scope import in_scope

        self._ensure_pool()
        context = compile_context_tags(project, strict_global)
        tag_value = f"project:{project}"

        def _row_to_dict(row) -> dict:
            r = dict(row)
            raw = r.get("tags")
            r["tags"] = json.loads(raw or "[]") if isinstance(raw, str) else (raw or [])
            for k, v in r.items():
                if isinstance(v, datetime):
                    r[k] = v.isoformat()
            return r

        async with self.pool.acquire() as conn:
            # Project-lane candidates — legacy project query MINUS the tag filter.
            project_rows = await conn.fetch(
                """
                SELECT * FROM memories
                WHERE tier IN ('canonical', 'working')
                  AND (superseded_by IS NULL OR superseded_by = '')
                  AND deleted_at IS NULL
                ORDER BY
                  CASE tier WHEN 'canonical' THEN 0 ELSE 1 END ASC,
                  updated_at DESC,
                  id DESC
                """
            )
            global_rows: list = []
            if include_global:
                # Global-lane candidates — legacy global query MINUS the tag clauses,
                # no tier filter (legacy global lane surfaces any tier).
                global_rows = await conn.fetch(
                    """
                    SELECT * FROM memories
                    WHERE (superseded_by IS NULL OR superseded_by = '')
                      AND deleted_at IS NULL
                    ORDER BY tier DESC, updated_at DESC, id DESC
                    """
                )
            # Other-project index — identical query to the oracle.
            other_raw = await conn.fetch(
                """
                SELECT tags FROM memories
                WHERE EXISTS (
                    SELECT 1 FROM jsonb_array_elements_text(tags) AS elem
                    WHERE elem LIKE 'project:%'
                )
                AND NOT (tags @> $1::jsonb)
                AND (superseded_by IS NULL OR superseded_by = '')
                AND deleted_at IS NULL
                """,
                json.dumps([tag_value]),
            )

        def _kept(d: dict) -> bool:
            return in_scope(compile_memory_scope(d), context)

        project_memories = [
            d
            for d in (_row_to_dict(r) for r in project_rows)
            if tag_value in d["tags"] and _kept(d)
        ]
        global_memories = [
            d
            for d in (_row_to_dict(r) for r in global_rows)
            if tag_value not in d["tags"] and _kept(d)
        ]
        self._bump_retrieval_bg(
            [m["name"] for m in project_memories] + [m["name"] for m in global_memories]
        )

        other_counts: dict[str, int] = {}
        for row in other_raw:
            raw = row["tags"]
            tags = json.loads(raw or "[]") if isinstance(raw, str) else (raw or [])
            for tag in tags:
                if isinstance(tag, str) and tag.startswith("project:") and tag != tag_value:
                    proj_name = tag[len("project:") :]
                    other_counts[proj_name] = other_counts.get(proj_name, 0) + 1
        other_projects = sorted(other_counts.items(), key=lambda x: x[1], reverse=True)

        return {
            "project_memories": project_memories,
            "global_memories": global_memories,
            "other_projects": other_projects,
        }

    async def get_memories_changed_since(
        self,
        since: str,
        project: str | None = None,
        include_global: bool = True,
        limit: int = 30,
    ) -> list[dict]:
        """Postgres parity for the post-compact delta brief.

        `since` accepts relative shorthand ("6h"/"7d") or ISO-8601; it is routed
        through `normalise_since` (handles shorthand) then `_ts` to a tz-aware UTC
        datetime, so the TIMESTAMPTZ comparison is correct regardless of the
        client's clock representation. Exclusive bound (`updated_at > since`).
        `updated_at` is stringified in the result to match the SQLite contract.
        """
        from mori_advisor.memory_store import normalise_since

        self._ensure_pool()
        try:
            since_dt = _ts(normalise_since(since))
        except (ValueError, TypeError):
            return []
        if since_dt is None:
            return []

        def _row_to_dict(row) -> dict:
            r = dict(row)
            raw = r.get("tags")
            r["tags"] = json.loads(raw or "[]") if isinstance(raw, str) else (raw or [])
            if r.get("updated_at") is not None:
                r["updated_at"] = str(r["updated_at"])
            return r

        params: list[Any] = [since_dt]
        if project:
            tag_value = f"project:{project}"
            params.append(json.dumps([tag_value]))
            if include_global:
                scope = (
                    "(tags @> $2::jsonb "
                    "OR tags @> '[\"scope:global\"]'::jsonb "
                    "OR tags @> '[\"scope:cross-project\"]'::jsonb "
                    "OR type IN ('profile', 'pattern'))"
                )
            else:
                scope = "tags @> $2::jsonb"
            params.append(limit)
            sql = f"""
                SELECT * FROM memories
                WHERE updated_at > $1
                  AND (superseded_by IS NULL OR superseded_by = '')
                  AND deleted_at IS NULL
                  AND {scope}
                ORDER BY updated_at DESC
                LIMIT $3
            """
        else:
            params.append(limit)
            sql = """
                SELECT * FROM memories
                WHERE updated_at > $1
                  AND (superseded_by IS NULL OR superseded_by = '')
                  AND deleted_at IS NULL
                ORDER BY updated_at DESC
                LIMIT $2
            """

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        return [_row_to_dict(r) for r in rows]

    async def session_summary(self, session_id: str) -> str:
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT name, title, type FROM memories WHERE origin_session_id = $1", session_id
            )
        if not rows:
            return f"No memories found for session {session_id}"
        lines = [f"- {r['name']} ({r['type']}) — {r['title']}" for r in rows]
        return f"Session {session_id}: {len(rows)} memories\n" + "\n".join(lines)

    async def history(self, name: str, limit: int = 10) -> str:
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT version_id, version_note, created_at FROM memory_versions "
                "WHERE memory_name = $1 ORDER BY version_id DESC LIMIT $2",
                name,
                limit,
            )
        if not rows:
            return f"No version history for '{name}'."
        lines = [
            f"v{r['version_id']} ({r['created_at']}) — {r['version_note'] or '(no note)'}"
            for r in rows
        ]
        return "\n".join(lines)

    async def diff(self, name: str, from_version: int, to_version: int) -> str:
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT version_id, body FROM memory_versions "
                "WHERE memory_name = $1 AND version_id IN ($2, $3) "
                "ORDER BY version_id ASC",
                name,
                from_version,
                to_version,
            )
        if len(rows) < 2:
            return f"Could not find both versions ({from_version}, {to_version}) for '{name}'."
        import difflib

        a_lines = (rows[0]["body"] or "").splitlines(keepends=True)
        b_lines = (rows[1]["body"] or "").splitlines(keepends=True)
        diff = "".join(difflib.unified_diff(a_lines, b_lines, fromfile="before", tofile="after"))
        return diff or "(no differences)"

    async def rollback(self, name: str, version_id: int) -> str:
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM memory_versions WHERE version_id = $1 AND memory_name = $2",
                version_id,
                name,
            )
            if not row:
                return f"Version {version_id} not found for '{name}'."
            await conn.execute(
                "UPDATE memories SET title=$2, description=$3, type=$4, tier=$5, body=$6, "
                "tags=$7::jsonb, updated_at=$8 WHERE name=$1",
                name,
                row["title"],
                row["description"],
                row["type"],
                row["tier"],
                row["body"],
                row["tags"],
                _now_utc(),
            )
        return f"Memory '{name}' rolled back to version {version_id}."

    # ── Counts / observability ─────────────────────────────────────────────

    async def count(self, tier: str | None = None, protected: bool | None = None) -> int:
        self._ensure_pool()
        q = "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL"
        params = []
        param_idx = 1
        if tier is not None:
            q += f" AND tier = ${param_idx}"
            params.append(tier)
            param_idx += 1
        if protected is not None:
            q += f" AND protected = ${param_idx}"
            params.append(protected)
            param_idx += 1

        async with self.pool.acquire() as conn:
            return await conn.fetchval(q, *params)

    async def pending_count(self, status: str | None = None) -> int:
        self._ensure_pool()
        q = "SELECT COUNT(*) FROM pending_writes"
        params = []
        if status is not None:
            q += " WHERE status = $1"
            params.append(status)
        else:
            q += " WHERE status = 'pending'"

        async with self.pool.acquire() as conn:
            return await conn.fetchval(q, *params)

    async def eviction_count(self) -> int:
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM eviction_queue WHERE resolved = FALSE")

    # ── Approval workflow ──────────────────────────────────────────────────

    async def queue_pending_write(
        self,
        name: str,
        title: str = "",
        description: str = "",
        type: str = "project",
        body: str = "",
        tags: list | None = None,
        origin_clients: list | None = None,
        proposed_by: str = "api",
        source: str = "",
        provenance: str | None = None,
        confidence: float | None = None,
        focus_mode: str = "",
        tier: str = "",
    ) -> str:
        """Insert or update a pending write proposal for an existing memory.

        On a second proposal for the same name (while a pending row exists),
        the existing pending row is UPDATED — latest candidate wins, no duplicate
        pileup. Uses INSERT … ON CONFLICT DO UPDATE on the unique constraint
        uq_pending_writes_name_pending (memory_name, status='pending').

        Captures existing_body at enqueue time so the review UI can diff.
        """
        self._ensure_pool()
        tags_v = _tags_json(tags)
        clients_v = _tags_json(origin_clients)
        now = _now_utc()

        # Serialise provenance to JSON string if it's a dict/list.
        if provenance is not None and not isinstance(provenance, str):
            provenance_str = json.dumps(provenance)
        else:
            provenance_str = provenance

        # Capture existing_body for diff.
        existing_body: str | None = None
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("SELECT body FROM memories WHERE name = $1", name)
                if row:
                    existing_body = row["body"]
        except Exception:
            pass  # non-fatal

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO pending_writes
                    (memory_name, title, description, type, body, tags,
                     origin_session_ids, origin_clients, proposed_by, proposed_at,
                     source, provenance, confidence, focus_mode, existing_body, tier)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8::jsonb, $9, $10,
                        $11, $12, $13, $14, $15, $16)
                ON CONFLICT (memory_name) WHERE status = 'pending'
                DO UPDATE SET
                    title          = EXCLUDED.title,
                    description    = EXCLUDED.description,
                    type           = EXCLUDED.type,
                    body           = EXCLUDED.body,
                    tags           = EXCLUDED.tags,
                    origin_clients = EXCLUDED.origin_clients,
                    proposed_by    = EXCLUDED.proposed_by,
                    proposed_at    = EXCLUDED.proposed_at,
                    source         = EXCLUDED.source,
                    provenance     = EXCLUDED.provenance,
                    confidence     = EXCLUDED.confidence,
                    focus_mode     = EXCLUDED.focus_mode,
                    existing_body  = EXCLUDED.existing_body,
                    tier           = EXCLUDED.tier
                """,
                name,
                title,
                description,
                type,
                body,
                tags_v,
                "[]",
                clients_v,
                proposed_by,
                now,
                source or "",
                provenance_str,
                confidence,
                focus_mode or "",
                existing_body,
                tier or "",
            )
        return (
            f"Memory '{name}' queued as pending write "
            "(dreamer review required via review.html or POST /api/memories/{name}/approve)."
        )

    async def pending_list(self, status: str = "pending") -> str:
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, memory_name, title, proposed_by, proposed_at, status "
                "FROM pending_writes WHERE status = $1 ORDER BY proposed_at DESC",
                status,
            )
        if not rows:
            return f"No {status} writes."
        lines = [
            f"[{r['id']}] {r['memory_name'] or '(new)'} — {r['title']} (by {r['proposed_by']})"
            for r in rows
        ]
        return "\n".join(lines)

    async def pending_list_json(
        self,
        status: str = "pending",
        proposed_by: str = "",
    ) -> list[dict]:
        """Return pending writes as a list of dicts (structured, for review UI).

        Args:
            status:      Filter to this status value.  Pass ``""`` or ``None`` to
                         return rows across ALL statuses (approved + pending + rejected).
            proposed_by: When non-empty, restrict results to rows where
                         ``proposed_by`` matches exactly (used by #16 agent endpoint).
        """
        self._ensure_pool()

        # Build WHERE clause and positional-parameter list dynamically so the
        # dreamer review path (single status, all proposers) and the agent
        # self-view path (all statuses, own rows only) share one method.
        conditions: list[str] = []
        params: list = []
        idx = 1  # Postgres uses $1, $2, …

        if status:
            conditions.append(f"status = ${idx}")
            params.append(status)
            idx += 1

        if proposed_by:
            conditions.append(f"proposed_by = ${idx}")
            params.append(proposed_by)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, memory_name, title, description, type, body, tags,
                       proposed_at, proposed_by, status,
                       source, provenance, confidence, focus_mode, existing_body,
                       tier, created_at
                FROM pending_writes
                {where}
                ORDER BY proposed_at ASC
                """,
                *params,
            )
        result = []
        for r in rows:
            tags = json.loads(r["tags"]) if r["tags"] else []
            prov = r["provenance"]
            if isinstance(prov, str):
                try:
                    prov = json.loads(prov)
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(
                {
                    "id": r["id"],
                    "name": r["memory_name"],
                    "title": r["title"],
                    "description": r["description"],
                    "type": r["type"],
                    "body": r["body"] or "",
                    "tags": tags,
                    "source": r["source"] or "",
                    "provenance": prov,
                    "confidence": r["confidence"],
                    "focus_mode": r["focus_mode"] or "",
                    "existing_body": r["existing_body"],
                    "tier": r["tier"] or "",
                    "proposed_at": r["proposed_at"].isoformat() if r["proposed_at"] else None,
                    "proposed_by": r["proposed_by"],
                    "status": r["status"],
                    "created_at": (
                        r["created_at"].isoformat()
                        if r["created_at"]
                        else (r["proposed_at"].isoformat() if r["proposed_at"] else None)
                    ),
                }
            )
        return result

    async def approve(self, write_id: int, note: str = "", reviewer: str = "") -> str:
        """Approve a pending write. Race-safe: uses SELECT … FOR UPDATE inside a transaction
        so concurrent approvals cannot both apply the same pending write.

        Two-phase agent-intake gate: a row with ``source='agent-intake'`` is
        recorded as a **vote** (``status='human_approved'``) rather than written
        to canon here — the bridge finalizer re-runs GOV-002 against the trusted
        intake ticket before promoting.  All other sources apply on approve.
        """
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Lock the row so a concurrent approve cannot race past this check.
                row = await conn.fetchrow(
                    "SELECT * FROM pending_writes WHERE id = $1 AND status = 'pending' FOR UPDATE",
                    write_id,
                )
                if not row:
                    return f"Pending write {write_id} not found or already processed."

                if (row.get("source") or "").strip() == "agent-intake":
                    # VOTE ONLY — defer the canon write to the bridge finalizer.
                    await conn.execute(
                        "UPDATE pending_writes SET status='human_approved', "
                        "review_note=$2, reviewed_by=$3, reviewed_at=$4 WHERE id=$1",
                        write_id,
                        note,
                        reviewer,
                        _now_utc(),
                    )
                    return (
                        f"Pending write {write_id} (agent-intake) approved — queued for the "
                        "bridge finalizer (GOV-002 re-check, then canon write with lineage)."
                    )

                await self.write(
                    name=row["memory_name"],
                    title=row["title"],
                    description=row["description"],
                    type=row["type"],
                    tier=row.get("tier", "working"),
                    body=row["body"],
                    tags=json.loads(row["tags"] or "[]"),
                    provenance=Provenance(
                        actor="governed-promotion", source="store:approve", op="approve"
                    ),
                    _skip_protection=True,
                    _conn=conn,
                )
                await conn.execute(
                    "UPDATE pending_writes SET status='approved', review_note=$2, reviewed_by=$3, reviewed_at=$4 WHERE id=$1",
                    write_id,
                    note,
                    reviewer,
                    _now_utc(),
                )
        return f"Pending write {write_id} approved and committed."

    async def reject(self, write_id: int, note: str = "", reviewer: str = "") -> str:
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE pending_writes SET status='rejected', review_note=$2, reviewed_by=$3, reviewed_at=$4 WHERE id=$1",
                write_id,
                note,
                reviewer,
                _now_utc(),
            )
        return f"Pending write {write_id} rejected."

    async def set_pending_status(
        self, write_id: int, status: str, note: str = "", reviewer: str = ""
    ) -> None:
        """Force a pending_write to *status* (any → any). Bridge finalizer use."""
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE pending_writes SET status=$2, review_note=$3, "
                "reviewed_by=$4, reviewed_at=$5 WHERE id=$1",
                write_id,
                status,
                note,
                reviewer or "bridge-finalizer",
                _now_utc(),
            )

    async def protect(self, name: str, domains=None) -> str:
        self._ensure_pool()
        domains_v = _tags_json(domains)
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE memories SET protected=TRUE, protected_domains=$2::jsonb WHERE name=$1 AND deleted_at IS NULL",
                name,
                domains_v,
            )
        return f"Memory '{name}' protected"

    # ── Freshness and eviction ─────────────────────────────────────────────

    async def check_freshness(self, llm_consult, limit: int = 20) -> dict:
        """Run freshness validation on canonical memories tagged with
        infrastructure/dependency/tooling/config tags.

        Improvements over the original sequential implementation:
        - **24h in-memory cache**: shared with the SQLite path via
          ``memory_store._freshness_cache`` — skips the LLM call when a
          cached result is less than 24 hours old.
        - **Bounded concurrency**: up to 5 concurrent LLM calls via
          ``asyncio.gather`` + ``asyncio.Semaphore(5)``.  All calls happen
          outside the pool connection so the pool is not held during LLM I/O.
        - **Single batched UPDATE**: all status changes are applied in one
          acquired connection, not one ``pool.acquire()`` per memory.

        NOTE: Moving this call off the brief() hot path into a background task
        is the next recommended improvement (tracked as follow-up).
        """
        self._ensure_pool()
        cand_tag_patterns = ["infrastructure", "dependency", "tooling", "config"]

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM memories
                WHERE tier = 'canonical'
                  AND freshness_status IN ('unknown', 'fresh')
                  AND deleted_at IS NULL
                  AND EXISTS (
                      SELECT 1 FROM jsonb_array_elements_text(tags) AS elem
                      WHERE elem = ANY($1::text[])
                  )
                ORDER BY freshness_checked_at IS NULL DESC, freshness_checked_at ASC
                LIMIT $2
                """,
                cand_tag_patterns,
                limit,
            )
        # Decode rows; connection released before any LLM calls.
        all_mems = []
        for row in rows:
            r = dict(row)
            raw = r.get("tags")
            r["tags"] = json.loads(raw or "[]") if isinstance(raw, str) else (raw or [])
            all_mems.append(r)

        results = {"checked": 0, "fresh": 0, "stale": 0, "no": 0, "errors": 0}

        # Separate cache hits from memories that need an LLM call.
        # Use _freshness_cache_lock for all cache reads and writes to prevent
        # concurrent misses on the same memory firing duplicate LLM calls
        # (thundering-herd). The lock is shared with the SQLite path.
        now = time.monotonic()
        mems_to_check: list[dict] = []
        for m in all_mems:
            with _freshness_cache_lock:
                cached = _freshness_cache.get(m["name"])
                if cached is not None:
                    cached_status, cached_at = cached
                    # In-flight sentinel: another coroutine already owns this check.
                    if cached_status == _IN_FLIGHT_SENTINEL:
                        continue  # skip; owning coroutine will count it
                    if (now - cached_at) < _FRESHNESS_CACHE_TTL:
                        results["checked"] += 1
                        results[cached_status] += 1
                        continue
                # Cache miss (or expired): mark in-flight before releasing lock.
                _freshness_cache[m["name"]] = (_IN_FLIGHT_SENTINEL, now)
            mems_to_check.append(m)

        if not mems_to_check:
            return results

        # Bounded concurrency — at most 5 LLM calls in flight at once.
        sem = asyncio.Semaphore(5)

        async def _check_one(m: dict) -> tuple[str, str | None]:
            async with sem:
                try:
                    prompt = FRESHNESS_CHECK_PROMPT.format(
                        title=m["title"],
                        tags=", ".join(m["tags"]),
                        body=(m["body"] or "")[:2000],
                    )
                    # llm_consult is synchronous (BifrostClient.consult).
                    # Run it in the default thread-pool executor so we don't
                    # block the event loop.
                    loop = asyncio.get_event_loop()
                    response = await loop.run_in_executor(
                        None,
                        lambda: llm_consult(
                            system=prompt,
                            user=m["name"],
                            vk="fast",
                            max_tokens=10,
                            temperature=0.0,
                        ),
                    )
                    status = (response or "").strip().upper()
                    normalized = "fresh"
                    if status == "NO":
                        normalized = "no"
                    elif status == "STALE":
                        normalized = "stale"
                    return m["name"], normalized
                except Exception as exc:
                    logger.warning("Freshness check failed for '%s': %s", m["name"], exc)
                    return m["name"], None

        check_results = await asyncio.gather(*[_check_one(m) for m in mems_to_check])

        updates: list[tuple[str, str]] = []
        checked_at = datetime.now(timezone.utc)
        for name, normalized in check_results:
            if normalized is None:
                results["errors"] += 1
                # Clear the in-flight sentinel on error so future calls retry.
                with _freshness_cache_lock:
                    if _freshness_cache.get(name, (None,))[0] == _IN_FLIGHT_SENTINEL:
                        del _freshness_cache[name]
            else:
                updates.append((name, normalized))
                results["checked"] += 1
                results[normalized] += 1
                # Store real result — replaces the in-flight sentinel.
                with _freshness_cache_lock:
                    _freshness_cache[name] = (normalized, time.monotonic())

        # Apply all status changes in a single connection — one pool acquire.
        if updates:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    for name, normalized in updates:
                        await conn.execute(
                            "UPDATE memories SET freshness_status = $1, "
                            "freshness_checked_at = $2 WHERE name = $3",
                            normalized,
                            checked_at,
                            name,
                        )

        return results

    async def scan_orphans(self, days: int = 30, dry_run: bool = True) -> str:
        self._ensure_pool()
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT name, title, last_retrieved_at FROM memories "
                "WHERE tier = 'working' AND protected = FALSE AND deleted_at IS NULL "
                "AND (last_retrieved_at IS NULL OR last_retrieved_at < $1)",
                cutoff,
            )
        if not rows:
            return f"No orphan memories older than {days} days."
        names = [r["name"] for r in rows]
        if not dry_run:
            async with self.pool.acquire() as conn:
                await conn.execute("DELETE FROM memories WHERE name = ANY($1)", names)
            return f"Deleted {len(names)} orphan memories"
        return f"Would delete {len(names)} orphan memories:\n" + "\n".join(f"- {n}" for n in names)

    # ── Internal helpers (transitional) ────────────────────────────────────

    async def get_config(self, key: str, default: str = "") -> str:
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM dreamer_config WHERE key = $1", key)
        return row["value"] if row else default

    async def is_trusted_client(self, client) -> bool:
        trusted_raw = await self.get_config("trusted_clients", "[]")
        try:
            trusted = json.loads(trusted_raw)
        except (json.JSONDecodeError, TypeError):
            trusted = []
        return client in trusted

    def parse_tags(self, raw: str) -> list:
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return [t.strip() for t in raw.split(",") if t.strip()]

    # ── Session / dream ────────────────────────────────────────────────────

    async def append_event(
        self,
        session_id,
        event_name,
        client="",
        tool_name=None,
        tool_input=None,
        tool_response=None,
        tool_error=None,
        model=None,
        cwd=None,
        transcript_path=None,
        prompt=None,
        stop_reason=None,
        assistant_text=None,
    ) -> int:
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO session_events
                   (session_id, event_name, client, timestamp, tool_name, tool_input,
                    tool_response, tool_error, model, cwd, transcript_path, prompt, stop_reason,
                    assistant_text)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14) RETURNING id""",
                session_id,
                event_name,
                client,
                _now_utc(),
                tool_name,
                tool_input,
                tool_response,
                tool_error,
                model,
                cwd,
                transcript_path,
                prompt,
                stop_reason,
                assistant_text,
            )
        return row["id"]

    async def append_event_dict(self, session_id: str, event_type: str, data=None) -> None:
        await self.append_event(
            session_id=session_id,
            event_name=event_type,
            tool_input=json.dumps(data) if data else None,
        )

    async def read_events(
        self, session_id=None, since_event_id=None, since=None, client=None, limit=None
    ) -> list:
        self._ensure_pool()
        clauses = []
        params: list[Any] = []
        i = 1
        if session_id:
            clauses.append(f"session_id = ${i}")
            params.append(session_id)
            i += 1
        if since_event_id:
            clauses.append(f"id > ${i}")
            params.append(since_event_id)
            i += 1
        if since:
            clauses.append(f"timestamp >= ${i}")
            params.append(_ts(since))
            i += 1
        if client:
            clauses.append(f"client = ${i}")
            params.append(client)
            i += 1
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        if limit is None:
            # Explicit unlimited — no LIMIT clause.
            lim_clause = ""
        elif limit > 0:
            lim_clause = f"LIMIT ${i}"
            params.append(limit)
        else:
            # limit == 0 → empty result without hitting the database.
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM session_events {where} ORDER BY id ASC {lim_clause}", *params
            )
        return [dict(r) for r in rows]

    async def read_events_grouped(self, since_event_id=None, group_limit=5) -> list:
        self._ensure_pool()
        where = "WHERE id > $1" if since_event_id else ""
        params = [since_event_id] if since_event_id else []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM session_events {where} ORDER BY session_id, id ASC", *params
            )
        grouped: dict = {}
        for r in rows:
            sid = r["session_id"]
            if sid not in grouped:
                grouped[sid] = []
            if len(grouped[sid]) < group_limit:
                grouped[sid].append(dict(r))
        return list(grouped.values())

    async def get_dream_state(self, key: str, default=None):
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM dream_state WHERE key = $1", key)
        return row["value"] if row else default

    async def set_dream_state(self, key: str, value: str, _conn=None) -> None:
        self._ensure_pool()

        async def _do(conn):
            await conn.execute(
                "INSERT INTO dream_state (key, value, updated_at) VALUES ($1,$2,$3) "
                "ON CONFLICT (key) DO UPDATE SET value=$2, updated_at=$3",
                key,
                value,
                _now_utc(),
            )

        if _conn:
            await _do(_conn)
        else:
            async with self.pool.acquire() as conn:
                await _do(conn)

    async def count_events(self) -> int:
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM session_events")

    async def count_events_since(self, since_event_id: int) -> int:
        """Count events with id > since_event_id — O(1) memory, no row fetch."""
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM session_events WHERE id > $1",
                since_event_id,
            )

    async def prune_events(self, before_event_id: int, _conn=None) -> int:
        self._ensure_pool()

        async def _do(conn):
            result = await conn.execute("DELETE FROM session_events WHERE id < $1", before_event_id)
            return int(result.split()[-1])

        if _conn:
            return await _do(_conn)
        async with self.pool.acquire() as conn:
            return await _do(conn)

    async def list_sessions(self) -> list:
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT session_id, MIN(timestamp) AS started, MAX(timestamp) AS last, COUNT(*) AS events "
                "FROM session_events GROUP BY session_id ORDER BY last DESC"
            )
        return [dict(r) for r in rows]

    # ── Ingestion log ──────────────────────────────────────────────────────

    async def is_ingested_by_hash(self, file_hash: str, status_filter=None) -> bool:
        self._ensure_pool()
        params: list[Any] = [file_hash]
        clause = ""
        if status_filter:
            clause = " AND status = $2"
            params.append(status_filter)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT id FROM ingestion_log WHERE source_hash = $1 AND dry_run = FALSE{clause}",
                *params,
            )
        return row is not None

    async def log_ingestion(
        self,
        source_path,
        source_hash,
        memories_written=0,
        model="",
        focus="all",
        tier="working",
        tags=None,
        dry_run=False,
        error_count=0,
        status="committed",
        candidates_total=None,
        convention_ratio=None,
        anchorable_pct=None,
    ) -> None:
        self._ensure_pool()
        tags_v = _tags_json(tags)
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO ingestion_log
                   (source_path, source_hash, memories_written, model, focus, tier,
                    tags, dry_run, error_count, status, ingested_at,
                    candidates_total, convention_ratio, anchorable_pct)
                   VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,$11,$12,$13,$14)""",
                source_path,
                source_hash,
                memories_written,
                model,
                focus,
                tier,
                tags_v,
                dry_run,
                error_count,
                status,
                _now_utc(),
                candidates_total,
                convention_ratio,
                anchorable_pct,
            )

    async def get_ingestion_status(self, limit: int = 20) -> str:
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT source_path, ingested_at, memories_written, model, focus, tier, dry_run, error_count, status "
                "FROM ingestion_log ORDER BY ingested_at DESC LIMIT $1",
                limit,
            )
        if not rows:
            return "No ingestion runs recorded."
        lines = [
            "| Source | When | Memories | Model | Focus | Tier | Status |",
            "|--------|------|----------|-------|-------|------|--------|",
        ]
        for row in rows:
            r = dict(row)
            src = Path(r["source_path"]).name if r["source_path"] else "?"
            when = str(r["ingested_at"])[:16] if r["ingested_at"] else "?"
            dry = " (dry)" if r["dry_run"] else ""
            err = f" ({r['error_count']} err)" if r["error_count"] else ""
            lines.append(
                f"| {src} | {when} | {r['memories_written']}{err} | {r['model'] or '—'} | {r['focus']} | {r['tier']} | {r['status']}{dry} |"
            )
        return "\n".join(lines)

    async def count_ingestion(self) -> int:
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM ingestion_log")

    async def latest_ingestion_shape(self) -> dict | None:
        """Most-recent committed ingest's shape metrics (measurement layer), or None."""
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT candidates_total, convention_ratio, anchorable_pct "
                "FROM ingestion_log WHERE status='committed' AND candidates_total IS NOT NULL "
                "ORDER BY id DESC LIMIT 1"
            )
        if not row:
            return None
        return {
            "candidates_total": row["candidates_total"],
            "convention_ratio": row["convention_ratio"],
            "anchorable_pct": row["anchorable_pct"],
        }

    async def canon_mortality_rate(self, days: int = 90) -> float | None:
        """Cohort mortality: share of canonical memories created >`days` ago never
        retrieved (retrieval_count=0). None if no eligible cohort. A rate, not a count
        (measurement layer). Served by the idx_memories_canon_mortality composite index."""
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COUNT(*) AS total, "
                "COUNT(*) FILTER (WHERE retrieval_count = 0) AS dead "
                "FROM memories WHERE tier='canonical' AND deleted_at IS NULL "
                "AND created_at < NOW() - ($1 || ' days')::INTERVAL",
                str(int(days)),
            )
        total = row["total"] or 0
        if not total:
            return None
        return round(row["dead"] / total, 3)

    async def audit_governance_stats(self, days: int = 7) -> dict:
        """write_audit-derived governance metrics (measurement layer b+d):
        td_reason distribution + coverage over approve/reject, and net_canon_growth
        (approvals - rejections - deletions) over the last `days`."""
        self._ensure_pool()
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT reason_code, COUNT(*) AS n FROM write_audit "
                    "WHERE op IN ('approve','reject') GROUP BY reason_code"
                )
                net = await conn.fetchval(
                    "SELECT COUNT(*) FILTER (WHERE op='approve') "
                    "- COUNT(*) FILTER (WHERE op='reject') "
                    "- COUNT(*) FILTER (WHERE op LIKE '%delete%') "
                    "FROM write_audit WHERE ts >= NOW() - ($1 || ' days')::INTERVAL",
                    str(int(days)),
                )
        except Exception as exc:
            if "write_audit" in str(exc).lower() or "does not exist" in str(exc).lower():
                return {}
            raise
        dist: dict[str, int] = {}
        total = reasoned = 0
        for r in rows:
            total += r["n"]
            if r["reason_code"]:
                dist[r["reason_code"]] = r["n"]
                reasoned += r["n"]
        return {
            "td_reason": dist,
            "td_total": total,
            "td_reasoned": reasoned,
            "net_canon_growth": net or 0,
        }

    # ── Msg ────────────────────────────────────────────────────────────────

    async def log_message(self, msg, status: str = "pending") -> None:
        self._ensure_pool()
        ts = _ts(msg.ts) if isinstance(msg.ts, str) else msg.ts
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO msg_log (id, from_host, to_host, type, ts, body, reply_to, status)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (id) DO NOTHING""",
                msg.id,
                msg.from_agent,
                msg.to,
                msg.type,
                ts,
                msg.body,
                msg.reply_to,
                status,
            )

    async def set_message_status(self, msg_id: str, status: str) -> None:
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE msg_log SET status=$2 WHERE id=$1", msg_id, status)

    async def get_pending_messages(
        self, hostname, types=None, from_host=None, unacked=False, include_broadcast=True
    ) -> list:
        self._ensure_pool()
        clauses = []
        params: list[Any] = []
        i = 1
        if include_broadcast:
            clauses.append(f"(to_host = ${i} OR to_host = 'broadcast')")
        else:
            clauses.append(f"to_host = ${i}")
        params.append(hostname)
        i += 1
        if types:
            placeholders = ",".join(f"${j}" for j in range(i, i + len(types)))
            clauses.append(f"type IN ({placeholders})")
            params.extend(types)
            i += len(types)
        if from_host:
            clauses.append(f"from_host = ${i}")
            params.append(from_host)
            i += 1
        if unacked:
            clauses.append("status = 'pending'")
        where = " AND ".join(clauses)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM msg_log WHERE {where} ORDER BY ts DESC", *params
            )
        return [dict(r) for r in rows]

    async def get_message_thread(self, root_id: str) -> list:
        """Return root + replies. root_id may be a full UUID or 8-char prefix.

        Timestamps: asyncpg returns TIMESTAMPTZ columns as Python datetime objects.
        This method coerces all datetime values to ISO-8601 strings before returning
        so callers can safely subscript them (e.g. ``row["ts"][:16]``), matching
        the contract of the SQLite backend which stores ts as TEXT.
        """
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            root = await conn.fetchrow("SELECT * FROM msg_log WHERE id = $1", root_id)
            if not root and len(root_id) <= 8:
                root = await conn.fetchrow("SELECT * FROM msg_log WHERE id LIKE $1", root_id + "%")
            if not root:
                return []
            full_id = dict(root)["id"]
            replies = await conn.fetch(
                "SELECT * FROM msg_log WHERE reply_to = $1 ORDER BY ts", full_id
            )
        return [_coerce_msg_row(root)] + [_coerce_msg_row(r) for r in replies]

    async def count_messages(self, status: str | None = None) -> int:
        self._ensure_pool()
        q = "SELECT COUNT(*) FROM msg_log"
        params = []
        if status is not None:
            q += " WHERE status = $1"
            params.append(status)

        async with self.pool.acquire() as conn:
            return await conn.fetchval(q, *params)

    # ── Ad-hoc query helpers ───────────────────────────────────────────────

    async def get_unresolved_goals(self) -> list:
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT name, title, tags, description FROM memories
                   WHERE type = 'requirement'
                   AND (tags @> '["status-pending"]'::jsonb OR tags @> '["status-in-progress"]'::jsonb)
                   ORDER BY updated_at DESC"""
            )
        return [
            {
                "name": r["name"],
                "title": r["title"],
                "tags": json.loads(r["tags"] or "[]"),
                "description": r["description"],
            }
            for r in rows
        ]

    async def get_stale_memories(self, days: int = 90) -> list:
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT name, title, type, last_retrieved_at, retrieval_count
                   FROM memories
                   WHERE tier = 'working'
                   AND last_retrieved_at IS NOT NULL
                   AND last_retrieved_at < NOW() - ($1 || ' days')::INTERVAL
                   AND protected = FALSE
                   ORDER BY last_retrieved_at ASC""",
                str(days),
            )
        return [
            {
                "name": r["name"],
                "title": r["title"],
                "type": r["type"],
                "last_retrieved_at": str(r["last_retrieved_at"]),
                "retrieval_count": r["retrieval_count"],
            }
            for r in rows
        ]

    async def get_superseded_memories(self) -> list:
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT name, title, superseded_by, updated_at FROM memories "
                "WHERE superseded_by IS NOT NULL AND superseded_by != '' AND deleted_at IS NULL ORDER BY updated_at DESC"
            )
        return [
            {
                "name": r["name"],
                "title": r["title"],
                "superseded_by": r["superseded_by"],
                "updated_at": str(r["updated_at"]),
            }
            for r in rows
        ]

    async def get_eviction_summary(self) -> list:
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, memory_name, reason, detail, detected_at FROM eviction_queue "
                "WHERE resolved = FALSE ORDER BY detected_at DESC"
            )
        return [
            {
                "id": r["id"],
                "memory_name": r["memory_name"],
                "reason": r["reason"],
                "detail": r["detail"],
                "detected_at": str(r["detected_at"]),
            }
            for r in rows
        ]

    async def get_stale_canonical_memories(self) -> list:
        """Return stale/invalid canonical memories."""
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT name, title, freshness_status, freshness_checked_at FROM memories "
                "WHERE freshness_status IN ('stale', 'no') AND deleted_at IS NULL ORDER BY freshness_checked_at DESC"
            )
        return [
            {
                "name": r["name"],
                "title": r["title"],
                "freshness_status": r["freshness_status"],
                "freshness_checked_at": str(r["freshness_checked_at"])
                if r["freshness_checked_at"]
                else None,
            }
            for r in rows
        ]

    async def get_eviction_queue_summary(self) -> list:
        """Return a count of eviction queue entries grouped by reason."""
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT reason, COUNT(*) as total, SUM(CASE WHEN resolved THEN 1 ELSE 0 END) as resolved "
                "FROM eviction_queue GROUP BY reason"
            )
        return [
            {
                "reason": r["reason"],
                "total": r["total"],
                "resolved": r["resolved"] or 0,
            }
            for r in rows
        ]

    # ── Agent-memory intake lineage ────────────────────────────────────────

    async def record_intake_lineage(
        self,
        canon_name: str,
        intake_candidate_id: str,
        intake_submission_ids: list[str],
        trust_snapshot: dict,
        promoted_at,
    ) -> None:
        """Idempotently insert a ``memory_intake_lineage`` row (Postgres).

        Uses ``ON CONFLICT (canon_name) DO NOTHING`` so a re-drive after a
        crash that already wrote the lineage row simply does nothing — no
        duplicate, no error.
        """
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO memory_intake_lineage
                    (canon_name, intake_candidate_id, intake_submission_ids,
                     trust_snapshot, promoted_at)
                VALUES ($1, $2::uuid, $3::uuid[], $4, $5)
                ON CONFLICT (canon_name) DO NOTHING
                """,
                canon_name,
                intake_candidate_id,
                intake_submission_ids,
                json.dumps(trust_snapshot),
                promoted_at,
            )

    async def get_intake_lineage(self, canon_name: str) -> dict | None:
        """Return the ``memory_intake_lineage`` row for *canon_name*, or None."""
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT canon_name, intake_candidate_id, intake_submission_ids, "
                "trust_snapshot, promoted_at "
                "FROM memory_intake_lineage WHERE canon_name = $1",
                canon_name,
            )
        if row is None:
            return None
        trust = row["trust_snapshot"]
        if isinstance(trust, str):
            try:
                trust = json.loads(trust)
            except (json.JSONDecodeError, TypeError):
                trust = {}
        sub_ids = row["intake_submission_ids"]
        if isinstance(sub_ids, str):
            try:
                sub_ids = json.loads(sub_ids)
            except (json.JSONDecodeError, TypeError):
                sub_ids = []
        promoted = row["promoted_at"]
        if hasattr(promoted, "isoformat"):
            promoted = promoted.isoformat()
        return {
            "canon_name": row["canon_name"],
            "intake_candidate_id": str(row["intake_candidate_id"]),
            "intake_submission_ids": [str(s) for s in (sub_ids or [])],
            "trust_snapshot": trust,
            "promoted_at": promoted,
        }

    async def record_intake_ticket(
        self,
        ticket_uuid: str,
        canon_name: str,
        candidate_id: str,
        submission_ids: list[str],
        trust_snapshot: dict,
        body_hash: str,
    ) -> None:
        """Idempotently insert an ``intake_promotion_tickets`` row (Postgres)."""
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO intake_promotion_tickets
                    (ticket_uuid, canon_name, candidate_id, submission_ids,
                     trust_snapshot, body_hash)
                VALUES ($1::uuid, $2, $3::uuid, $4::uuid[], $5, $6)
                ON CONFLICT (ticket_uuid) DO NOTHING
                """,
                ticket_uuid,
                canon_name,
                candidate_id,
                submission_ids,
                json.dumps(trust_snapshot),
                body_hash,
            )

    async def get_intake_ticket(self, ticket_uuid: str) -> dict | None:
        """Return the ``intake_promotion_tickets`` row for *ticket_uuid*, or None."""
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT ticket_uuid, canon_name, candidate_id, submission_ids, "
                "trust_snapshot, body_hash, created_at "
                "FROM intake_promotion_tickets WHERE ticket_uuid = $1::uuid",
                ticket_uuid,
            )
        if row is None:
            return None
        trust = row["trust_snapshot"]
        if isinstance(trust, str):
            try:
                trust = json.loads(trust)
            except (json.JSONDecodeError, TypeError):
                trust = {}
        sub_ids = row["submission_ids"]
        created = row["created_at"]
        if hasattr(created, "isoformat"):
            created = created.isoformat()
        return {
            "ticket_uuid": str(row["ticket_uuid"]),
            "canon_name": row["canon_name"],
            "candidate_id": str(row["candidate_id"]),
            "submission_ids": [str(s) for s in (sub_ids or [])],
            "trust_snapshot": trust,
            "body_hash": row["body_hash"],
            "created_at": created,
        }

    def canon_reader(self):
        """Return a ``(search, fetch_body)`` pair of read-only **async** callables.

        Both callables are coroutines that must be ``await``-ed by the caller.
        They delegate to the existing async pool methods and do NOT bump
        ``retrieval_count`` — pure read access.

        Returns
        -------
        search : ``async (query: str, limit: int) -> list[dict]``
            Delegates to ``self.search_json(query=query, limit=limit)``.
            Returns a list of dicts with at least ``name`` and ``tier`` keys.
        fetch_body : ``async (name: str) -> str``
            Returns the full ``body`` text of a single memory, or ``""`` when
            not found.  Delegates to ``self.get_memory(name)`` which does NOT
            bump ``retrieval_count``.
        """
        from mori_intake.assess_model import CanonReader

        async def _search(query: str, limit: int) -> list[dict]:
            return await self.search_json(query=query, limit=limit)

        async def _fetch_body(name: str) -> str:
            mem = await self.get_memory(name)
            return (mem or {}).get("body") or ""

        return CanonReader(search=_search, fetch_body=_fetch_body)

    async def get_requirements(
        self, project: str = "", status: str = "", tag: str = "", limit: int = 50
    ) -> list:
        """Return requirement memories filtered by project, status, or tag."""
        self._ensure_pool()
        import json

        clauses = ["type = 'requirement'"]
        params = []
        i = 1
        if tag:
            clauses.append(f"tags @> ${i}::jsonb")
            params.append(json.dumps([tag]))
            i += 1
        else:
            if project:
                clauses.append(f"tags @> ${i}::jsonb")
                params.append(json.dumps([f"project-{project}"]))
                i += 1
            if status:
                clauses.append(f"tags @> ${i}::jsonb")
                params.append(json.dumps([f"status-{status}"]))
                i += 1

        where = " AND ".join(clauses)
        params.append(limit)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT name, title, tags, description, body FROM memories WHERE {where} ORDER BY name ASC LIMIT ${i}",
                *params,
            )
        return [
            {
                "name": r["name"],
                "title": r["title"],
                "tags": r["tags"],
                "description": r["description"],
                "body": r["body"],
            }
            for r in rows
        ]
