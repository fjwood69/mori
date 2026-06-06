"""PERF-003 — check_freshness concurrency, cache, and batching tests.

No real LLM calls are made — llm_consult is always mocked.

Verifies:
1. Concurrency cap: at most 5 LLM calls run in parallel (ThreadPoolExecutor
   workers=5).
2. Cache: a second call within 24h does NOT trigger a second LLM call for the
   same memory.
3. Batching: all status UPDATEs are applied in a single connection/transaction
   (verified by inspecting that the results tally correctly after one call).
4. Verdicts unchanged: the normalisation logic (FRESH/STALE/NO → lowercase)
   matches the old sequential behaviour.

All tests run against SQLite.  Postgres tests are skipped unless
MORI_TEST_DATABASE_URL is set.

Async tests use asyncio.run() to match repo style (no asyncio_mode in
pyproject.toml).
"""

from __future__ import annotations

import asyncio
import os
import threading
import time

import pytest

PG_URL = os.environ.get("MORI_TEST_DATABASE_URL", "")
requires_pg = pytest.mark.skipif(not PG_URL, reason="MORI_TEST_DATABASE_URL not set")


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_freshness_cache():
    """Reset the global freshness cache before each test."""
    import mori_advisor.memory_store as ms

    ms._freshness_cache.clear()
    yield
    ms._freshness_cache.clear()


@pytest.fixture()
def mem_store(tmp_path):
    """Return a bootstrapped MemoryStore via SQLiteStore (uses migration runner)."""
    from mori_advisor.store.sqlite_store import SQLiteStore

    db = tmp_path / "mem.db"
    store = SQLiteStore(db)
    store.bootstrap()
    return store._mem  # the composed MemoryStore


def _insert_canonical_infra(store, name: str, title: str = "A canonical memory") -> None:
    """Insert a canonical memory with the 'infrastructure' tag."""
    store.write(
        name=name,
        title=title,
        type="project",
        tier="canonical",
        body="Some infrastructure detail.",
        tags=["infrastructure"],
        _skip_protection=True,
    )


# ── Verdicts normalisation ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "llm_response,expected_status",
    [
        ("FRESH", "fresh"),
        ("fresh", "fresh"),
        ("STALE", "stale"),
        ("NO", "no"),
        ("garbage", "fresh"),  # unrecognised → fresh (safe default)
        ("", "fresh"),
    ],
)
def test_verdict_normalisation(mem_store, llm_response, expected_status, tmp_path):
    """check_freshness normalises LLM responses correctly."""
    _insert_canonical_infra(mem_store, f"mem-norm-{expected_status}-{llm_response[:4]}")

    call_count = 0

    def _fake_llm(**kwargs):
        nonlocal call_count
        call_count += 1
        return llm_response

    results = mem_store.check_freshness(_fake_llm, limit=1)
    assert results["checked"] == 1
    assert results[expected_status] == 1
    assert call_count == 1


# ── Concurrency cap ───────────────────────────────────────────────────────


def test_concurrency_cap_max_5_in_flight(mem_store):
    """At most 5 LLM calls may run simultaneously."""
    n = 12  # more than the cap
    for i in range(n):
        _insert_canonical_infra(mem_store, f"infra-conc-{i:02d}", title=f"Memory {i}")

    max_concurrent = 0
    lock = threading.Lock()
    current = 0

    def _slow_llm(**kwargs):
        nonlocal max_concurrent, current
        with lock:
            current += 1
            if current > max_concurrent:
                max_concurrent = current
        time.sleep(0.01)  # enough to overlap
        with lock:
            current -= 1
        return "FRESH"

    mem_store.check_freshness(_slow_llm, limit=n)
    assert max_concurrent <= 5, (
        f"Expected at most 5 concurrent LLM calls, observed {max_concurrent}"
    )


def test_all_n_memories_checked(mem_store):
    """All N memories within the limit are eventually checked."""
    n = 8
    for i in range(n):
        _insert_canonical_infra(mem_store, f"infra-all-{i:02d}")

    call_count = 0

    def _count_llm(**kwargs):
        nonlocal call_count
        call_count += 1
        return "FRESH"

    results = mem_store.check_freshness(_count_llm, limit=n)
    assert call_count == n
    assert results["checked"] == n
    assert results["fresh"] == n


# ── 24h cache ─────────────────────────────────────────────────────────────


def test_cache_prevents_second_llm_call_within_ttl(mem_store):
    """A second check_freshness within 24h must NOT call llm_consult again."""
    _insert_canonical_infra(mem_store, "infra-cache-hit")

    call_count = 0

    def _counting_llm(**kwargs):
        nonlocal call_count
        call_count += 1
        return "FRESH"

    # First call — LLM should be invoked once.
    results1 = mem_store.check_freshness(_counting_llm, limit=1)
    assert results1["checked"] == 1
    assert call_count == 1

    # Second call — must hit cache, no further LLM call.
    results2 = mem_store.check_freshness(_counting_llm, limit=1)
    assert results2["checked"] == 1
    assert call_count == 1, (
        f"LLM was called again on second check_freshness — cache not working. "
        f"call_count={call_count}"
    )


