"""Postgres integration tests for mori-intake.

Gated on the ``MORI_INTAKE_TEST_DATABASE_URL`` environment variable.  When the
variable is not set, all tests in this module are skipped with a clear message.

No real sleeps: the drain worker is driven directly via
``worker.drain_once(pool)`` so tests are deterministic.

Setup: tests share one asyncpg pool + run migrations once per session.  Each
test function cleans up its own rows so tests are order-independent.
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio

# ── Skip if no test DSN ───────────────────────────────────────────────────────

_TEST_DSN = os.environ.get("MORI_INTAKE_TEST_DATABASE_URL", "")

if not _TEST_DSN:
    pytest.skip(
        "MORI_INTAKE_TEST_DATABASE_URL is not set — skipping Postgres integration tests. "
        "Set the variable to an asyncpg DSN pointing at a throwaway Postgres to run them.",
        allow_module_level=True,
    )

# ── Imports (only reached when DSN is set) ────────────────────────────────────

import asyncpg  # noqa: E402

from mori_intake import migrations, worker  # noqa: E402
from mori_intake.normalize import content_hash  # noqa: E402

# Override the config DSN so db.create_pool() uses the test database.
os.environ["MORI_INTAKE_DATABASE_URL"] = _TEST_DSN
# Ensure data-boundary guard doesn't fire for the test DSN.
os.environ.pop("MORI_DATABASE_URL", None)

import mori_intake.db as db  # noqa: E402 — must be after env var override

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def pool():
    """Function-scoped pool so the fixture and each test share ONE event loop.

    pytest-asyncio 1.x runs each test on its own function-scoped loop; a
    session-scoped asyncpg pool would be bound to a different loop and raise
    "attached to a different loop".  Migrations are idempotent (CREATE IF NOT
    EXISTS + ledger), so applying them per test is cheap.  All intake tables are
    truncated for a hermetic starting state.

    Isolation note: ``mori_intake.db._pool`` is a module-level global shared
    across test modules.  A sibling module (e.g. ``test_intake_assessor``) may
    call ``close_pool()`` and null the global between fixture setup and a test
    that exercises app handlers via ``db.get_pool()``.  We therefore force a
    clean pool creation by resetting ``db._pool = None`` before ``create_pool()``
    so the module-global is always rebound to the live test pool for the duration
    of this test, regardless of what another fixture left behind.
    """
    db._pool = None  # force fresh pool; deterministically rebind the global
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


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _insert_submission(
    pool, *, content: str, stable_key: str | None = None, session_id: str | None = None
) -> str:
    """Insert a raw submission and return its UUID string."""
    sid = session_id or f"test-session-{uuid.uuid4()}"
    sk = stable_key or f"learned-test-{uuid.uuid4()}"
    row_id = await pool.fetchval(
        "INSERT INTO intake_submissions "
        "  (id, session_id, agent_id, target_name, action, stable_key, raw_source_text) "
        "VALUES ($1, $2, 'test-agent', 'memory', 'add', $3, $4) "
        "RETURNING id",
        uuid.uuid4(),
        sid,
        sk,
        content,
    )
    return str(row_id)


async def _clean(pool, *ids: str) -> None:
    """Delete test rows by submission id, cascading to corroborations."""
    for sid in ids:
        await pool.execute("DELETE FROM intake_submissions WHERE id = $1::uuid", sid)


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_migrations_idempotent(pool):
    """Applying migrations a second time must not raise; ledger count stays stable."""
    await migrations.apply(pool)  # second apply — idempotency check
    count = await pool.fetchval("SELECT COUNT(*) FROM intake_schema_migrations")
    assert count == len(migrations.MIGRATIONS)


@pytest.mark.asyncio
async def test_post_eligible_creates_submission(pool):
    """An eligible submission lands in intake_submissions as a new row."""
    content = "The system consistently performs better with connection pooling enabled."
    sid = await _insert_submission(pool, content=content)

    row = await pool.fetchrow(
        "SELECT id, raw_source_text FROM intake_submissions WHERE id = $1::uuid", sid
    )
    assert row is not None
    assert row["raw_source_text"] == content

    # Cleanup
    await _clean(pool, sid)


@pytest.mark.asyncio
async def test_duplicate_session_stable_key(pool):
    """Re-inserting the same (session_id, stable_key) violates the UNIQUE constraint.

    The app handles this as an idempotency hit (202 duplicate:true) by
    checking before inserting.  At the DB level, a second raw INSERT raises.
    """
    session_id = f"test-session-{uuid.uuid4()}"
    stable_key = f"learned-dedup-{uuid.uuid4()}"
    content = "Idempotency is a key property of robust distributed systems."

    sid1 = await _insert_submission(
        pool, content=content, stable_key=stable_key, session_id=session_id
    )

    # A second insert with the same (session_id, stable_key) must fail at DB level.
    with pytest.raises(asyncpg.UniqueViolationError):
        await pool.execute(
            "INSERT INTO intake_submissions "
            "  (id, session_id, agent_id, target_name, action, stable_key, raw_source_text) "
            "VALUES ($1, $2, 'test-agent', 'memory', 'add', $3, $4)",
            uuid.uuid4(),
            session_id,
            stable_key,
            content,
        )

    await _clean(pool, sid1)


@pytest.mark.asyncio
async def test_worker_drains_submission_creates_candidate(pool):
    """After drain_once, the submission has a corroboration and a pending candidate."""
    content = "Asynchronous I/O significantly reduces thread contention in high-load scenarios."
    sid = await _insert_submission(pool, content=content)

    drained = await worker.drain_once(pool)
    assert drained >= 1

    # Check corroboration exists.
    corr = await pool.fetchrow(
        "SELECT candidate_id FROM intake_corroborations WHERE submission_id = $1::uuid",
        sid,
    )
    assert corr is not None

    # Check candidate is pending.
    candidate = await pool.fetchrow(
        "SELECT status, content_hash, reinforcement_count FROM intake_candidates WHERE id = $1",
        corr["candidate_id"],
    )
    assert candidate is not None
    assert candidate["status"] == "pending"
    assert candidate["content_hash"] == content_hash(content)
    assert candidate["reinforcement_count"] == 1

    # Cleanup: delete submission (cascades corroborations); delete candidate separately.
    await _clean(pool, sid)
    await pool.execute("DELETE FROM intake_candidates WHERE id = $1", corr["candidate_id"])


@pytest.mark.asyncio
async def test_worker_reinforces_duplicate_body(pool):
    """Two submissions with identical bodies produce one candidate with reinforcement_count == 2."""
    content = "Event-driven architecture decouples producers from consumers at scale."

    sid1 = await _insert_submission(pool, content=content)
    sid2 = await _insert_submission(pool, content=content)

    # Drain both.
    await worker.drain_once(pool)
    await worker.drain_once(pool)

    h = content_hash(content)
    candidate = await pool.fetchrow(
        "SELECT id, reinforcement_count FROM intake_candidates WHERE content_hash = $1",
        h,
    )
    assert candidate is not None
    assert candidate["reinforcement_count"] == 2

    # Cleanup.
    await _clean(pool, sid1, sid2)
    await pool.execute("DELETE FROM intake_candidates WHERE id = $1", candidate["id"])


@pytest.mark.asyncio
async def test_post_idempotent_duplicate(pool):
    """POSTing the same (session_id, stable_key) twice → duplicate:true, exactly one row.

    Exercises the real POST handler (eligibility + race-free ON CONFLICT
    idempotency) over ASGI on the test loop.
    """
    import httpx

    from mori_advisor.auth import init_auth
    from mori_intake.app import app

    init_auth()  # open mode — no keys configured

    payload = {
        "session_id": f"idem-{uuid.uuid4()}",
        "agent_id": "test-agent",
        "target": "memory",
        "action": "add",
        "stable_key": f"learned-idem-{uuid.uuid4()}",
        "content": "Connection pooling materially improves throughput under sustained load.",
    }

    # Bind the live fixture pool on the EXACT db module the handler uses.
    # A sibling test module's sys.modules churn can leave the app's
    # mori_intake.db bound to a different instance than this test module's
    # `db`, so set _pool via the app module's own reference (what get_pool reads).
    import mori_intake.app as _appmod

    _appmod.db._pool = pool
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r1 = await client.post("/intake/submissions", json=payload)
        r2 = await client.post("/intake/submissions", json=payload)

    assert r1.status_code == 202
    assert r1.json()["duplicate"] is False
    assert r2.status_code == 202
    assert r2.json()["duplicate"] is True
    assert r1.json()["submission_id"] == r2.json()["submission_id"]

    count = await pool.fetchval(
        "SELECT COUNT(*) FROM intake_submissions WHERE session_id = $1",
        payload["session_id"],
    )
    assert count == 1

    await pool.execute(
        "DELETE FROM intake_submissions WHERE session_id = $1", payload["session_id"]
    )


@pytest.mark.asyncio
async def test_post_ineligible_rejected(pool):
    """An ineligible namespace → 422 with a reason and NO submissions row."""
    import httpx

    from mori_advisor.auth import init_auth
    from mori_intake.app import app

    init_auth()

    payload = {
        "session_id": f"inelig-{uuid.uuid4()}",
        "agent_id": "test-agent",
        "target": "memory",
        "action": "add",
        "stable_key": f"scratch-{uuid.uuid4()}",  # denied namespace
        "content": "This is scratch chatter that must never be proposed.",
    }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/intake/submissions", json=payload)

    assert resp.status_code == 422
    assert resp.json()["reason"] == "namespace-not-allowlisted"

    count = await pool.fetchval(
        "SELECT COUNT(*) FROM intake_submissions WHERE session_id = $1",
        payload["session_id"],
    )
    assert count == 0


@pytest.mark.asyncio
async def test_candidates_endpoint_returns_pending(pool):
    """After draining, GET /intake/candidates returns the pending candidate.

    Exercises the real FastAPI handler over ASGI on the SAME event loop as the
    test (httpx ASGITransport, no lifespan) — so the handler's pool query runs
    on this loop, not a TestClient-spawned one.  ``db.create_pool()`` (called by
    the fixture) already bound the module-global pool, so the handler picks it
    up via ``db.get_pool()``.
    """
    import httpx

    from mori_advisor.auth import init_auth
    from mori_intake.app import app

    init_auth()  # open mode — no keys configured, so the handler allows the call

    content = "CQRS separates read and write models, improving scalability for complex domains."
    sid = await _insert_submission(pool, content=content)
    await worker.drain_once(pool)

    import mori_intake.app as _appmod

    _appmod.db._pool = pool  # bind on the exact db module the handler uses
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/intake/candidates?status=pending&limit=100")
    assert resp.status_code == 200

    hashes = [row["content_hash"] for row in resp.json()]
    assert content_hash(content) in hashes

    # Cleanup.
    await _clean(pool, sid)
    await pool.execute(
        "DELETE FROM intake_candidates WHERE content_hash = $1", content_hash(content)
    )
