"""Postgres integration tests for Stream B1/B2 — assessor + canon writer.

Gated on BOTH ``MORI_INTAKE_TEST_DATABASE_URL`` (intake Postgres) and
``MORI_CANON_TEST_DATABASE_URL`` (mori canon Postgres).  When either
variable is unset all integration tests in this module are skipped with a
clear message.

**Design decision (non-negotiable):** the agent-intake promotion feature is
Postgres-only.  The canon side of these tests targets a real ``PostgresStore``
so we exercise the exact production path — ``search_json``, ``get_memory``,
``canon_reader()``, ``record_intake_lineage()`` — all of which are async
Postgres methods with no SQLite equivalent.

Setup
-----
* Each test gets function-scoped pools for both intake Postgres and canon
  Postgres.
* Intake migrations are applied via ``mori_intake.migrations.apply``.
* Canon migrations are applied via
  ``mori_advisor.store.migrations.apply_postgres`` targeting the ``memories``
  and ``memory_intake_lineage`` tables (migration ids 1 + 10).
* Tables are truncated for hermetic test isolation.
* Workers (``assess_once`` / ``drain_once``) are driven directly — no real
  sleeps.
* The fast-model assessor is STUBBED — deterministic, no network.

Pure-logic / unit tests live in ``test_intake_assessor_unit.py`` and always
run without a database.
"""

from __future__ import annotations

import os
import uuid

import pytest

# ── Module-level skip when either DSN is absent ────────────────────────────────

_INTAKE_DSN = os.environ.get("MORI_INTAKE_TEST_DATABASE_URL", "")
_CANON_DSN = os.environ.get("MORI_CANON_TEST_DATABASE_URL", "")

if not _INTAKE_DSN or not _CANON_DSN:
    _missing = []
    if not _INTAKE_DSN:
        _missing.append("MORI_INTAKE_TEST_DATABASE_URL")
    if not _CANON_DSN:
        _missing.append("MORI_CANON_TEST_DATABASE_URL")
    pytest.skip(
        f"Skipping Postgres/canon integration tests — "
        f"{', '.join(_missing)} not set.  "
        "Set both variables to asyncpg DSNs pointing at throwaway Postgres "
        "instances to run these tests.",
        allow_module_level=True,
    )

# ── Imports (only reached when both DSNs are set) ─────────────────────────────

import pytest_asyncio  # noqa: E402

from mori_intake import migrations, worker  # noqa: E402
from mori_intake.assessor import AssessmentResult, assess_once  # noqa: E402
from mori_intake.canon_writer import drain_once  # noqa: E402
from mori_intake.normalize import content_hash  # noqa: E402

os.environ["MORI_INTAKE_DATABASE_URL"] = _INTAKE_DSN
os.environ.pop("MORI_DATABASE_URL", None)

import mori_intake.db as intake_db  # noqa: E402

# ── Canon store factory ───────────────────────────────────────────────────────


async def _make_pg_mori_store(dsn: str):
    """Return a bootstrapped ``PostgresStore`` against *dsn*.

    Applies the mori Postgres migrations (baseline + ``memory_intake_lineage``)
    so the ``memories`` and ``memory_intake_lineage`` tables exist.

    The caller is responsible for closing the store pool when done:
    ``await store.pool.close()``.
    """
    from mori_advisor.store.migrations import MIGRATIONS, apply_postgres
    from mori_advisor.store.postgres_store import PostgresStore

    store = PostgresStore(dsn)
    # apply_postgres connects (creates pool) then applies all pending migrations.
    await apply_postgres(store, MIGRATIONS)
    return store


async def _truncate_canon(store) -> None:
    """Truncate mori canon tables for test isolation."""
    async with store.pool.acquire() as conn:
        await conn.execute("TRUNCATE memory_intake_lineage, memories RESTART IDENTITY CASCADE")


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def intake_pool():
    """Function-scoped intake pool with migrations applied and tables truncated.

    Isolation note: ``mori_intake.db._pool`` is a module-level global.  When
    the full suite runs, another module's fixture (e.g. ``test_intake_pg``) may
    have left the global pointing at a closed or stale pool.  Resetting
    ``intake_db._pool = None`` before ``create_pool()`` ensures a fresh pool is
    always created and rebound to the global, so ``db.get_pool()`` calls within
    app handlers work correctly during this test.
    """
    intake_db._pool = None  # force fresh pool; deterministically rebind the global
    pool = await intake_db.create_pool()
    await migrations.apply(pool)
    await pool.execute(
        "TRUNCATE intake_corroborations, promotion_queue, intake_promotion_map, "
        "intake_candidates, intake_submissions RESTART IDENTITY CASCADE"
    )
    try:
        yield pool
    finally:
        await intake_db.close_pool()


