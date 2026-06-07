"""Postgres integration tests for the human-review gate (Full two-phase B).

Gated on BOTH ``MORI_INTAKE_TEST_DATABASE_URL`` (intake Postgres) and
``MORI_CANON_TEST_DATABASE_URL`` (mori canon Postgres) — identical to
``test_intake_assessor.py``.  The agent-intake promotion feature is
Postgres-only; the canon side targets a real ``PostgresStore``.

Flow under test
---------------
    assess (UNRELATED, gate)  → candidate under_review
                              + mori pending_write (source=agent-intake, pending)
                              + intake_promotion_tickets row (trusted carrier)
    TD approve                → pending_write 'human_approved' (a VOTE, no canon)
    finalize_once             → GOV-002 re-check → canon + lineage + promoted
    TD reject                 → pending_write 'rejected'
    finalize_once             → candidate reconciled to 'rejected'

Adversarial cases: forged provenance (no/invalid ticket), body-integrity
tampering, idempotent re-drive, missing mori_store (fail closed).
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
        f"Skipping human-review-gate integration tests — {', '.join(_missing)} not set. "
        "Set both to asyncpg DSNs pointing at throwaway Postgres instances.",
        allow_module_level=True,
    )

# ── Imports (only reached when both DSNs are set) ─────────────────────────────

import pytest_asyncio  # noqa: E402

from mori_intake import migrations, worker  # noqa: E402
from mori_intake.assessor import AssessmentResult, assess_once  # noqa: E402
from mori_intake.canon_writer import finalize_once  # noqa: E402

os.environ["MORI_INTAKE_DATABASE_URL"] = _INTAKE_DSN
os.environ.pop("MORI_DATABASE_URL", None)

import mori_intake.db as intake_db  # noqa: E402

_PROPOSED_BY = "intake-assessor"


def _unrelated_stub(body, h):
    return AssessmentResult(verdict="UNRELATED", score=0.5)


# ── Canon store factory ───────────────────────────────────────────────────────


async def _make_pg_mori_store(dsn: str):
    from mori_advisor.store.migrations import MIGRATIONS, apply_postgres
    from mori_advisor.store.postgres_store import PostgresStore

    store = PostgresStore(dsn)
    await apply_postgres(store, MIGRATIONS)
    return store


async def _truncate_canon(store) -> None:
    async with store.pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE intake_promotion_tickets, pending_writes, memory_intake_lineage, "
            "memories RESTART IDENTITY CASCADE"
        )


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def intake_pool():
    intake_db._pool = None
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
    store = await _make_pg_mori_store(_CANON_DSN)
    await _truncate_canon(store)
    try:
        yield store
    finally:
        await store.pool.close()


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _seed_pending_candidate(pool, *, content: str) -> tuple[str, str]:
    """Insert a submission + drain the dedup worker → (submission_id, candidate_id)."""
    session_id = f"test-session-{uuid.uuid4()}"
    stable_key = f"learned-gate-{uuid.uuid4()}"
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
    assert corr is not None, "dedup worker did not create a corroboration row"
    return str(submission_id), str(corr["candidate_id"])


async def _candidate(pool, cid: str) -> dict:
    row = await pool.fetchrow(
        "SELECT status, canonicalized_body, content_hash, promoted_canon_name "
        "FROM intake_candidates WHERE id = $1::uuid",
        cid,
    )
    return dict(row)


async def _only_pending_write(store, status: str = "pending") -> dict:
    rows = await store.pending_list_json(status=status, proposed_by=_PROPOSED_BY)
    assert len(rows) == 1, f"expected exactly one {status} agent-intake row, got {len(rows)}"
    return rows[0]


# Terminal statuses a dead-and-reconciled agent-intake proposal can land in.  A
# full finalize_once runs phase 2a (reject) THEN phase 2b (reconcile sweep), so a
# row the finalizer rejects on GOV-002 grounds is swept to 'rejected_reconciled'
# in the same pass.  Either is an acceptable terminal state.
_DEAD = {"rejected", "rejected_reconciled"}


async def _pw_status(store, write_id) -> str | None:
    """Return a pending_write's current status across all statuses, or None."""
    rows = await store.pending_list_json(status="", proposed_by=_PROPOSED_BY)
    for r in rows:
        if r["id"] == write_id:
            return r["status"]
    return None


