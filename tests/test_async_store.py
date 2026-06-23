"""Issue #59 — AsyncStore facade: SQLite work runs OFF the event loop on a dedicated
single-thread executor; Postgres methods are awaited directly (never off-loaded);
multi-statement transactions run as one unit on the executor via run_in_txn().

The load-bearing proof is THREAD IDENTITY (not a heartbeat): the off-loaded sync body
must run on a thread that is not the event-loop thread, where no running loop exists.
That assertion is deterministic and cannot false-pass (a heartbeat can).
"""

import asyncio
import sqlite3
import threading

import pytest
import pytest_asyncio  # noqa: F401

from mori_advisor.store.async_store import AsyncStore, _assert_off_loop, _running_loop_or_none
from mori_advisor.store.sqlite_store import SQLiteStore


@pytest.fixture
def backend(tmp_path):
    b = SQLiteStore(tmp_path / "memories.db")
    b.bootstrap()
    return b


@pytest.fixture
def store(backend):
    s = AsyncStore(backend)
    try:
        yield s
    finally:
        s.aclose()


def _names(db_path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[0] for r in conn.execute("SELECT name FROM memories WHERE deleted_at IS NULL")}
    finally:
        conn.close()


# ── thread identity: the off-loop proof ───────────────────────────────────


@pytest.mark.asyncio
async def test_offloaded_call_runs_off_the_event_loop_thread(store, backend):
    loop_thread = threading.get_ident()
    captured = {}

    def probe(name):  # stands in for a sync store method
        captured["thread"] = threading.get_ident()
        captured["loop"] = _running_loop_or_none()
        return "ok"

    backend.read = probe  # facade will off-load this sync method
    result = await store.read("x")

    assert result == "ok"
    assert captured["thread"] != loop_thread, "sync body ran ON the event-loop thread"
    assert captured["loop"] is None, "a running loop exists on the executor thread"


@pytest.mark.asyncio
async def test_concurrent_writes_all_persist(store, backend):
    n = 60
    await asyncio.gather(
        *[store.write(name=f"m{i}", title="t", body="b", tier="working") for i in range(n)]
    )
    names = _names(backend.db_path)
    assert {f"m{i}" for i in range(n)} <= names


# ── run_in_txn: whole transaction, one unit, on the executor ──────────────


@pytest.mark.asyncio
async def test_run_in_txn_commits_and_runs_off_loop(store, backend):
    loop_thread = threading.get_ident()
    seen = {}

    def work(conn):
        seen["thread"] = threading.get_ident()
        seen["conn_loop"] = _running_loop_or_none()
        backend._mem.write(name="t1", title="t", body="b", _conn=conn)
        backend._mem.write(name="t2", title="t", body="b", _conn=conn)

    await store.run_in_txn(work)
    assert {"t1", "t2"} <= _names(backend.db_path)
    assert seen["thread"] != loop_thread, "transaction ran on the event-loop thread"
    assert seen["conn_loop"] is None


@pytest.mark.asyncio
async def test_run_in_txn_rolls_back_atomically(store, backend):
    def work(conn):
        backend._mem.write(name="ghost", title="t", body="b", _conn=conn)
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await store.run_in_txn(work)
    assert "ghost" not in _names(backend.db_path)


@pytest.mark.asyncio
async def test_run_in_txn_single_thread_even_with_more_workers(tmp_path):
    # Anti-masking: even with a multi-worker executor, run_in_txn keeps the WHOLE txn on
    # ONE thread (one submit) — conn is created and used on the same worker, so no
    # sqlite3 cross-thread ProgrammingError. max_workers=1 alone could hide a split.
    from concurrent.futures import ThreadPoolExecutor

    b = SQLiteStore(tmp_path / "m.db")
    b.bootstrap()
    s = AsyncStore(b)
    s._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="mori-db-test")
    try:
        threads = []

        def work(conn):
            threads.append(threading.get_ident())
            b._mem.write(name="a", title="t", body="b", _conn=conn)
            threads.append(threading.get_ident())  # same thread as the open

        await s.run_in_txn(work)
        assert threads[0] == threads[1], "transaction crossed threads"
        assert "a" in _names(b.db_path)
    finally:
        s.aclose()


# ── the off-loop guard ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_assert_off_loop_fires_on_the_loop(store):
    # Called directly from a coroutine (on the loop) → must raise.
    with pytest.raises(RuntimeError, match="event loop"):
        _assert_off_loop()


def test_assert_off_loop_passes_off_loop():
    # No running loop on this (sync) thread → must NOT raise.
    _assert_off_loop()


# ── Postgres parity: await directly, NEVER off-load ────────────────────────


class _FakePG:
    """Minimal async backend — begin_transaction is a coroutine fn so AsyncStore treats
    it as the async backend; write asserts it runs on the loop."""

    def __init__(self):
        self.test_loop = None

    async def begin_transaction(self):  # only its async-ness matters here
        yield None

    async def write(self, **kw):
        assert _running_loop_or_none() is self.test_loop, "PG method ran off the event loop"
        return "pg-ok"


@pytest.mark.asyncio
async def test_postgres_methods_awaited_not_offloaded():
    pg = _FakePG()
    pg.test_loop = asyncio.get_running_loop()
    s = AsyncStore(pg)
    result = await s.write(name="x")
    assert result == "pg-ok"
    # the off-load path was never taken → the executor was never even created
    assert s._executor is None, "Postgres write was wrongly off-loaded to the DB executor"


@pytest.mark.asyncio
async def test_raw_attrs_returned_unwrapped(store, backend):
    # begin_transaction / _mem must come back raw (not async-wrapped), for run_in_txn
    # and the sync surface respectively.
    assert store.begin_transaction.__self__ is backend
    assert store._mem is backend._mem