@pytest_asyncio.fixture
async def canon_store():
    """Function-scoped Postgres canon store with migrations applied and tables truncated."""
    store = await _make_pg_mori_store(_CANON_DSN)
    await _truncate_canon(store)
    try:
        yield store
    finally:
        await store.pool.close()


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _seed_pending_candidate(pool, *, content: str) -> tuple[str, str]:
    """Insert a submission + drain the worker → return (submission_id, candidate_id)."""
    session_id = f"test-session-{uuid.uuid4()}"
    stable_key = f"learned-b1-{uuid.uuid4()}"
    submission_id = await pool.fetchval(
        "INSERT INTO intake_submissions "
        "  (id, session_id, agent_id, target_name, action, stable_key, raw_source_text) "
        "VALUES ($1, $2, 'hermes', 'memory', 'add', $3, $4) RETURNING id",
        uuid.uuid4(),
        session_id,
        stable_key,
        content,
    )
    await worker.drain_once(pool)
    corr = await pool.fetchrow(
        "SELECT candidate_id FROM intake_corroborations WHERE submission_id = $1",
        submission_id,
    )
    assert corr is not None, "Worker did not create a corroboration row"
    return str(submission_id), str(corr["candidate_id"])


# ── Assessor tests (no canon interaction) ────────────────────────────────────


@pytest.mark.asyncio
async def test_assessor_unrelated_transitions_to_under_review(intake_pool):
    """UNRELATED → candidate under_review + promotion_queue row queued."""
    _, cid = await _seed_pending_candidate(
        intake_pool,
        content="Distributed tracing improves observability in microservice architectures.",
    )

    def _unrelated_stub(body, h):
        return AssessmentResult(verdict="UNRELATED")

    processed = await assess_once(intake_pool, assess=_unrelated_stub)
    assert processed >= 1

    candidate = await intake_pool.fetchrow(
        "SELECT status FROM intake_candidates WHERE id = $1::uuid", cid
    )
    assert candidate["status"] == "under_review"

    queued = await intake_pool.fetchrow(
        "SELECT status FROM promotion_queue WHERE candidate_id = $1::uuid", cid
    )
    assert queued is not None
    assert queued["status"] == "queued"


@pytest.mark.asyncio
async def test_assessor_supersedes_transitions_to_rejected(intake_pool):
    """SUPERSEDES → candidate rejected with correct rejection_reason."""
    _, cid = await _seed_pending_candidate(
        intake_pool,
        content="Immutable infrastructure reduces configuration drift over time.",
    )

    def supersedes_stub(body, h):
        return AssessmentResult(
            verdict="SUPERSEDES", matched_canon_name="known-canon-memory", score=0.97
        )

    processed = await assess_once(intake_pool, assess=supersedes_stub)
    assert processed >= 1

    candidate = await intake_pool.fetchrow(
        "SELECT status, rejection_reason FROM intake_candidates WHERE id = $1::uuid", cid
    )
    assert candidate["status"] == "rejected"
    assert candidate["rejection_reason"] == "duplicate-of-canon:known-canon-memory"


@pytest.mark.asyncio
async def test_assessor_related_transitions_to_rejected(intake_pool):
    """RELATED → candidate rejected."""
    _, cid = await _seed_pending_candidate(
        intake_pool,
        content="Blue-green deployments eliminate downtime during releases.",
    )

    def related_stub(body, h):
        return AssessmentResult(verdict="RELATED", matched_canon_name="canon-bg", score=0.80)

    await assess_once(intake_pool, assess=related_stub)

    candidate = await intake_pool.fetchrow(
        "SELECT status, rejection_reason FROM intake_candidates WHERE id = $1::uuid", cid
    )
    assert candidate["status"] == "rejected"
    assert "duplicate-of-canon:canon-bg" in candidate["rejection_reason"]