def test_cache_expires_after_ttl(mem_store):
    """After the TTL expires, the LLM must be called again."""
    import mori_advisor.memory_store as ms

    _insert_canonical_infra(mem_store, "infra-cache-expire")

    call_count = 0

    def _counting_llm(**kwargs):
        nonlocal call_count
        call_count += 1
        return "FRESH"

    # First call.
    mem_store.check_freshness(_counting_llm, limit=1)
    assert call_count == 1

    # Backdating the cache entry to simulate TTL expiry.
    expired_at = time.monotonic() - ms._FRESHNESS_CACHE_TTL - 1
    ms._freshness_cache["infra-cache-expire"] = ("fresh", expired_at)

    # Second call — cache is expired, LLM must be called again.
    mem_store.check_freshness(_counting_llm, limit=1)
    assert call_count == 2, f"Expected 2 LLM calls after cache expiry but got {call_count}"


def test_cache_hit_still_counted_in_results(mem_store):
    """Cache hits are still reflected in the results dict.

    The cache is consulted for memories whose freshness_status is 'unknown'
    or 'fresh'.  After a first call that returns FRESH, the DB status is
    'fresh' and the cache entry is set.  A second call within the TTL must
    return the cached verdict without calling the LLM, but still count it.
    """
    _insert_canonical_infra(mem_store, "infra-cache-counted")

    def _llm(**kwargs):
        return "FRESH"

    # First call — LLM returns FRESH; DB updated to 'fresh'; cache populated.
    r1 = mem_store.check_freshness(_llm, limit=1)
    assert r1["checked"] == 1
    assert r1["fresh"] == 1

    def _llm_noop(**kwargs):
        raise AssertionError("LLM should not be called on cache hit")

    # Second call — status in DB is 'fresh' (still in candidate set);
    # cache hit suppresses the LLM call but results["checked"] must still be 1.
    results = mem_store.check_freshness(_llm_noop, limit=1)
    assert results["checked"] == 1
    assert results["fresh"] == 1


# ── Batched UPDATE ────────────────────────────────────────────────────────


def test_results_tally_correct_after_batch(mem_store):
    """After a batch of mixed verdicts, result tallies match DB state."""
    names = ["infra-batch-a", "infra-batch-b", "infra-batch-c"]
    for n in names:
        _insert_canonical_infra(mem_store, n)

    responses = {"infra-batch-a": "FRESH", "infra-batch-b": "STALE", "infra-batch-c": "NO"}

    def _verdict_llm(**kwargs):
        return responses[kwargs["user"]]

    results = mem_store.check_freshness(_verdict_llm, limit=3)
    assert results["checked"] == 3
    assert results["fresh"] == 1
    assert results["stale"] == 1
    assert results["no"] == 1

    # Verify DB was actually updated.
    # MemoryStore._get_conn() does NOT set row_factory — rows are plain tuples.
    # Column order in the SELECT: name=0, freshness_status=1.
    conn = mem_store._get_conn()
    try:
        rows = conn.execute(
            "SELECT name, freshness_status FROM memories WHERE name LIKE 'infra-batch-%'"
        ).fetchall()
    finally:
        conn.close()

    db_statuses = {r[0]: r[1] for r in rows}
    assert db_statuses["infra-batch-a"] == "fresh"
    assert db_statuses["infra-batch-b"] == "stale"
    assert db_statuses["infra-batch-c"] == "no"


# ── Thundering-herd / in-flight sentinel ─────────────────────────────────