async def _surface(intake_pool, canon_store, *, content: str) -> tuple[str, dict]:
    """Seed + assess through the human gate → (candidate_id, pending_write dict)."""
    _, cid = await _seed_pending_candidate(intake_pool, content=content)
    await assess_once(
        intake_pool, assess=_unrelated_stub, mori_store=canon_store, promotion_enabled=False
    )
    pw = await _only_pending_write(canon_store, "pending")
    return cid, pw


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_human_gate_surfaces_pending_write_and_ticket(intake_pool, canon_store):
    """UNRELATED (gate) → under_review + pending_write + ticket; NO promotion_queue row."""
    content = "Service meshes decouple traffic management from application code."
    cid, pw = await _surface(intake_pool, canon_store, content=content)

    cand = await _candidate(intake_pool, cid)
    assert cand["status"] == "under_review"

    # No legacy promotion_queue row under the gate.
    q = await intake_pool.fetchrow(
        "SELECT 1 FROM promotion_queue WHERE candidate_id = $1::uuid", cid
    )
    assert q is None, "gate path must NOT enqueue promotion_queue"

    # pending_write shape.
    expected_name = f"agent-intake-{cand['content_hash'][:16]}"
    assert pw["name"] == expected_name
    assert pw["source"] == "agent-intake"
    assert pw["status"] == "pending"
    assert pw["tier"] == "working"

    # Provenance carries ONLY an opaque ticket_uuid — never the trusted ids.
    prov = pw["provenance"]
    assert isinstance(prov, dict) and set(prov.keys()) == {"ticket_uuid"}
    ticket_uuid = prov["ticket_uuid"]

    # Ticket is the trusted carrier of candidate_id + body_hash.
    ticket = await canon_store.get_intake_ticket(ticket_uuid)
    assert ticket is not None
    assert ticket["canon_name"] == expected_name
    assert str(ticket["candidate_id"]) == cid
    assert ticket["body_hash"] == cand["content_hash"]

    # No canon memory written yet — only a proposal.
    assert await canon_store.get_memory(expected_name) is None


@pytest.mark.asyncio
async def test_legacy_path_when_flag_on(intake_pool, canon_store):
    """promotion_enabled=True → promotion_queue row, NO pending_write."""
    _, cid = await _seed_pending_candidate(
        intake_pool, content="Feature flags decouple deploy from release."
    )
    await assess_once(
        intake_pool, assess=_unrelated_stub, mori_store=canon_store, promotion_enabled=True
    )

    q = await intake_pool.fetchrow(
        "SELECT status FROM promotion_queue WHERE candidate_id = $1::uuid", cid
    )
    assert q is not None and q["status"] == "queued"

    rows = await canon_store.pending_list_json(status="pending", proposed_by=_PROPOSED_BY)
    assert rows == [], "legacy auto path must NOT surface a pending_write"


@pytest.mark.asyncio
async def test_human_gate_requires_mori_store_fails_closed(intake_pool):
    """Gate path with no mori_store → candidate stays pending (fail closed)."""
    _, cid = await _seed_pending_candidate(
        intake_pool, content="Idempotency keys make retries safe."
    )
    # mori_store omitted → _surface_for_human_review raises → assess_once catches
    # → candidate not transitioned.
    processed = await assess_once(
        intake_pool, assess=_unrelated_stub, mori_store=None, promotion_enabled=False
    )
    assert processed == 0
    cand = await _candidate(intake_pool, cid)
    assert cand["status"] == "pending"


@pytest.mark.asyncio
async def test_two_phase_approve_is_vote_only(intake_pool, canon_store):
    """TD approve of an agent-intake row → 'human_approved'; NO canon write."""
    content = "Backpressure protects systems from overload."
    cid, pw = await _surface(intake_pool, canon_store, content=content)

    msg = await canon_store.approve(pw["id"], reviewer="fred")
    assert "agent-intake" in msg and "finalizer" in msg

    # Row moved to the vote state; nothing in canon yet.
    voted = await canon_store.pending_list_json(status="human_approved", proposed_by=_PROPOSED_BY)
    assert len(voted) == 1 and voted[0]["id"] == pw["id"]
    assert await canon_store.get_memory(pw["name"]) is None

    cand = await _candidate(intake_pool, cid)
    assert cand["status"] == "under_review", "approve must not promote the candidate"


@pytest.mark.asyncio
async def test_finalizer_promotes_approved(intake_pool, canon_store):
    """approve → finalize_once → canon + lineage + candidate promoted + pending approved."""
    content = "Sagas coordinate distributed transactions without 2PC."
    cid, pw = await _surface(intake_pool, canon_store, content=content)
    canon_name = pw["name"]

    await canon_store.approve(pw["id"], reviewer="fred")
    finalized = await finalize_once(intake_pool, canon_store)
    assert finalized == 1

    # Canon memory written.
    mem = await canon_store.get_memory(canon_name)
    assert mem is not None and content in mem["body"]
    assert mem["type"] == "agent-intake"

    # Lineage recorded with the candidate id.
    lineage = await canon_store.get_intake_lineage(canon_name)
    assert lineage is not None and lineage["intake_candidate_id"] == cid

    # Candidate promoted + map row + pending_write terminal 'approved'.
    cand = await _candidate(intake_pool, cid)
    assert cand["status"] == "promoted" and cand["promoted_canon_name"] == canon_name
    pmap = await intake_pool.fetchrow(
        "SELECT canon_name FROM intake_promotion_map WHERE candidate_id = $1::uuid", cid
    )
    assert pmap is not None and pmap["canon_name"] == canon_name

    approved = await canon_store.pending_list_json(status="approved", proposed_by=_PROPOSED_BY)
    assert len(approved) == 1 and approved[0]["id"] == pw["id"]