# ── Full loop with Postgres canon ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_canon_writer_full_promotion_path_pg(intake_pool, canon_store):
    """End-to-end with Postgres canon: pending → under_review → promoted.

    Verifies:
    * canon memory row written to Postgres memories table.
    * memory_intake_lineage row written with correct candidate id.
    * intake_promotion_map row present.
    * promotion_queue row committed.
    * canon_reader() async callables work: search + fetch_body.
    """
    content = "Circuit breakers prevent cascade failures in distributed systems."

    _, cid = await _seed_pending_candidate(intake_pool, content=content)

    # Assess with explicit UNRELATED stub → under_review + queued.
    # (Default stub now returns NEEDS_REVIEW — must inject UNRELATED explicitly.)
    def _unrelated_stub(body, h):
        return AssessmentResult(verdict="UNRELATED")

    await assess_once(intake_pool, assess=_unrelated_stub)

    # Drain promotion queue → Postgres canon write.
    committed = await drain_once(intake_pool, canon_store)
    assert committed >= 1

    # Candidate should be promoted.
    candidate = await intake_pool.fetchrow(
        "SELECT status, promoted_canon_name FROM intake_candidates WHERE id = $1::uuid", cid
    )
    assert candidate["status"] == "promoted"
    assert candidate["promoted_canon_name"] is not None
    canon_name = candidate["promoted_canon_name"]

    # intake_promotion_map row should exist.
    promo_map = await intake_pool.fetchrow(
        "SELECT canon_name FROM intake_promotion_map WHERE candidate_id = $1::uuid", cid
    )
    assert promo_map is not None
    assert promo_map["canon_name"] == canon_name

    # Queue row should be committed.
    queue_row = await intake_pool.fetchrow(
        "SELECT status FROM promotion_queue WHERE candidate_id = $1::uuid", cid
    )
    assert queue_row is not None
    assert queue_row["status"] == "committed"

    # canon memory should exist in Postgres.
    mem = await canon_store.get_memory(canon_name)
    assert mem is not None, f"Canon memory {canon_name!r} not found in Postgres canon"
    assert mem["body"] == content or content in mem["body"]

    # memory_intake_lineage row should exist in Postgres.
    lineage = await canon_store.get_intake_lineage(canon_name)
    assert lineage is not None, "memory_intake_lineage row missing from Postgres canon"
    assert lineage["intake_candidate_id"] == cid

    # canon_reader() async callables must work on the Postgres canon store.
    reader = canon_store.canon_reader()
    # Use the first four whole words so websearch_to_tsquery('english', …) can
    # satisfy all AND-ed terms.  content[:50] risks truncating mid-token (e.g.
    # "distributed" → "distr") which breaks the FTS AND query → empty result.
    fts_query = " ".join(content.split()[:4])
    search_results = await reader.search(fts_query, 5)
    assert isinstance(search_results, list)
    # The newly promoted memory should appear in search results.
    names = [r.get("name") for r in search_results]
    assert canon_name in names, (
        f"Promoted memory {canon_name!r} not found in canon_reader search results: {names}"
    )

    fetched_body = await reader.fetch_body(canon_name)
    assert fetched_body, "fetch_body returned empty for a promoted canon memory"


@pytest.mark.asyncio
async def test_canon_writer_idempotent_redrive_pg(intake_pool, canon_store):
    """Re-driving the promotion queue after commit is a no-op — no duplicate canon row."""
    content = "Eventual consistency is a well-understood trade-off in distributed databases."

    def _unrelated_stub(body, h):
        return AssessmentResult(verdict="UNRELATED")

    _, cid = await _seed_pending_candidate(intake_pool, content=content)
    await assess_once(intake_pool, assess=_unrelated_stub)
    await drain_once(intake_pool, canon_store)

    # Candidate promoted on first drain.
    candidate = await intake_pool.fetchrow(
        "SELECT status FROM intake_candidates WHERE id = $1::uuid", cid
    )
    assert candidate["status"] == "promoted"

    # Count canon rows before re-drive.
    async with canon_store.pool.acquire() as conn:
        count_before = await conn.fetchval("SELECT COUNT(*) FROM memories")
        lin_before = await conn.fetchval("SELECT COUNT(*) FROM memory_intake_lineage")

    # Re-drive: queue row is committed → WHERE clause excludes it → 0 committed.
    second_committed = await drain_once(intake_pool, canon_store)
    assert second_committed == 0

    # Canon and lineage counts must not increase.
    async with canon_store.pool.acquire() as conn:
        count_after = await conn.fetchval("SELECT COUNT(*) FROM memories")
        lin_after = await conn.fetchval("SELECT COUNT(*) FROM memory_intake_lineage")

    assert count_after == count_before, "Duplicate canon memory created on re-drive"
    assert lin_after == lin_before, "Duplicate lineage row created on re-drive"


