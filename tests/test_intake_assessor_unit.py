"""Pure-logic / unit tests for Stream B1 assessor and canon writer.

These tests ALWAYS run — no database required.  Workers and their
internals are tested via async mocks.

Covers:
* Verdict→action mapping (SUPERSEDES, RELATED, UNRELATED, unknown).
* Idempotency guard: second drain of an already-promoted candidate is a
  no-op (no duplicate mori write).
* promotion_queue state machine helpers (_mark_committed, _mark_failed).
* Default assess stub always returns UNRELATED.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from mori_intake.assessor import AssessmentResult, _assess_one, _default_stub
from mori_intake.normalize import content_hash

# ── Async context manager helper ──────────────────────────────────────────────


class _AsyncCtxMgr:
    """Minimal async context manager that yields a single value."""

    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *args):
        pass


# ── Pool/conn factory ─────────────────────────────────────────────────────────


def _make_pool_conn():
    """Return (pool mock, conn mock) with transaction support."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=_AsyncCtxMgr(conn))
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtxMgr(conn))
    return pool, conn


def _make_row(candidate_id=None):
    """Return a fake asyncpg Record-like row for a pending candidate."""
    cid = candidate_id or uuid.uuid4()
    row = MagicMock()
    row.__getitem__ = lambda self, key: {
        "id": cid,
        "canonicalized_body": "Connection pooling improves database throughput significantly.",
        "content_hash": "a" * 64,
        "attempt_count": 0,
    }[key]
    return row


# ══════════════════════════════════════════════════════════════════════════════
# Default stub
# ══════════════════════════════════════════════════════════════════════════════


class TestDefaultStub:
    """The default stub now returns NEEDS_REVIEW (fail closed — GOV-005/SEC-002).

    Tests that want the full promotion path must inject an explicit UNRELATED
    stub — the default stub must no longer auto-promote.
    """

    def test_returns_needs_review(self):
        """Default stub returns NEEDS_REVIEW — fail closed, not auto-promote."""
        result = _default_stub("Some learning body.", "abc123")
        assert result.verdict == "NEEDS_REVIEW"
        assert result.matched_canon_name is None
        assert result.score == 0.0

    def test_ignores_inputs(self):
        """Default stub always returns the same NEEDS_REVIEW regardless of input."""
        r1 = _default_stub("body one", "hash1")
        r2 = _default_stub("completely different body here", "hash2")
        assert r1.verdict == r2.verdict == "NEEDS_REVIEW"

    def test_does_not_return_unrelated(self):
        """UNRELATED from the default stub would auto-promote — must not happen."""
        result = _default_stub("Some body.", "somehash")
        assert result.verdict != "UNRELATED", (
            "Default stub must NOT return UNRELATED — that triggers auto-promotion. "
            "Inject an explicit stub when testing the promotion path."
        )


# ══════════════════════════════════════════════════════════════════════════════
# Verdict → action mapping
# ══════════════════════════════════════════════════════════════════════════════


