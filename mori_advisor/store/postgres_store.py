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
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mori_advisor.memory_store import FRESHNESS_CHECK_PROMPT

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
    status           TEXT NOT NULL DEFAULT 'committed'
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
        _skip_protection=False,
        _conn=None,
    ) -> str:
        self._ensure_pool()
        from mori_advisor.memory_store import _slugify

        if not name and title:
            name = _slugify(title)
        elif not name:
            import time

            name = f"memory-{int(time.time())}"

        tags_v = _tags_json(tags)
        sess_ids = _tags_json(origin_session_ids)
        clients = _tags_json(origin_clients or ([client] if client else []))
        now = _now_utc()

        async def _do(conn):
            existing = await conn.fetchrow(
                "SELECT id, protected, protected_domains, tier, origin_session_ids, origin_clients FROM memories WHERE name = $1",
                name,
            )
            if existing and existing["protected"] and not _skip_protection:
                return f"Memory '{name}' is protected — use _skip_protection=True to override"

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
                   ON CONFLICT (name) DO UPDATE SET
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
            return f"Memory '{name}' written"

        if _conn:
            return await _retry(_do, _conn)
        async with self.pool.acquire() as conn:
            return await _retry(_do, conn)

    async def read(self, name: str) -> str:
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM memories WHERE name = $1", name)
            if not row:
                return f"Memory '{name}' not found"
            await conn.execute(
                "UPDATE memories SET retrieval_count = retrieval_count + 1, last_retrieved_at = $2 WHERE name = $1",
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

    async def get_memory(self, name: str) -> dict | None:
        """Return a curated detail dict for a single memory, or None if not found.

        Does NOT bump retrieval_count (browse/API access, not agent recall).
        Returns exactly the DETAIL_KEYS shape:
          name, title, type, tier, tags, description, body,
          created_at, updated_at, origin_clients, retrieval_count, freshness_status.
        """
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM memories WHERE name = $1", name)
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
        clauses = []
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
        clauses: list[str] = []
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
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            result = await conn.execute("DELETE FROM memories WHERE name = $1", name)
        deleted = int(result.split()[-1])
        return f"Memory '{name}' deleted." if deleted else f"Memory '{name}' not found."

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
            )
            imported += 1
        return f"Imported {imported} memories from {source_dir}"

    # ── Memory metadata ────────────────────────────────────────────────────

    async def get_memories_by_project(self, project: str, include_global: bool = True) -> dict:
        self._ensure_pool()
        tag_value = f"project:{project}"

        def _row_to_dict(row) -> dict:
            r = dict(row)
            raw = r.get("tags")
            r["tags"] = json.loads(raw or "[]") if isinstance(raw, str) else (raw or [])
            return r

        async with self.pool.acquire() as conn:
            project_rows = await conn.fetch(
                """
                SELECT * FROM memories
                WHERE tags @> $1::jsonb
                  AND tier IN ('canonical', 'working')
                  AND (superseded_by IS NULL OR superseded_by = '')
                ORDER BY
                  CASE tier WHEN 'canonical' THEN 0 ELSE 1 END ASC,
                  updated_at DESC
                """,
                json.dumps([tag_value]),
            )

            global_rows: list = []
            if include_global:
                global_rows = await conn.fetch(
                    """
                    SELECT * FROM memories
                    WHERE (
                        tags @> '["scope:global"]'::jsonb
                        OR tags @> '["scope:cross-project"]'::jsonb
                        OR type IN ('profile', 'pattern')
                    )
                    AND (superseded_by IS NULL OR superseded_by = '')
                    AND NOT (tags @> $1::jsonb)
                    ORDER BY tier DESC, updated_at DESC
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
                """,
                json.dumps([tag_value]),
            )

        project_memories = [_row_to_dict(r) for r in project_rows]
        global_memories = [_row_to_dict(r) for r in global_rows]

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
        q = "SELECT COUNT(*) FROM memories WHERE 1=1"
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

    async def approve(self, write_id: int, note: str = "", reviewer: str = "") -> str:
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM pending_writes WHERE id = $1", write_id)
            if not row:
                return f"Pending write {write_id} not found."
            await self.write(
                name=row["memory_name"],
                title=row["title"],
                description=row["description"],
                type=row["type"],
                tier=row["tier"],
                body=row["body"],
                tags=json.loads(row["tags"] or "[]"),
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

    async def protect(self, name: str, domains=None) -> str:
        self._ensure_pool()
        domains_v = _tags_json(domains)
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE memories SET protected=TRUE, protected_domains=$2::jsonb WHERE name=$1",
                name,
                domains_v,
            )
        return f"Memory '{name}' protected"

    # ── Freshness and eviction ─────────────────────────────────────────────

    async def check_freshness(self, llm_consult, limit: int = 20) -> dict:
        self._ensure_pool()
        cand_tag_patterns = ["infrastructure", "dependency", "tooling", "config"]

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM memories
                WHERE tier = 'canonical'
                  AND freshness_status IN ('unknown', 'fresh')
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
        # Fetch all rows, release connection before LLM calls
        mems = []
        for row in rows:
            r = dict(row)
            raw = r.get("tags")
            r["tags"] = json.loads(raw or "[]") if isinstance(raw, str) else (raw or [])
            mems.append(r)

        results = {"checked": 0, "fresh": 0, "stale": 0, "no": 0, "errors": 0}

        for m in mems:
            try:
                prompt = FRESHNESS_CHECK_PROMPT.format(
                    title=m["title"],
                    tags=", ".join(m["tags"]),
                    body=(m["body"] or "")[:2000],
                )
                response = llm_consult(
                    system=prompt,
                    user=m["name"],
                    vk="fast",
                    max_tokens=10,
                    temperature=0.0,
                )
                status = (response or "").strip().upper()
                normalized = "fresh"
                if status == "NO":
                    normalized = "no"
                elif status == "STALE":
                    normalized = "stale"

                async with self.pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE memories SET freshness_status = $1, freshness_checked_at = $2 WHERE name = $3",
                        normalized,
                        datetime.now(timezone.utc),
                        m["name"],
                    )

                results["checked"] += 1
                results[normalized] += 1
            except Exception as e:
                logger.warning("Freshness check failed for '%s': %s", m["name"], e)
                results["errors"] += 1

        return results

    async def scan_orphans(self, days: int = 30, dry_run: bool = True) -> str:
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT name, title, last_retrieved_at FROM memories "
                "WHERE tier = 'working' AND protected = FALSE "
                "AND (last_retrieved_at IS NULL OR last_retrieved_at < NOW() - INTERVAL '$1 days')",
                days,
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
        lim_clause = f"LIMIT ${i}" if limit else ""
        if limit:
            params.append(limit)
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
    ) -> None:
        self._ensure_pool()
        tags_v = _tags_json(tags)
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO ingestion_log
                   (source_path, source_hash, memories_written, model, focus, tier,
                    tags, dry_run, error_count, status, ingested_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,$11)""",
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
        self._ensure_pool()
        async with self.pool.acquire() as conn:
            root = await conn.fetchrow("SELECT * FROM msg_log WHERE id = $1", root_id)
            if not root:
                return []
            replies = await conn.fetch(
                "SELECT * FROM msg_log WHERE reply_to = $1 ORDER BY ts", root_id
            )
        return [dict(root)] + [dict(r) for r in replies]

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
                "WHERE superseded_by IS NOT NULL AND superseded_by != '' ORDER BY updated_at DESC"
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
                "WHERE freshness_status IN ('stale', 'no') ORDER BY freshness_checked_at DESC"
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
