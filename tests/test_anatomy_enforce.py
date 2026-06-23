"""Phase 2 step 6 (last) — MORI_ANATOMY_ENFORCE: the completeness verdict ACTS, and the
``_skip_protection`` trapdoor closes, both behind ONE separate flag (board: decoupled from tier).

Default (unset) = audit: an incomplete write is logged + counted but ACCEPTED, and
``_skip_protection`` still bypasses protection — zero behaviour change. In enforce mode a failed
anatomy verdict is DOWNGRADED_TO_PENDING (board-chosen over hard-reject), and ``_skip_protection``
is ignored so the dreamer's protected overwrites route to review too.

Anatomy rules (validate_anatomy): empty-body | empty-warrant (warrant=description, needs >=10 chars)
| unwarranted-directive. Most writes pass description="" -> empty-warrant, which is exactly why the
audit soak must precede the flip.
"""

import asyncio
import os

import pytest

from mori_advisor.memory_store import MemoryStore
from mori_advisor.provenance import DREAMER, Provenance, anatomy_enforce_mode
from mori_advisor.store.migrations import MIGRATIONS, apply_sqlite
from mori_advisor.write_result import Disposition

PG_URL = os.environ.get("MORI_TEST_DATABASE_URL", "")
requires_pg = pytest.mark.skipif(not PG_URL, reason="MORI_TEST_DATABASE_URL not set")

MCP = Provenance(actor="mcp", actor_detail="dev-host", source="main:memory_write")
WARRANT = "a sufficiently long warrant to pass the anatomy check"  # >= 10 chars


def _store(tmp_path):
    db = tmp_path / "m.db"
    MemoryStore.bootstrap_schema(db)
    apply_sqlite(db, tuple(m for m in MIGRATIONS if m.target == "memories"))
    return MemoryStore(db)


# --- anatomy_enforce_mode resolver (separate flag from tier) ------------------


def test_mode_default_audit(monkeypatch):
    monkeypatch.delenv("MORI_ANATOMY_ENFORCE", raising=False)
    assert anatomy_enforce_mode("dreamer") == "audit"


def test_mode_enforce_and_per_actor(monkeypatch):
    monkeypatch.setenv("MORI_ANATOMY_ENFORCE", "enforce")
    assert anatomy_enforce_mode("dreamer") == "enforce"
    monkeypatch.setenv("MORI_ANATOMY_ENFORCE", "enforce:dreamer")
    assert anatomy_enforce_mode("dreamer") == "enforce"
    assert anatomy_enforce_mode("mcp") == "audit"


def test_anatomy_flag_independent_of_tier(monkeypatch):
    # The two flags are separate — enabling tier enforcement must NOT enable anatomy.
    monkeypatch.setenv("MORI_TIER_ENFORCE", "enforce")
    monkeypatch.delenv("MORI_ANATOMY_ENFORCE", raising=False)
    assert anatomy_enforce_mode("dreamer") == "audit"


# --- completeness enforcement ------------------------------------------------


def test_audit_default_incomplete_write_accepted(tmp_path, monkeypatch):
    monkeypatch.delenv("MORI_ANATOMY_ENFORCE", raising=False)
    s = _store(tmp_path)
    # description="" -> empty-warrant -> invalid, but audit-mode accepts.
    r = s._write(name="inc", title="t", body="b", description="", provenance=DREAMER)
    assert r.disposition is Disposition.ACCEPTED
    assert s.get_memory("inc") is not None


def test_enforce_incomplete_write_downgraded_to_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("MORI_ANATOMY_ENFORCE", "enforce")
    s = _store(tmp_path)
    r = s._write(name="inc2", title="t", body="b", description="", provenance=DREAMER)
    assert r.disposition is Disposition.DOWNGRADED_TO_PENDING
    assert "anatomy" in r.reason.lower() or "warrant" in r.reason.lower()
    # downgrade routes to review — the memory itself is NOT persisted.
    assert s.get_memory("inc2") is None


def test_enforce_complete_write_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("MORI_ANATOMY_ENFORCE", "enforce")
    s = _store(tmp_path)
    r = s._write(name="ok", title="t", body="b", description=WARRANT, provenance=DREAMER)
    assert r.disposition is Disposition.ACCEPTED
    assert s.get_memory("ok") is not None


def test_enforce_per_actor_spares_unlisted(tmp_path, monkeypatch):
    monkeypatch.setenv("MORI_ANATOMY_ENFORCE", "enforce:dreamer")
    s = _store(tmp_path)
    # dreamer incomplete -> downgraded; mcp incomplete -> still audit -> accepted.
    assert s._write(name="d", body="b", description="", provenance=DREAMER).disposition is (
        Disposition.DOWNGRADED_TO_PENDING
    )
    assert s._write(name="m", body="b", description="", provenance=MCP).disposition is (
        Disposition.ACCEPTED
    )


# --- _skip_protection gating -------------------------------------------------


def test_skip_protection_ignored_under_enforce(tmp_path, monkeypatch):
    # A protected existing memory + an untrusted writer using _skip_protection=True:
    #  - audit mode  : skip honoured -> overwrite (ACCEPTED)
    #  - enforce mode : skip ignored -> protection applies -> DOWNGRADED_TO_PENDING
    s = _store(tmp_path)
    s._write(name="locked", title="t", body="b", description=WARRANT, provenance=DREAMER)
    conn = s._get_conn()
    conn.execute("UPDATE memories SET protected = 1 WHERE name = 'locked'")
    conn.commit()
    conn.close()

    monkeypatch.delenv("MORI_ANATOMY_ENFORCE", raising=False)
    r_audit = s._write(
        name="locked",
        title="t2",
        body="b2",
        description=WARRANT,
        provenance=MCP,
        client="rando",
        _skip_protection=True,
    )
    assert r_audit.disposition is Disposition.ACCEPTED  # trapdoor open in audit

    monkeypatch.setenv("MORI_ANATOMY_ENFORCE", "enforce")
    r_enforce = s._write(
        name="locked",
        title="t3",
        body="b3",
        description=WARRANT,
        provenance=MCP,
        client="rando",
        _skip_protection=True,
    )
    assert r_enforce.disposition is Disposition.DOWNGRADED_TO_PENDING  # trapdoor closed


# --- Postgres parity (completeness enforce) ----------------------------------


@requires_pg
def test_pg_enforce_incomplete_downgraded_to_pending(monkeypatch):
    from mori_advisor.store.postgres_store import PostgresStore

    monkeypatch.setenv("MORI_ANATOMY_ENFORCE", "enforce")

    async def run():
        s = PostgresStore(PG_URL)
        await s.bootstrap()
        async with s.pool.acquire() as conn:
            await conn.execute("TRUNCATE memories, pending_writes CASCADE")
        try:
            r = await s._write(
                name="pg-inc", title="t", body="b", description="", provenance=DREAMER
            )
            assert r.disposition is Disposition.DOWNGRADED_TO_PENDING
            assert await s.get_memory("pg-inc") is None  # not persisted as a memory
        finally:
            if s.pool:
                await s.pool.close()

    asyncio.run(run())
