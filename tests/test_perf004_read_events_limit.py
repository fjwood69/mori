"""PERF-004 — read_events limit semantics + count_events_since tests.

Verifies:
1. count_events_since(watermark) returns the correct count without loading rows.
2. read_events(limit=0) returns [] (never fetches rows — the LIMIT 0 safe-guard).
3. read_events(limit=None) returns all rows (explicit unlimited).
4. The dream get_status path uses count_events_since — tested by confirming it
   returns the correct undreamed count and does NOT call read_events with limit=0.

All tests run against the SQLite backend.  Postgres-gated tests are skipped
unless MORI_TEST_DATABASE_URL is set.

Async tests use asyncio.run() rather than pytest-asyncio to match this repo's
existing test style (no asyncio_mode configured in pyproject.toml).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from mori_advisor.store.sqlite_store import SQLiteStore

PG_URL = os.environ.get("MORI_TEST_DATABASE_URL", "")
requires_pg = pytest.mark.skipif(not PG_URL, reason="MORI_TEST_DATABASE_URL not set")


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture()
def tmp_db(tmp_path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture()
def sqlite_store(tmp_db) -> SQLiteStore:
    store = SQLiteStore(tmp_db)
    store.bootstrap()
    return store


@pytest.fixture()
def session_log(sqlite_store):
    """Return the composed SessionLog from a bootstrapped SQLiteStore."""
    return sqlite_store._log


def _populate(log, n: int, session_id: str = "s1") -> list[int]:
    """Insert n events and return their IDs."""
    ids = []
    for i in range(n):
        eid = log.append_event(session_id=session_id, event_name=f"event_{i}")
        ids.append(eid)
    return ids


# ── count_events_since ────────────────────────────────────────────────────


def test_count_events_since_zero_watermark(session_log):
    """After inserting 5 events, count_events_since(0) == 5."""
    _populate(session_log, 5)
    assert session_log.count_events_since(0) == 5


def test_count_events_since_mid_watermark(session_log):
    """count_events_since(watermark) counts only events AFTER watermark."""
    ids = _populate(session_log, 10)
    watermark = ids[4]  # 5th event ID
    expected = 5  # events 6-10
    assert session_log.count_events_since(watermark) == expected


def test_count_events_since_at_last_event(session_log):
    """count_events_since(last_id) == 0 when no new events."""
    ids = _populate(session_log, 3)
    assert session_log.count_events_since(ids[-1]) == 0


def test_count_events_since_empty_table(session_log):
    """count_events_since on an empty table returns 0."""
    assert session_log.count_events_since(0) == 0


# ── SQLiteStore delegation ────────────────────────────────────────────────


def test_sqlite_store_count_events_since(sqlite_store):
    """SQLiteStore.count_events_since delegates correctly to SessionLog."""
    for i in range(7):
        sqlite_store.append_event(session_id="sess", event_name=f"e{i}")
    total = sqlite_store.count_events()
    assert total == 7
    # count_events_since(0) must return all 7
    assert sqlite_store.count_events_since(0) == 7
    # count_events_since with a watermark mid-stream
    # Events are returned DESC; get the 4th-highest ID as our watermark
    all_events = sqlite_store.read_events(limit=None)  # all, newest first
    fourth_id = sorted(r["id"] for r in all_events)[3]  # 4th smallest = first 4 committed
    # Events with id > fourth_id → 3 events remain (ids 5, 6, 7)
    assert sqlite_store.count_events_since(fourth_id) == 3


# ── read_events limit=0 semantics ────────────────────────────────────────


def test_read_events_limit_zero_returns_empty(session_log):
    """limit=0 must return [] even when events exist."""
    _populate(session_log, 5)
    result = session_log.read_events(limit=0)
    assert result == [], f"Expected [], got {result}"


def test_read_events_limit_none_returns_all(session_log):
    """limit=None must return all matching events."""
    _populate(session_log, 8)
    result = session_log.read_events(limit=None)
    assert len(result) == 8


def test_read_events_positive_limit_respected(session_log):
    """limit=3 must return at most 3 events."""
    _populate(session_log, 10)
    result = session_log.read_events(limit=3)
    assert len(result) == 3


def test_read_events_since_with_limit_zero_returns_empty(session_log):
    """Combining since_event_id and limit=0 must still return []."""
    ids = _populate(session_log, 5)
    result = session_log.read_events(since_event_id=ids[1], limit=0)
    assert result == []


# ── dream.get_status does not call read_events(limit=0) ──────────────────


def test_dream_get_status_uses_count_not_read_events(tmp_db):
    """DreamPipeline.get_status must call count_events_since, not read_events(limit=0).

    We spy on session_log.read_events and verify it is never called with
    limit=0 during get_status().  Uses asyncio.run() to match repo style.
    """
    from mori_advisor.dream import DreamPipeline

    store = SQLiteStore(tmp_db)
    store.bootstrap()

    # Insert some events so there's something to count.
    for i in range(5):
        store.append_event(session_id="s1", event_name=f"evt{i}")

    pipeline = DreamPipeline(db_path=tmp_db, bifrost_client=None, store=store)

    # Intercept read_events calls
    read_events_calls: list[dict] = []
    original_read_events = store._log.read_events

    def _spy_read_events(**kwargs):
        read_events_calls.append(kwargs)
        return original_read_events(**kwargs)

    store._log.read_events = _spy_read_events
    # Also update the alias used by DreamPipeline (it grabs session_log at init time)
    pipeline.session_log = store._log

    status = asyncio.run(pipeline.get_status())

    # Verify: no call with limit=0
    zero_limit_calls = [c for c in read_events_calls if c.get("limit") == 0]
    assert not zero_limit_calls, (
        f"get_status() called read_events(limit=0) — memory spike bug not fixed. "
        f"Calls: {read_events_calls}"
    )

    # Verify: the status output contains an undreamed count
    assert "Undreamed events:" in status
    assert "5" in status  # 5 events, watermark at 0


# ── Postgres: count_events_since ─────────────────────────────────────────


@requires_pg
def test_pg_count_events_since(tmp_path):
    """PostgresStore.count_events_since returns correct count."""
    from mori_advisor.store.postgres_store import PostgresStore

    async def _run():
        store = PostgresStore(PG_URL)
        await store.bootstrap()
        try:
            for i in range(6):
                await store.append_event(session_id="pg-sess-cnt", event_name=f"e{i}")
            total = await store.count_events()
            assert total >= 6
            undreamed = await store.count_events_since(0)
            assert undreamed >= 6
        finally:
            async with store.pool.acquire() as conn:
                await conn.execute("DELETE FROM session_events WHERE session_id = 'pg-sess-cnt'")

    asyncio.run(_run())


@requires_pg
def test_pg_read_events_limit_zero_returns_empty(tmp_path):
    """PostgresStore.read_events(limit=0) must return []."""
    from mori_advisor.store.postgres_store import PostgresStore

    async def _run():
        store = PostgresStore(PG_URL)
        await store.bootstrap()
        try:
            await store.append_event(session_id="pg-lim0", event_name="e1")
            result = await store.read_events(session_id="pg-lim0", limit=0)
            assert result == []
        finally:
            async with store.pool.acquire() as conn:
                await conn.execute("DELETE FROM session_events WHERE session_id = 'pg-lim0'")

    asyncio.run(_run())
