"""Phase 2 step 2 — the store.write chokepoint now runs a named authorization pipeline:
``_validate_provenance`` -> ``_authorize_tier`` (may_target) -> anatomy -> persist -> audit.

This step is **AUDIT-MODE ONLY**: the tier decision is computed and logged (a would-block),
but enforcement (downgrade-to-pending) lands behind ``MORI_TIER_ENFORCE`` in step 3. Every
write here still completes as ACCEPTED — these tests lock that observe-only contract so the
step-3 flip is the *only* thing that changes behaviour.
"""

import asyncio
import os

import pytest

from mori_advisor.memory_store import MemoryStore
from mori_advisor.provenance import DREAMER, GOVERNED_PROMOTION, LEGACY, Provenance
from mori_advisor.store.migrations import MIGRATIONS, apply_sqlite
from mori_advisor.write_result import Disposition

PG_URL = os.environ.get("MORI_TEST_DATABASE_URL", "")
requires_pg = pytest.mark.skipif(not PG_URL, reason="MORI_TEST_DATABASE_URL not set")


def _store(tmp_path):
    db = tmp_path / "m.db"
    MemoryStore.bootstrap_schema(db)
    apply_sqlite(db, tuple(m for m in MIGRATIONS if m.target == "memories"))
    return MemoryStore(db)


# --- _authorize_tier: pure decision over provenance.may_target ---------------


def test_authorize_tier_permits_actor_within_caps(tmp_path):
    s = _store(tmp_path)
    ok, reason = s._authorize_tier(DREAMER, "working")
    assert ok is True
    assert reason == ""


def test_authorize_tier_blocks_dreamer_targeting_canonical(tmp_path):
    s = _store(tmp_path)
    ok, reason = s._authorize_tier(DREAMER, "canonical")
    assert ok is False
    assert "canonical" in reason
    assert "dreamer" in reason


def test_authorize_tier_blocks_request_actor_targeting_canonical(tmp_path):
    s = _store(tmp_path)
    mcp = Provenance(actor="mcp", actor_detail="dev-host", source="main:memory_write")
    ok, reason = s._authorize_tier(mcp, "canonical")
    assert ok is False


def test_authorize_tier_permits_governed_promotion_to_canonical(tmp_path):
    s = _store(tmp_path)
    ok, reason = s._authorize_tier(GOVERNED_PROMOTION, "canonical")
    assert ok is True
    assert reason == ""


# --- audit-mode: the decision is OBSERVED, never enforced (step 2) ------------


def test_unauthorized_canonical_is_queued(tmp_path):
    # v2.3.16 (the canon lock) closes the #5 seam: an mcp actor without canon authority targeting
    # canonical is stored as a proposal, in every mode — nothing reaches canon.
    s = _store(tmp_path)
    mcp = Provenance(actor="mcp", actor_detail="dev-host", source="main:memory_write")
    r = s._write(name="seam1", title="t", body="b", tier="canonical", provenance=mcp)
    assert r.disposition is Disposition.DOWNGRADED_TO_PENDING and r.pending_id
    assert s.get_memory("seam1") is None


def test_validate_provenance_legacy_does_not_block(tmp_path):
    # LEGACY (unmigrated caller) logs loud but must not change the outcome.
    s = _store(tmp_path)
    r = s._write(name="leg1", title="t", body="b", provenance=LEGACY)
    assert r.disposition is Disposition.ACCEPTED
    assert r.stored_tier == "working"


def test_authorized_write_unchanged(tmp_path):
    # The common path (dreamer -> working) is authorized and behaviour-identical.
    s = _store(tmp_path)
    r = s._write(name="ok1", title="t", body="b", tier="working", provenance=DREAMER)
    assert r.disposition is Disposition.ACCEPTED
    assert r.stored_tier == "working"


# --- Postgres parity: same observe-only contract on the async backend ---------


@requires_pg
def test_pg_unauthorized_canonical_is_queued():
    from mori_advisor.store.postgres_store import PostgresStore

    async def run():
        s = PostgresStore(PG_URL)
        await s.bootstrap()
        async with s.pool.acquire() as conn:
            await conn.execute("TRUNCATE memories, pending_writes CASCADE")
        try:
            mcp = Provenance(actor="mcp", actor_detail="dev-host", source="main:memory_write")
            r = await s._write(
                name="pg-seam1", title="t", body="b", tier="canonical", provenance=mcp
            )
            # v2.3.16: queued as a proposal; nothing reaches canon.
            assert r.disposition is Disposition.DOWNGRADED_TO_PENDING and r.pending_id
            assert await s.get_memory("pg-seam1") is None
        finally:
            if s.pool:
                await s.pool.close()

    asyncio.run(run())