def test_no_duplicate_llm_calls_concurrent_threads(mem_store):
    """Two threads racing on the same memory fire at most 1 LLM call.

    This exercises the in-flight sentinel logic: the first thread marks the
    memory as in-flight and calls the LLM; the second thread sees the sentinel
    and skips its call entirely.

    We cannot guarantee which thread 'wins' the sentinel race without
    cooperation, so the test inserts two memories and races 6 threads across
    them — the total LLM call count must be <= 2 (one per memory).
    """
    import mori_advisor.memory_store as ms

    names = ["infra-therd-a", "infra-therd-b"]
    for n in names:
        _insert_canonical_infra(mem_store, n)

    call_log: list[str] = []
    barrier = threading.Barrier(6)  # synchronise 6 threads to maximise overlap

    def _slow_llm(**kwargs):
        call_log.append(kwargs.get("user", "?"))
        time.sleep(0.03)  # hold long enough for others to hit the in-flight sentinel
        return "FRESH"

    def _run():
        barrier.wait()  # all threads start concurrently
        mem_store.check_freshness(_slow_llm, limit=2)

    threads = [threading.Thread(target=_run) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # At most 2 LLM calls (one per memory); sentinel prevents extras.
    assert len(call_log) <= 2, (
        f"Thundering-herd not prevented: {len(call_log)} LLM calls for 2 memories "
        f"(call_log={call_log})"
    )

    # Clean up cache for subsequent tests.
    with ms._freshness_cache_lock:
        for n in names:
            ms._freshness_cache.pop(n, None)


def test_in_flight_sentinel_cleared_on_llm_error(mem_store):
    """When the LLM call errors, the in-flight sentinel is cleared so the next
    call retries (rather than being permanently stuck in-flight)."""
    import mori_advisor.memory_store as ms

    _insert_canonical_infra(mem_store, "infra-sentinel-err")

    call_count = 0

    def _failing_llm(**kwargs):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("simulated LLM failure")

    # First call — LLM fails; sentinel should be cleared.
    results1 = mem_store.check_freshness(_failing_llm, limit=1)
    assert results1["errors"] == 1

    def _ok_llm(**kwargs):
        nonlocal call_count
        call_count += 1
        return "FRESH"

    # Second call — sentinel was cleared, so LLM is called again.
    results2 = mem_store.check_freshness(_ok_llm, limit=1)
    assert results2["checked"] == 1
    assert results2["fresh"] == 1

    # Clean up.
    with ms._freshness_cache_lock:
        ms._freshness_cache.pop("infra-sentinel-err", None)


# ── Error handling ────────────────────────────────────────────────────────


def test_llm_error_counted_in_errors(mem_store):
    """LLM exception increments errors, doesn't break the whole check."""
    _insert_canonical_infra(mem_store, "infra-err-1")
    _insert_canonical_infra(mem_store, "infra-err-2")

    call_count = 0

    def _flaky_llm(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("LLM transient failure")
        return "FRESH"

    results = mem_store.check_freshness(_flaky_llm, limit=2)
    assert results["errors"] == 1
    assert results["checked"] == 1  # only the successful one


# ── Postgres tests ────────────────────────────────────────────────────────


@requires_pg
def test_pg_check_freshness_concurrency(tmp_path):
    """PostgresStore.check_freshness runs LLM calls concurrently, capped at 5."""
    from mori_advisor.store.postgres_store import PostgresStore

    async def _run():
        store = PostgresStore(PG_URL)
        await store.bootstrap()
        n = 8
        try:
            for i in range(n):
                await store.write(
                    name=f"pg-fresh-{i:02d}",
                    title=f"PG memory {i}",
                    type="project",
                    tier="canonical",
                    body="detail",
                    tags=["infrastructure"],
                    _skip_protection=True,
                )

            max_concurrent = 0
            lock = threading.Lock()
            current = 0

            def _slow_llm(**kwargs):
                nonlocal max_concurrent, current
                with lock:
                    current += 1
                    if current > max_concurrent:
                        max_concurrent = current
                time.sleep(0.02)
                with lock:
                    current -= 1
                return "FRESH"

            results = await store.check_freshness(_slow_llm, limit=n)
            assert results["checked"] == n
            assert max_concurrent <= 5, (
                f"PG: expected ≤5 concurrent LLM calls, observed {max_concurrent}"
            )
        finally:
            async with store.pool.acquire() as conn:
                await conn.execute("DELETE FROM memories WHERE name LIKE 'pg-fresh-%'")

    asyncio.run(_run())


@requires_pg
def test_pg_cache_prevents_second_llm_call(tmp_path):
    """PostgresStore shares the global cache — second call is a cache hit."""
    import mori_advisor.memory_store as ms

    ms._freshness_cache.clear()

    from mori_advisor.store.postgres_store import PostgresStore

    async def _run():
        store = PostgresStore(PG_URL)
        await store.bootstrap()
        try:
            await store.write(
                name="pg-cache-test",
                title="PG cache test memory",
                type="project",
                tier="canonical",
                body="detail",
                tags=["infrastructure"],
                _skip_protection=True,
            )

            call_count = 0

            def _counting_llm(**kwargs):
                nonlocal call_count
                call_count += 1
                return "FRESH"

            await store.check_freshness(_counting_llm, limit=1)
            assert call_count == 1

            await store.check_freshness(_counting_llm, limit=1)
            assert call_count == 1, f"PG: LLM called again on cache hit — call_count={call_count}"
        finally:
            async with store.pool.acquire() as conn:
                await conn.execute("DELETE FROM memories WHERE name = 'pg-cache-test'")
            ms._freshness_cache.clear()

    asyncio.run(_run())