@pytest.mark.asyncio
async def test_idempotency_guard_via_promotion_map_pg(intake_pool, canon_store):
    """Simulate crash-after-canon-write: promotion_map present but queue not committed.

    The canon writer must detect the existing map row, skip the mori write,
    and still mark the queue row committed.
    """
    content = "Write-ahead logging ensures durability in database crash recovery."

    def _unrelated_stub(body, h):
        return AssessmentResult(verdict="UNRELATED")

    _, cid = await _seed_pending_candidate(intake_pool, content=content)
    await assess_once(intake_pool, assess=_unrelated_stub)

    # Manually insert promotion_map row (simulating prior partial run).
    cid_uuid = uuid.UUID(cid)
    existing_canon_name = f"agent-intake-{content_hash(content)[:16]}"

    # Write canon memory + lineage directly to simulate the prior partial completion.
    await canon_store.write(
        name=existing_canon_name,
        title="Pre-existing canon (crash recovery test)",
        body=content,
        type="feedback",
        tier="working",
    )
    from datetime import datetime, timezone

    await canon_store.record_intake_lineage(
        canon_name=existing_canon_name,
        intake_candidate_id=cid,
        intake_submission_ids=[],
        trust_snapshot={},
        promoted_at=datetime.now(timezone.utc),
    )

    await intake_pool.execute(
        "INSERT INTO intake_promotion_map (canon_name, candidate_id, submission_ids) "
        "VALUES ($1, $2, $3)",
        existing_canon_name,
        cid_uuid,
        [],
    )

    # Canon writer must detect existing promotion_map and mark queue committed.
    committed = await drain_once(intake_pool, canon_store)
    assert committed >= 1

    queue_row = await intake_pool.fetchrow(
        "SELECT status FROM promotion_queue WHERE candidate_id = $1::uuid", cid_uuid
    )
    assert queue_row is not None
    assert queue_row["status"] == "committed"

    # Exactly one canon memory row must exist — no duplicate.
    async with canon_store.pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM memories WHERE name = $1", existing_canon_name
        )
    assert count == 1, f"Expected 1 canon row, got {count}"


# ── Unit tests: canon_reader + B3 flag behaviour (no DB required) ────────────
#
# These run unconditionally (not skipped by the module-level DSN check above
# because the module is only reached when BOTH DSNs are set — but the tests
# themselves use only mocks and no real database).