class TestVerdictActionMapping:
    """Unit-test the verdict→status transition via _assess_one with mock pool."""

    def test_supersedes_sets_rejected(self):
        pool, conn = _make_pool_conn()
        row = _make_row()

        def stub(body, h):
            return AssessmentResult(
                verdict="SUPERSEDES", matched_canon_name="existing-canon", score=0.99
            )

        asyncio.run(_assess_one(pool, row, stub))

        calls_str = " ".join(str(c) for c in conn.execute.call_args_list)
        assert "rejected" in calls_str
        assert "duplicate-of-canon:existing-canon" in calls_str

    def test_related_sets_rejected(self):
        pool, conn = _make_pool_conn()
        row = _make_row()

        def stub(body, h):
            return AssessmentResult(
                verdict="RELATED", matched_canon_name="related-canon", score=0.82
            )

        asyncio.run(_assess_one(pool, row, stub))

        calls_str = " ".join(str(c) for c in conn.execute.call_args_list)
        assert "rejected" in calls_str
        assert "duplicate-of-canon:related-canon" in calls_str

    def test_unrelated_sets_under_review_and_enqueues(self):
        pool, conn = _make_pool_conn()
        row = _make_row()

        def stub(body, h):
            return AssessmentResult(verdict="UNRELATED")

        asyncio.run(_assess_one(pool, row, stub))

        calls_str = " ".join(str(c) for c in conn.execute.call_args_list)
        assert "under_review" in calls_str
        assert "promotion_queue" in calls_str

    def test_supersedes_with_null_matched_name_uses_unknown(self):
        """matched_canon_name=None falls back to 'unknown' in rejection_reason."""
        pool, conn = _make_pool_conn()
        row = _make_row()

        def stub(body, h):
            return AssessmentResult(verdict="SUPERSEDES", matched_canon_name=None, score=0.95)

        asyncio.run(_assess_one(pool, row, stub))

        calls_str = " ".join(str(c) for c in conn.execute.call_args_list)
        assert "duplicate-of-canon:unknown" in calls_str

    def test_unknown_verdict_raises_value_error(self):
        pool, conn = _make_pool_conn()
        row = _make_row()

        def stub(body, h):
            return AssessmentResult(verdict="MAYBE")

        with pytest.raises(ValueError, match="unknown verdict"):
            asyncio.run(_assess_one(pool, row, stub))

    def test_needs_review_does_not_raise_and_leaves_pending(self):
        """NEEDS_REVIEW is a valid verdict — must not raise ValueError.

        The candidate stays pending (no UPDATE issued): _assess_one must execute
        the NEEDS_REVIEW branch without any DB write.
        """
        pool, conn = _make_pool_conn()
        row = _make_row()

        def stub(body, h):
            return AssessmentResult(verdict="NEEDS_REVIEW")

        result = asyncio.run(_assess_one(pool, row, stub))

        assert result.verdict == "NEEDS_REVIEW"
        # No UPDATE should have been called (candidate stays pending).
        calls_str = " ".join(str(c) for c in conn.execute.call_args_list)
        assert "under_review" not in calls_str
        assert "rejected" not in calls_str

    def test_verdict_case_normalised_to_upper(self):
        """Lower-case verdict strings are normalised to upper before mapping."""
        pool, conn = _make_pool_conn()
        row = _make_row()

        def stub(body, h):
            return AssessmentResult(verdict="unrelated")  # lower-case

        asyncio.run(_assess_one(pool, row, stub))

        calls_str = " ".join(str(c) for c in conn.execute.call_args_list)
        assert "under_review" in calls_str


# ══════════════════════════════════════════════════════════════════════════════
# Idempotency guard — second drain is a no-op
# ══════════════════════════════════════════════════════════════════════════════


class TestCanonWriterIdempotencyUnit:
    """The idempotency guard in _promote_one.

    When intake_promotion_map already has a row for the candidate_id, the
    mori store write() must NOT be called again.
    """

    def test_second_drain_skips_canon_write(self):
        from mori_intake.canon_writer import _promote_one

        candidate_id = uuid.uuid4()
        queue_id = uuid.uuid4()
        existing_canon_name = "agent-intake-deadbeef12345678"

        conn = AsyncMock()
        # fetchrow returns the existing map row → guard fires on first call.
        conn.fetchrow = AsyncMock(return_value={"canon_name": existing_canon_name})
        conn.execute = AsyncMock(return_value=None)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=_AsyncCtxMgr(conn))

        mori_store = MagicMock()
        mori_store.write = MagicMock(return_value="Memory written")

        result = asyncio.run(_promote_one(pool, mori_store, queue_id, candidate_id))

        assert result is True
        # mori store write must NOT be called — guard short-circuits.
        mori_store.write.assert_not_called()

    def test_second_drain_marks_queue_committed(self):
        """Even on a no-op re-drive the queue row is marked committed."""
        from mori_intake.canon_writer import _promote_one

        candidate_id = uuid.uuid4()
        queue_id = uuid.uuid4()

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value={"canon_name": "agent-intake-deadbeef12345678"})
        conn.execute = AsyncMock(return_value=None)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=_AsyncCtxMgr(conn))

        mori_store = MagicMock()
        mori_store.write = MagicMock(return_value="Memory written")

        asyncio.run(_promote_one(pool, mori_store, queue_id, candidate_id))

        calls_str = " ".join(str(c) for c in conn.execute.call_args_list)
        assert "committed" in calls_str