@pytest.mark.asyncio
async def test_finalizer_idempotent_redrive(intake_pool, canon_store):
    """A second finalize pass after promotion is a no-op — no duplicate canon row."""
    content = "Outbox pattern guarantees at-least-once event delivery."
    cid, pw = await _surface(intake_pool, canon_store, content=content)
    await canon_store.approve(pw["id"], reviewer="fred")

    assert await finalize_once(intake_pool, canon_store) == 1
    # Re-approve a fresh vote state would be needed to re-enter; instead assert the
    # promoted row is terminal and a second pass finds nothing to finalize.
    second = await finalize_once(intake_pool, canon_store)
    assert second == 0

    async with canon_store.pool.acquire() as conn:
        n = await conn.fetchval("SELECT COUNT(*) FROM memories WHERE name = $1", pw["name"])
        lin = await conn.fetchval(
            "SELECT COUNT(*) FROM memory_intake_lineage WHERE canon_name = $1", pw["name"]
        )
    assert n == 1 and lin == 1


@pytest.mark.asyncio
async def test_finalizer_body_integrity_fail_rejects(intake_pool, canon_store):
    """Tampered live candidate body (hash mismatch) → reject, no canon write."""
    content = "Bulkheads isolate failures to a single pool."
    cid, pw = await _surface(intake_pool, canon_store, content=content)
    await canon_store.approve(pw["id"], reviewer="fred")

    # Tamper: change canonicalized_body WITHOUT updating content_hash → GOV-002b fail.
    await intake_pool.execute(
        "UPDATE intake_candidates SET canonicalized_body = $1 WHERE id = $2::uuid",
        "tampered body injected after approval",
        cid,
    )

    finalized = await finalize_once(intake_pool, canon_store)
    assert finalized == 0

    assert await canon_store.get_memory(pw["name"]) is None
    cand = await _candidate(intake_pool, cid)
    assert cand["status"] == "rejected"
    assert await _pw_status(canon_store, pw["id"]) in _DEAD


@pytest.mark.asyncio
async def test_finalizer_forged_provenance_rejected(intake_pool, canon_store):
    """A human_approved row whose provenance ticket_uuid has no ticket → rejected."""
    content = "Quorum reads trade latency for consistency."
    cid, pw = await _surface(intake_pool, canon_store, content=content)

    # Forge: overwrite provenance with a random, non-existent ticket_uuid, then
    # push to the vote state directly (bypassing approve, which is fine — we are
    # testing the finalizer's trust check).
    fake = str(uuid.uuid4())
    async with canon_store.pool.acquire() as conn:
        await conn.execute(
            "UPDATE pending_writes SET provenance = $1, status = 'human_approved' WHERE id = $2",
            f'{{"ticket_uuid": "{fake}"}}',
            pw["id"],
        )

    finalized = await finalize_once(intake_pool, canon_store)
    assert finalized == 0
    assert await canon_store.get_memory(pw["name"]) is None

    # Candidate must NOT have been promoted off a forged ticket.
    cand = await _candidate(intake_pool, cid)
    assert cand["status"] != "promoted"
    assert await _pw_status(canon_store, pw["id"]) in _DEAD


@pytest.mark.asyncio
async def test_reject_reconciles_candidate(intake_pool, canon_store):
    """TD reject → finalize reconciles the candidate back to 'rejected'."""
    content = "Read-your-writes consistency needs session affinity."
    cid, pw = await _surface(intake_pool, canon_store, content=content)

    # TD rejects the proposal (mori side only).
    await canon_store.reject(pw["id"], reviewer="fred", note="not canon-worthy")
    cand = await _candidate(intake_pool, cid)
    assert cand["status"] == "under_review", "reject alone cannot reach intake"

    # Finalizer reconciles the orphaned candidate.
    await finalize_once(intake_pool, canon_store)
    cand = await _candidate(intake_pool, cid)
    assert cand["status"] == "rejected"

    # pending_write terminalized so it is not re-scanned.
    reconciled = await canon_store.pending_list_json(
        status="rejected_reconciled", proposed_by=_PROPOSED_BY
    )
    assert any(r["id"] == pw["id"] for r in reconciled)
    assert await canon_store.get_memory(pw["name"]) is None
