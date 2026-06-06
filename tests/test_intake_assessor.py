"""Postgres integration tests for Stream B1 — assessor + canon writer.

Gated on ``MORI_INTAKE_TEST_DATABASE_URL``.  When the variable is not set
all tests in this module are skipped with a clear message.

Pure-logic / unit tests live in ``test_intake_assessor_unit.py`` and always
run without a database.

Setup:
* Each test gets a function-scoped asyncpg pool against the test Postgres.
* Migrations (intake + mori migration 10) are applied before each test.
* Tables are truncated for a hermetic starting state.
* The mori canon side uses a real SQLiteStore in a temp directory so
  canon writes can be verified without a second Postgres.
* Workers are driven directly (assess_once / drain_once) — no real sleeps.
* The FAST model is stubbed — deterministic, no network.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import uuid
from pathlib import Path

import pytest

# ── Module-level skip when no test DSN ───────────────────────────────────────

_TEST_DSN = os.environ.get("MORI_INTAKE_TEST_DATABASE_URL", "")

if not _TEST_DSN:
    pytest.skip(
        "MORI_INTAKE_TEST_DATABASE_URL is not set — skipping Postgres integration "
        "tests for assessor/canon_writer (Stream B1).  "
        "Set the variable to an asyncpg DSN pointing at a throwaway Postgres to run them.",
        allow_module_level=True,
    )

# ── Imports (only reached when DSN is set) ────────────────────────────────────

import pytest_asyncio  # noqa: E402

from mori_intake import migrations, worker  # noqa: E402
from mori_intake.assessor import AssessmentResult, assess_once  # noqa: E402
from mori_intake.canon_writer import drain_once  # noqa: E402
from mori_intake.normalize import content_hash  # noqa: E402

os.environ["MORI_INTAKE_DATABASE_URL"] = _TEST_DSN
os.environ.pop("MORI_DATABASE_URL", None)

import mori_intake.db as db  # noqa: E402

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_sqlite_mori_store():
    """Return a real SQLiteStore in a fresh temp directory with all migrations applied.

    Migration 10 (memory_intake_lineage) must be applied so the canon writer
    can write lineage rows.
    """
    from mori_advisor.store.migrations import MIGRATIONS, apply_sqlite
    from mori_advisor.store.sqlite_store import SQLiteStore

    tmp = tempfile.mkdtemp(prefix="mori-b1-test-")
    db_path = Path(tmp) / "memories.db"
    store = SQLiteStore(db_path)
    apply_sqlite(db_path, tuple(m for m in MIGRATIONS if m.target == "memories"))
    return store


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def pool():
    """Function-scoped intake pool with all migrations applied and tables truncated."""
    p = await db.create_pool()
    await migrations.apply(p)
    await p.execute(
        "TRUNCATE intake_corroborations, promotion_queue, intake_promotion_map, "
        "intake_candidates, intake_submissions RESTART IDENTITY CASCADE"
    )
    try:
        yield p
    finally:
        await db.close_pool()


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


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_assessor_unrelated_transitions_to_under_review(pool):
    """UNRELATED → candidate under_review + promotion_queue row queued."""
    _, cid = await _seed_pending_candidate(
        pool, content="Distributed tracing improves observability in microservice architectures."
    )

    processed = await assess_once(pool)  # default stub → UNRELATED
    assert processed >= 1

    candidate = await pool.fetchrow("SELECT status FROM intake_candidates WHERE id = $1::uuid", cid)
    assert candidate["status"] == "under_review"

    queued = await pool.fetchrow(
        "SELECT status FROM promotion_queue WHERE candidate_id = $1::uuid", cid
    )
    assert queued is not None
    assert queued["status"] == "queued"


@pytest.mark.asyncio
async def test_assessor_supersedes_transitions_to_rejected(pool):
    """SUPERSEDES → candidate rejected with correct rejection_reason."""
    _, cid = await _seed_pending_candidate(
        pool, content="Immutable infrastructure reduces configuration drift over time."
    )

    def supersedes_stub(body, h):
        return AssessmentResult(
            verdict="SUPERSEDES", matched_canon_name="known-canon-memory", score=0.97
        )

    processed = await assess_once(pool, assess=supersedes_stub)
    assert processed >= 1

    candidate = await pool.fetchrow(
        "SELECT status, rejection_reason FROM intake_candidates WHERE id = $1::uuid", cid
    )
    assert candidate["status"] == "rejected"
    assert candidate["rejection_reason"] == "duplicate-of-canon:known-canon-memory"


@pytest.mark.asyncio
async def test_assessor_related_transitions_to_rejected(pool):
    """RELATED → candidate rejected."""
    _, cid = await _seed_pending_candidate(
        pool, content="Blue-green deployments eliminate downtime during releases."
    )

    def related_stub(body, h):
        return AssessmentResult(verdict="RELATED", matched_canon_name="canon-bg", score=0.80)

    await assess_once(pool, assess=related_stub)

    candidate = await pool.fetchrow(
        "SELECT status, rejection_reason FROM intake_candidates WHERE id = $1::uuid", cid
    )
    assert candidate["status"] == "rejected"
    assert "duplicate-of-canon:canon-bg" in candidate["rejection_reason"]


@pytest.mark.asyncio
async def test_canon_writer_full_promotion_path(pool):
    """End-to-end: pending → under_review → promoted; canon memory + lineage + map."""
    mori_store = _make_sqlite_mori_store()
    content = "Circuit breakers prevent cascade failures in distributed systems."

    _, cid = await _seed_pending_candidate(pool, content=content)

    # Assess (UNRELATED) → under_review + queued
    await assess_once(pool)

    # Drain promotion queue → canon write
    committed = await drain_once(pool, mori_store)
    assert committed >= 1

    # Candidate should be promoted.
    candidate = await pool.fetchrow(
        "SELECT status, promoted_canon_name FROM intake_candidates WHERE id = $1::uuid", cid
    )
    assert candidate["status"] == "promoted"
    assert candidate["promoted_canon_name"] is not None
    canon_name = candidate["promoted_canon_name"]

    # intake_promotion_map row should exist.
    promo_map = await pool.fetchrow(
        "SELECT canon_name FROM intake_promotion_map WHERE candidate_id = $1::uuid", cid
    )
    assert promo_map is not None
    assert promo_map["canon_name"] == canon_name

    # Queue row should be committed.
    queue_row = await pool.fetchrow(
        "SELECT status FROM promotion_queue WHERE candidate_id = $1::uuid", cid
    )
    assert queue_row is not None
    assert queue_row["status"] == "committed"

    # mori canon memory should exist in SQLite.
    conn = sqlite3.connect(str(mori_store.db_path))
    try:
        row = conn.execute("SELECT name FROM memories WHERE name = ?", (canon_name,)).fetchone()
        assert row is not None, f"Canon memory {canon_name!r} not found in mori SQLite"

        lin_row = conn.execute(
            "SELECT canon_name, intake_candidate_id "
            "FROM memory_intake_lineage WHERE canon_name = ?",
            (canon_name,),
        ).fetchone()
        assert lin_row is not None, "memory_intake_lineage row missing"
        assert lin_row[1] == cid
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_canon_writer_idempotent_redrive(pool):
    """Re-driving the promotion queue after commit is a no-op — no duplicate canon row."""
    mori_store = _make_sqlite_mori_store()
    content = "Eventual consistency is a well-understood trade-off in distributed databases."

    _, cid = await _seed_pending_candidate(pool, content=content)
    await assess_once(pool)
    await drain_once(pool, mori_store)

    # Candidate promoted on first drain.
    candidate = await pool.fetchrow("SELECT status FROM intake_candidates WHERE id = $1::uuid", cid)
    assert candidate["status"] == "promoted"

    # Count canon rows before re-drive.
    conn = sqlite3.connect(str(mori_store.db_path))
    try:
        count_before = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        lin_before = conn.execute("SELECT COUNT(*) FROM memory_intake_lineage").fetchone()[0]
    finally:
        conn.close()

    # Re-drive: queue row is committed → WHERE clause excludes it → 0 committed.
    second_committed = await drain_once(pool, mori_store)
    assert second_committed == 0

    # Canon and lineage counts must not increase.
    conn = sqlite3.connect(str(mori_store.db_path))
    try:
        count_after = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        lin_after = conn.execute("SELECT COUNT(*) FROM memory_intake_lineage").fetchone()[0]
    finally:
        conn.close()

    assert count_after == count_before, "Duplicate canon memory created on re-drive"
    assert lin_after == lin_before, "Duplicate lineage row created on re-drive"


@pytest.mark.asyncio
async def test_idempotency_guard_via_promotion_map(pool):
    """Simulate crash-after-canon-write: promotion_map present but queue not committed.

    The canon writer must detect the existing map row, skip the mori write,
    and still mark the queue row committed.
    """
    mori_store = _make_sqlite_mori_store()
    content = "Write-ahead logging ensures durability in database crash recovery."

    _, cid = await _seed_pending_candidate(pool, content=content)
    await assess_once(pool)

    # Manually insert promotion_map row (simulating prior partial run).
    cid_uuid = uuid.UUID(cid)
    existing_canon_name = f"agent-intake-{content_hash(content)[:16]}"

    # Write canon memory + lineage directly to simulate the prior partial completion.
    mori_store.write(
        name=existing_canon_name,
        title="Pre-existing canon (crash recovery test)",
        body=content,
        type="feedback",
        tier="working",
    )
    conn = sqlite3.connect(str(mori_store.db_path))
    try:
        conn.execute(
            "INSERT OR IGNORE INTO memory_intake_lineage "
            "(canon_name, intake_candidate_id, intake_submission_ids, trust_snapshot) "
            "VALUES (?, ?, ?, ?)",
            (existing_canon_name, cid, "[]", "{}"),
        )
        conn.commit()
    finally:
        conn.close()

    await pool.execute(
        "INSERT INTO intake_promotion_map (canon_name, candidate_id, submission_ids) "
        "VALUES ($1, $2, $3)",
        existing_canon_name,
        cid_uuid,
        [],
    )

    # Canon writer must detect existing promotion_map and mark queue committed.
    committed = await drain_once(pool, mori_store)
    assert committed >= 1

    queue_row = await pool.fetchrow(
        "SELECT status FROM promotion_queue WHERE candidate_id = $1::uuid", cid_uuid
    )
    assert queue_row is not None
    assert queue_row["status"] == "committed"

    # Exactly one canon memory row must exist — no duplicate.
    conn = sqlite3.connect(str(mori_store.db_path))
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE name = ?", (existing_canon_name,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1, f"Expected 1 canon row, got {count}"