# ══════════════════════════════════════════════════════════════════════════════
# promotion_queue state machine helpers
# ══════════════════════════════════════════════════════════════════════════════


class TestPromotionQueueStateMachine:
    def test_mark_committed_sql_contains_committed(self):
        from mori_intake.canon_writer import _mark_committed

        conn = AsyncMock()
        conn.execute = AsyncMock()
        asyncio.run(_mark_committed(conn, uuid.uuid4(), "some-canon"))
        sql = conn.execute.call_args[0][0]
        assert "committed" in sql

    def test_mark_failed_sql_updates_attempt_count_and_error(self):
        from mori_intake.canon_writer import _mark_failed

        conn = AsyncMock()
        conn.execute = AsyncMock()
        asyncio.run(_mark_failed(conn, uuid.uuid4(), "timeout connecting to mori"))
        sql = conn.execute.call_args[0][0]
        assert "attempt_count" in sql
        assert "error_message" in sql

    def test_mark_committed_passes_canon_name(self):
        from mori_intake.canon_writer import _mark_committed

        conn = AsyncMock()
        conn.execute = AsyncMock()
        qid = uuid.uuid4()
        asyncio.run(_mark_committed(conn, qid, "agent-intake-abc123"))

        args = conn.execute.call_args[0]
        # First positional arg after the SQL is the canon_name.
        assert "agent-intake-abc123" in args

    def test_mark_failed_resets_status_to_queued_below_cap(self):
        """_mark_failed sets status back to 'queued' (not 'processing') below cap."""
        from mori_intake.canon_writer import _mark_failed

        conn = AsyncMock()
        conn.execute = AsyncMock()
        asyncio.run(_mark_failed(conn, uuid.uuid4(), "transient error"))
        sql = conn.execute.call_args[0][0]
        # The CASE expression should produce 'failed' or 'queued' — never
        # 'processing', which would prevent stale-lease reclaim.
        assert "queued" in sql
        assert "processing" not in sql


# ══════════════════════════════════════════════════════════════════════════════
# Fix 3: processing lease + stale-lease reclaim
# ══════════════════════════════════════════════════════════════════════════════


class TestProcessingLease:
    """_fetch_and_lease_batch sets status='processing' atomically."""

    def test_fetch_and_lease_sets_processing_status(self):
        """Batch fetch must UPDATE status='processing' inside the same transaction."""
        from mori_intake.canon_writer import _fetch_and_lease_batch

        queue_id = uuid.uuid4()
        candidate_id = uuid.uuid4()

        row = MagicMock()
        row.__getitem__ = lambda self, key: {
            "id": queue_id,
            "candidate_id": candidate_id,
            "attempt_count": 0,
        }[key]
        row.__iter__ = lambda self: iter({"id": queue_id})

        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[row])
        conn.execute = AsyncMock(return_value=None)
        conn.transaction = MagicMock(return_value=_AsyncCtxMgr(conn))

        pool = MagicMock()
        pool.acquire = MagicMock(return_value=_AsyncCtxMgr(conn))

        asyncio.run(_fetch_and_lease_batch(pool, 20))

        # The UPDATE … SET status = 'processing' must have been called.
        all_sql = " ".join(str(c) for c in conn.execute.call_args_list)
        assert "processing" in all_sql

    def test_fetch_query_reclaims_stale_leases(self):
        """The fetch SQL must include the stale-lease reclaim clause."""
        from mori_intake.canon_writer import _fetch_and_lease_batch

        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock(return_value=None)
        conn.transaction = MagicMock(return_value=_AsyncCtxMgr(conn))

        pool = MagicMock()
        pool.acquire = MagicMock(return_value=_AsyncCtxMgr(conn))

        asyncio.run(_fetch_and_lease_batch(pool, 20))

        # The SELECT SQL must filter for stale processing rows.
        fetch_sql = conn.fetch.call_args[0][0]
        assert "processing" in fetch_sql, "Fetch query must address 'processing' rows"
        assert "updated_at" in fetch_sql, "Fetch query must check updated_at for stale lease"

    def test_no_double_promote_after_crash_via_idempotency_guard(self):
        """Crash-after-canon-write scenario: re-drive hits idempotency guard.

        Simulates: canon written + lineage written, but final intake commit
        failed (promotion_map absent).  On re-drive the promotion_map check
        fires (row now present from a prior committed pass) → write() skipped.

        This test verifies the idempotency guard path from _promote_one.
        """
        from mori_intake.canon_writer import _promote_one

        candidate_id = uuid.uuid4()
        queue_id = uuid.uuid4()
        canon_name = "agent-intake-crashtest12345678"

        conn = AsyncMock()
        # First fetchrow (idempotency check) returns an existing map row.
        conn.fetchrow = AsyncMock(return_value={"canon_name": canon_name})
        conn.execute = AsyncMock(return_value=None)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=_AsyncCtxMgr(conn))

        mori_store = MagicMock()
        mori_store.write = MagicMock(return_value="written")
        mori_store.record_intake_lineage = MagicMock(return_value=None)

        result = asyncio.run(_promote_one(pool, mori_store, queue_id, candidate_id))

        assert result is True
        mori_store.write.assert_not_called()
        mori_store.record_intake_lineage.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# Fix 4: public record_intake_lineage — no _get_conn() access