class TestCanonReaderBackends:
    """canon_reader() PG returns async callables; SQLite raises NotImplementedError."""

    def test_sqlite_store_canon_reader_raises(self, tmp_path):
        """SQLiteStore.canon_reader() must raise NotImplementedError immediately."""
        from mori_advisor.store.sqlite_store import SQLiteStore

        db_path = tmp_path / "memories.db"
        store = SQLiteStore(db_path)
        with pytest.raises(NotImplementedError, match="Postgres canon store"):
            store.canon_reader()

    def test_postgres_store_canon_reader_returns_canon_reader_object(self):
        """PostgresStore.canon_reader() returns a CanonReader with async callables."""
        import inspect
        from unittest.mock import AsyncMock, MagicMock, patch

        from mori_advisor.store.postgres_store import PostgresStore
        from mori_intake.assess_model import CanonReader

        store = PostgresStore("postgresql://fake/db")
        # Patch pool so _ensure_pool doesn't raise.
        store.pool = MagicMock()

        with (
            patch.object(store, "search_json", new=AsyncMock(return_value=[])),
            patch.object(store, "get_memory", new=AsyncMock(return_value=None)),
        ):
            reader = store.canon_reader()

        assert isinstance(reader, CanonReader)
        # Both callables must be coroutine functions (async).
        assert inspect.iscoroutinefunction(reader.search), (
            "PostgresStore.canon_reader().search must be a coroutine function"
        )
        assert inspect.iscoroutinefunction(reader.fetch_body), (
            "PostgresStore.canon_reader().fetch_body must be a coroutine function"
        )

    def test_postgres_canon_reader_search_delegates_to_search_json(self):
        """reader.search() awaits search_json with the correct arguments."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        from mori_advisor.store.postgres_store import PostgresStore

        store = PostgresStore("postgresql://fake/db")
        store.pool = MagicMock()

        mock_results = [{"name": "n1", "tier": "canonical"}]
        mock_search_json = AsyncMock(return_value=mock_results)
        mock_get_memory = AsyncMock(return_value=None)

        with (
            patch.object(store, "search_json", mock_search_json),
            patch.object(store, "get_memory", mock_get_memory),
        ):
            reader = store.canon_reader()
            results = asyncio.run(reader.search("test query", 3))

        mock_search_json.assert_awaited_once_with(query="test query", limit=3)
        assert results == mock_results

    def test_postgres_canon_reader_fetch_body_returns_body(self):
        """reader.fetch_body() returns the body field from get_memory."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        from mori_advisor.store.postgres_store import PostgresStore

        store = PostgresStore("postgresql://fake/db")
        store.pool = MagicMock()

        mock_mem = {"name": "test-mem", "body": "The full body text.", "tier": "canonical"}
        mock_get_memory = AsyncMock(return_value=mock_mem)

        with (
            patch.object(store, "search_json", AsyncMock(return_value=[])),
            patch.object(store, "get_memory", mock_get_memory),
        ):
            reader = store.canon_reader()
            body = asyncio.run(reader.fetch_body("test-mem"))

        mock_get_memory.assert_awaited_once_with("test-mem")
        assert body == "The full body text."

    def test_postgres_canon_reader_fetch_body_missing_memory_returns_empty(self):
        """reader.fetch_body() returns '' when get_memory returns None."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        from mori_advisor.store.postgres_store import PostgresStore

        store = PostgresStore("postgresql://fake/db")
        store.pool = MagicMock()

        with (
            patch.object(store, "search_json", AsyncMock(return_value=[])),
            patch.object(store, "get_memory", AsyncMock(return_value=None)),
        ):
            reader = store.canon_reader()
            body = asyncio.run(reader.fetch_body("missing-mem"))

        assert body == ""


class TestB3FlagBehaviour:
    """B3 flag-off is a no-op; flag-on with non-PG store is a no-op."""

    def _make_pipeline(self, tmp_path):
        """Return a DreamPipeline with a SQLiteStore (flag-off tests)."""
        from unittest.mock import MagicMock

        from mori_advisor.bifrost_client import BifrostClient
        from mori_advisor.dream import DreamPipeline

        client = MagicMock(spec=BifrostClient)
        return DreamPipeline(
            db_path=tmp_path / "memories.db",
            bifrost_client=client,
        )

    def test_flag_off_no_intake_import(self, tmp_path, monkeypatch):
        """When flag is off, _run_intake_promotion returns without importing mori_intake.db."""
        import asyncio
        import sys

        monkeypatch.delenv("MORI_INTAKE_PROMOTION_ENABLED", raising=False)
        pipeline = self._make_pipeline(tmp_path)

        # Remove mori_intake.db from sys.modules so we can detect any import attempt.
        sys.modules.pop("mori_intake.db", None)
        asyncio.run(pipeline._run_intake_promotion())

        # mori_intake.db must not have been imported as a side-effect.
        assert "mori_intake.db" not in sys.modules, (
            "mori_intake.db must NOT be imported when the promotion flag is off"
        )

    def test_flag_off_run_succeeds_no_events(self, tmp_path, monkeypatch):
        """run() with flag off and no events returns [] without touching intake."""
        import asyncio
        from unittest.mock import MagicMock

        from mori_advisor.bifrost_client import BifrostClient
        from mori_advisor.dream import DreamPipeline
        from mori_advisor.store.sqlite_store import SQLiteStore

        monkeypatch.delenv("MORI_INTAKE_PROMOTION_ENABLED", raising=False)

        db_path = tmp_path / "memories.db"
        store = SQLiteStore(db_path)
        store.bootstrap()

        client = MagicMock(spec=BifrostClient)
        pipeline = DreamPipeline(db_path=db_path, bifrost_client=client, store=store)

        result = asyncio.run(pipeline.run())
        assert result == []
        # Model was never called (no events).
        client.consult.assert_not_called()

    def test_flag_on_non_pg_store_is_noop(self, tmp_path, monkeypatch):
        """flag=on but SQLiteStore → no-op (no intake connection attempt)."""
        import asyncio
        import sys

        monkeypatch.setenv("MORI_INTAKE_PROMOTION_ENABLED", "true")
        monkeypatch.setenv("MORI_INTAKE_DATABASE_URL", "postgresql://fake/intake")

        # Remove asyncpg / mori_intake.db so any connection attempt raises immediately.
        sys.modules.pop("mori_intake.db", None)

        pipeline = self._make_pipeline(tmp_path)
        # SQLiteStore: hasattr(store, 'pool') is False.
        assert not hasattr(pipeline.store, "pool")

        # Must not raise — the no-pool guard fires first.
        asyncio.run(pipeline._run_intake_promotion())

    def test_flag_on_no_dsn_is_noop(self, tmp_path, monkeypatch):
        """flag=on but MORI_INTAKE_DATABASE_URL unset → no-op."""
        import asyncio

        monkeypatch.setenv("MORI_INTAKE_PROMOTION_ENABLED", "true")
        monkeypatch.delenv("MORI_INTAKE_DATABASE_URL", raising=False)

        pipeline = self._make_pipeline(tmp_path)
        # Must not raise.
        asyncio.run(pipeline._run_intake_promotion())

    def test_promotion_error_never_propagates_into_run(self, tmp_path, monkeypatch):
        """Even when an INNER operation inside _run_intake_promotion raises, run() must not
        propagate the exception.

        We make ``asyncpg.create_pool`` raise inside the flag-on + PG-store path so the
        real try/except inside ``_run_intake_promotion`` (and the outer defence-in-depth
        guard in ``run()``) are exercised — NOT a replaced method, which would bypass both
        safety nets and prove nothing.
        """
        import asyncio
        import sys
        from unittest.mock import AsyncMock, MagicMock, patch

        from mori_advisor.bifrost_client import BifrostClient
        from mori_advisor.dream import DreamPipeline
        from mori_advisor.store.sqlite_store import SQLiteStore

        monkeypatch.setenv("MORI_INTAKE_PROMOTION_ENABLED", "true")
        monkeypatch.setenv("MORI_INTAKE_DATABASE_URL", "postgresql://fake/intake")

        db_path = tmp_path / "memories.db"
        store = SQLiteStore(db_path)
        store.bootstrap()

        client = MagicMock(spec=BifrostClient)
        pipeline = DreamPipeline(db_path=db_path, bifrost_client=client, store=store)

        # SQLiteStore has no 'pool' attribute, so _run_intake_promotion returns early
        # before the asyncpg path (no-pool guard fires first).  Give the mock store a
        # pool stub to get past the guard and reach the try/except block.
        store.pool = MagicMock()  # type: ignore[attr-defined]

        # Patch asyncpg.create_pool to simulate a hard connection failure INSIDE the
        # method's try block — this is an INNER operation failure, not a replaced method.
        mock_asyncpg = MagicMock()
        mock_asyncpg.create_pool = AsyncMock(
            side_effect=OSError("simulated: intake Postgres unreachable")
        )

        with patch.dict(sys.modules, {"asyncpg": mock_asyncpg}):
            # run() must not raise despite the inner failure.  The method's internal
            # try/except catches it; the outer guard in run() is defence-in-depth.
            result = asyncio.run(pipeline.run())

        assert result == [], (
            "run() must return [] cleanly even when _run_intake_promotion encounters "
            "a connection error internally"
        )
