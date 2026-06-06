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
    def test_always_unrelated(self):
        result = _default_stub("Some learning body.", "abc123")
        assert result.verdict == "UNRELATED"
        assert result.matched_canon_name is None
        assert result.score == 0.0

    def test_ignores_inputs(self):
        r1 = _default_stub("body one", "hash1")
        r2 = _default_stub("completely different body here", "hash2")
        assert r1.verdict == r2.verdict == "UNRELATED"


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