# ══════════════════════════════════════════════════════════════════════════════


class TestPublicLineageAPI:
    """canon_writer uses mori_store.record_intake_lineage() not _get_conn()."""

    def test_record_lineage_called_not_get_conn(self):
        """canon_writer must call record_intake_lineage(), never _get_conn()."""
        from mori_intake.canon_writer import _promote_one

        candidate_id = uuid.uuid4()
        queue_id = uuid.uuid4()
        _body = "Full body of candidate memory."
        content_hash_value = content_hash(_body)

        # conn: fetchrow returns None (no existing map row), then candidate row,
        # then the GOV-002 submission join (3rd call added for delta-hardening).
        fetchrow_results = [
            None,  # idempotency check — no existing map row
            {
                "canonicalized_body": _body,
                "content_hash": content_hash_value,
                "reinforcement_count": 3,
            },
            {  # GOV-002: originating submission — eligible key so body check fires
                "target_name": "memory",
                "stable_key": "learned-valid-key",
                "action": "add",
            },
        ]
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(side_effect=fetchrow_results)
        conn.fetch = AsyncMock(return_value=[])  # no corroborations
        conn.execute = AsyncMock(return_value=None)
        conn.transaction = MagicMock(return_value=_AsyncCtxMgr(conn))
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=_AsyncCtxMgr(conn))

        mori_store = MagicMock()
        mori_store.write = MagicMock(return_value="Memory 'agent-intake-...' written")
        mori_store.record_intake_lineage = MagicMock(return_value=None)

        asyncio.run(_promote_one(pool, mori_store, queue_id, candidate_id))

        # Public method must have been called.
        mori_store.record_intake_lineage.assert_called_once()
        # Private _get_conn must NOT have been called (Fix 4).
        assert not hasattr(mori_store, "_get_conn") or not mori_store._get_conn.called

    def test_record_lineage_receives_correct_args(self):
        """record_intake_lineage is called with the right keyword arguments."""
        from mori_intake.canon_writer import _promote_one

        candidate_id = uuid.uuid4()
        queue_id = uuid.uuid4()
        _body = "Candidate body text."
        content_hash_value = content_hash(_body)

        fetchrow_results = [
            None,
            {
                "canonicalized_body": _body,
                "content_hash": content_hash_value,
                "reinforcement_count": 2,
            },
            {  # GOV-002: originating submission — eligible key so body check fires
                "target_name": "memory",
                "stable_key": "learned-valid-key",
                "action": "add",
            },
        ]
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(side_effect=fetchrow_results)
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock(return_value=None)
        conn.transaction = MagicMock(return_value=_AsyncCtxMgr(conn))
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=_AsyncCtxMgr(conn))

        mori_store = MagicMock()
        mori_store.write = MagicMock(return_value="written")
        mori_store.record_intake_lineage = MagicMock(return_value=None)

        asyncio.run(_promote_one(pool, mori_store, queue_id, candidate_id))

        call_kwargs = mori_store.record_intake_lineage.call_args.kwargs
        expected_canon_name = f"agent-intake-{content_hash_value[:16]}"
        assert call_kwargs["canon_name"] == expected_canon_name
        assert call_kwargs["intake_candidate_id"] == str(candidate_id)
        assert isinstance(call_kwargs["intake_submission_ids"], list)
        assert isinstance(call_kwargs["trust_snapshot"], dict)
        assert "promoted_at" in call_kwargs

    def test_sqlite_store_record_lineage_creates_fresh_connection(self, tmp_path):
        """SQLiteStore.record_intake_lineage opens its own connection and closes it.

        Verifies that the public method does not grab or close any externally
        owned connection — it is fully self-contained.  SQLiteStore is not
        constructed here (it requires the `nats` module which is not installed
        in CI); instead we exercise the underlying SQLite logic directly via a
        minimal stub that carries only the two fields our methods use.
        """
        import sqlite3
        from datetime import datetime, timezone
        from unittest.mock import MagicMock

        db_path = tmp_path / "memories.db"

        # Bootstrap the memory_intake_lineage table (same DDL as migration 10).
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS memory_intake_lineage ("
            "  canon_name             TEXT        PRIMARY KEY,"
            "  intake_candidate_id    TEXT        NOT NULL,"
            "  intake_submission_ids  TEXT        NOT NULL DEFAULT '[]',"
            "  trust_snapshot         TEXT        NOT NULL DEFAULT '{}',"
            "  promoted_at            TEXT        NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
        conn.commit()
        conn.close()

        # Build a minimal stub that has only the db_path attribute needed by
        # SQLiteStore.record_intake_lineage / get_intake_lineage.
        stub = MagicMock(spec=[])  # empty spec — no accidental write methods
        stub.db_path = db_path

        # Bind the real methods from SQLiteStore onto the stub so we test
        # the actual SQL without constructing the full store.
        from mori_advisor.store.sqlite_store import SQLiteStore

        stub.record_intake_lineage = SQLiteStore.record_intake_lineage.__get__(stub)
        stub.get_intake_lineage = SQLiteStore.get_intake_lineage.__get__(stub)

        stub.record_intake_lineage(
            canon_name="test-lineage-canon",
            intake_candidate_id=str(uuid.uuid4()),
            intake_submission_ids=[str(uuid.uuid4())],
            trust_snapshot={"reinforcement_count": 1},
            promoted_at=datetime.now(timezone.utc),
        )

        # Verify the row was written.
        result = stub.get_intake_lineage("test-lineage-canon")
        assert result is not None
        assert result["canon_name"] == "test-lineage-canon"
        assert result["trust_snapshot"]["reinforcement_count"] == 1

    def test_sqlite_store_record_lineage_idempotent(self, tmp_path):
        """Calling record_intake_lineage twice for the same canon_name is a no-op."""
        import sqlite3
        from datetime import datetime, timezone
        from unittest.mock import MagicMock

        db_path = tmp_path / "memories.db"

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS memory_intake_lineage ("
            "  canon_name             TEXT        PRIMARY KEY,"
            "  intake_candidate_id    TEXT        NOT NULL,"
            "  intake_submission_ids  TEXT        NOT NULL DEFAULT '[]',"
            "  trust_snapshot         TEXT        NOT NULL DEFAULT '{}',"
            "  promoted_at            TEXT        NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
        conn.commit()
        conn.close()

        stub = MagicMock(spec=[])
        stub.db_path = db_path

        from mori_advisor.store.sqlite_store import SQLiteStore

        stub.record_intake_lineage = SQLiteStore.record_intake_lineage.__get__(stub)
        stub.get_intake_lineage = SQLiteStore.get_intake_lineage.__get__(stub)

        cid = str(uuid.uuid4())
        kwargs = dict(
            canon_name="idempotent-canon",
            intake_candidate_id=cid,
            intake_submission_ids=[],
            trust_snapshot={"reinforcement_count": 1},
            promoted_at=datetime.now(timezone.utc),
        )
        stub.record_intake_lineage(**kwargs)
        stub.record_intake_lineage(**kwargs)  # second call must not raise

        result = stub.get_intake_lineage("idempotent-canon")
        assert result is not None  # exactly one row
