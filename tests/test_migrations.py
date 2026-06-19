"""Tests for the schema-migration runner (mori_advisor.store.migrations).

SQLite is always exercised. Postgres is exercised only when MORI_TEST_DATABASE_URL
is set (CI service / dev box); otherwise those tests skip, keeping local `pytest`
fast and dependency-free.
"""

import asyncio
import os
import sqlite3
import threading
from pathlib import Path

import pytest

from mori_advisor.store.migrations import MIGRATIONS, apply_sqlite

PG_URL = os.environ.get("MORI_TEST_DATABASE_URL", "")
requires_pg = pytest.mark.skipif(not PG_URL, reason="MORI_TEST_DATABASE_URL not set")

MEMORIES_MIGS = tuple(m for m in MIGRATIONS if m.target == "memories")
MSG_MIGS = tuple(m for m in MIGRATIONS if m.target == "msg")


def _ids(db: Path) -> list[int]:
    c = sqlite3.connect(str(db))
    try:
        return [r[0] for r in c.execute("SELECT id FROM schema_migrations ORDER BY id")]
    finally:
        c.close()


def _tables(db: Path) -> set[str]:
    c = sqlite3.connect(str(db))
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()


# ── SQLite ──────────────────────────────────────────────────────────────────


def test_fresh_apply_stamps_all(tmp_path):
    db = tmp_path / "memories.db"
    apply_sqlite(db, MEMORIES_MIGS)
    assert _ids(db) == sorted(m.id for m in MEMORIES_MIGS)
    assert {"memories", "session_events", "dream_state", "schema_migrations"} <= _tables(db)


def test_msg_baseline_separate_file(tmp_path):
    db = tmp_path / "msg.db"
    apply_sqlite(db, MSG_MIGS)
    assert _ids(db) == sorted(m.id for m in MSG_MIGS)
    assert {"msg_log", "schema_migrations"} <= _tables(db)


def test_idempotent(tmp_path):
    db = tmp_path / "memories.db"
    apply_sqlite(db, MEMORIES_MIGS)
    first = _ids(db)
    apply_sqlite(db, MEMORIES_MIGS)  # second run must be a no-op
    assert _ids(db) == first


def test_legacy_db_stamps_baseline_and_preserves_data(tmp_path):
    """A pre-runner DB (old bootstrap, no schema_migrations, with data) must get
    baseline-stamped without error and without losing data."""
    from mori_advisor.memory_store import MemoryStore
    from mori_advisor.session_log import SessionLog

    db = tmp_path / "memories.db"
    MemoryStore.bootstrap_schema(db)
    SessionLog.bootstrap_schema(db)
    c = sqlite3.connect(str(db))
    c.execute("INSERT INTO memories (name, title) VALUES ('legacy-row', 'kept')")
    c.commit()
    c.close()
    assert "schema_migrations" not in _tables(db)  # genuinely a pre-runner DB

    apply_sqlite(db, MEMORIES_MIGS)

    assert _ids(db) == sorted(m.id for m in MEMORIES_MIGS)
    c = sqlite3.connect(str(db))
    n = c.execute("SELECT count(*) FROM memories WHERE name='legacy-row'").fetchone()[0]
    c.close()
    assert n == 1  # data preserved across baseline-stamp


def test_concurrent_bootstrap(tmp_path):
    """Many threads applying to the same file → no crash, each id stamped once."""
    db = tmp_path / "memories.db"
    errors: list[Exception] = []

    def worker():
        try:
            apply_sqlite(db, MEMORIES_MIGS)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent bootstrap raised: {errors}"
    # PK on id guarantees each migration is recorded exactly once.
    assert _ids(db) == sorted(m.id for m in MEMORIES_MIGS)


# ── Postgres (gated on MORI_TEST_DATABASE_URL) ───────────────────────────────


@requires_pg
def test_pg_fresh_idempotent_and_tables():
    from mori_advisor.store.postgres_store import PostgresStore

    async def run():
        store = PostgresStore(PG_URL)
        await store.bootstrap()
        async with store.pool.acquire() as conn:
            ids1 = [
                r["id"] for r in await conn.fetch("SELECT id FROM schema_migrations ORDER BY id")
            ]
        await store.bootstrap()  # idempotent
        async with store.pool.acquire() as conn:
            ids2 = [
                r["id"] for r in await conn.fetch("SELECT id FROM schema_migrations ORDER BY id")
            ]
            tables = {
                r["tablename"]
                for r in await conn.fetch(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public'"
                )
            }
        await store.pool.close()
        return ids1, ids2, tables

    ids1, ids2, tables = asyncio.run(run())
    assert set(m.id for m in MIGRATIONS) <= set(ids1)  # all migrations applied (one DB)
    assert ids1 == ids2  # idempotent
    assert {
        "memories",
        "session_events",
        "msg_log",
        "delegate_tasks",
        "schema_migrations",
    } <= tables


# ── Stage B drift fixes ──────────────────────────────────────────────────────


def test_drift_fixes_sqlite(tmp_path):
    db = tmp_path / "memories.db"
    apply_sqlite(db, MEMORIES_MIGS)
    c = sqlite3.connect(str(db))
    try:
        dreamer_cols = {r[1] for r in c.execute("PRAGMA table_info(dreamer_config)")}
        assert "updated_at" in dreamer_cols  # 0003
        assert "delegate_tasks" in _tables(db)  # 0004 parity
        # PRAGMA table_info columns: cid, name, type, notnull, dflt_value, pk
        mem = {r[1]: r for r in c.execute("PRAGMA table_info(memories)")}
        assert mem["freshness_status"][3] == 1  # already NOT NULL on SQLite
    finally:
        c.close()


@requires_pg
def test_pg_drift_fixes():
    from mori_advisor.store.postgres_store import PostgresStore

    async def run():
        store = PostgresStore(PG_URL)
        await store.bootstrap()
        async with store.pool.acquire() as conn:
            notnull = await conn.fetchval(
                "SELECT attnotnull FROM pg_attribute "
                "WHERE attrelid = 'memories'::regclass AND attname = 'freshness_status'"
            )
            has_delegate = await conn.fetchval("SELECT to_regclass('delegate_tasks') IS NOT NULL")
        await store.pool.close()
        return notnull, has_delegate

    notnull, has_delegate = asyncio.run(run())
    assert notnull is True  # 0005 — freshness_status now NOT NULL on Postgres
    assert has_delegate is True


# ── 0015 scope map (H2 scope router) ─────────────────────────────────────────


def test_scope_column_sqlite(tmp_path):
    db = tmp_path / "memories.db"
    apply_sqlite(db, MEMORIES_MIGS)
    c = sqlite3.connect(str(db))
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(memories)")}
        assert "scope" in cols  # 0015 — nullable scope map (JSON text on SQLite)
    finally:
        c.close()


@requires_pg
def test_pg_scope_column_and_gin():
    from mori_advisor.store.postgres_store import PostgresStore

    async def run():
        store = PostgresStore(PG_URL)
        await store.bootstrap()
        async with store.pool.acquire() as conn:
            coltype = await conn.fetchval(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name='memories' AND column_name='scope'"
            )
            has_gin = await conn.fetchval(
                "SELECT to_regclass('idx_memories_scope_tags') IS NOT NULL"
            )
        await store.pool.close()
        return coltype, has_gin

    coltype, has_gin = asyncio.run(run())
    assert coltype == "jsonb"  # 0015 — scope is JSONB on Postgres
    assert has_gin is True  # GIN on (scope->'tags') serves the set-membership filter
