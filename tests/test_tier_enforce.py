"""Phase 2 step 3 (policy) — MORI_TIER_ENFORCE makes the tier decision ACT.

R2 (board-ratified): an unauthorized tier target is HARD-REJECTED on BOTH backends — no
downgrade-to-pending (that lane is for name/tag protection only). Default (flag unset) stays
audit-mode: would-block logged, write ACCEPTED — zero behaviour change. The flag supports a
per-actor allow-list (``enforce:mcp,rest``) for the board's per-actor flip (mcp/rest first).
"""

import asyncio
import os

import pytest

from mori_advisor.memory_store import MemoryStore
from mori_advisor.provenance import (
    DREAMER,
    GOVERNED_PROMOTION,
    Provenance,
    tier_decision,
    tier_enforce_mode,
)
from mori_advisor.store.migrations import MIGRATIONS, apply_sqlite
from mori_advisor.write_result import Disposition

PG_URL = os.environ.get("MORI_TEST_DATABASE_URL", "")
requires_pg = pytest.mark.skipif(not PG_URL, reason="MORI_TEST_DATABASE_URL not set")

MCP = Provenance(actor="mcp", actor_detail="nuc15pro", source="main:memory_write")
INGEST = Provenance(actor="ingestion", source="ingestion.py:ingest", op="ingest")


def _store(tmp_path):
    db = tmp_path / "m.db"
    MemoryStore.bootstrap_schema(db)
    apply_sqlite(db, tuple(m for m in MIGRATIONS if m.target == "memories"))
    return MemoryStore(db)


# --- tier_enforce_mode resolver ----------------------------------------------


def test_mode_default_is_audit(monkeypatch):
    monkeypatch.delenv("MORI_TIER_ENFORCE", raising=False)
    assert tier_enforce_mode("mcp") == "audit"


def test_mode_enforce_all(monkeypatch):
    monkeypatch.setenv("MORI_TIER_ENFORCE", "enforce")
    assert tier_enforce_mode("mcp") == "enforce"
    assert tier_enforce_mode("dreamer") == "enforce"


def test_mode_per_actor_allowlist(monkeypatch):
    monkeypatch.setenv("MORI_TIER_ENFORCE", "enforce:mcp,rest")
    assert tier_enforce_mode("mcp") == "enforce"
    assert tier_enforce_mode("rest") == "enforce"
    assert tier_enforce_mode("ingestion") == "audit"  # not in the allow-list → still observe


def test_mode_garbage_fails_safe_to_audit(monkeypatch):
    monkeypatch.setenv("MORI_TIER_ENFORCE", "yolo")
    assert tier_enforce_mode("mcp") == "audit"


# --- tier_decision: the snapshot the store acts on ---------------------------


def test_decision_allowed(monkeypatch):
    monkeypatch.setenv("MORI_TIER_ENFORCE", "enforce")
    reject, decision, mode, reason = tier_decision(DREAMER, "working")
    assert (reject, decision) == (False, "allowed")


def test_decision_would_block_in_audit(monkeypatch):
    monkeypatch.delenv("MORI_TIER_ENFORCE", raising=False)
    reject, decision, mode, reason = tier_decision(MCP, "canonical")
    assert (reject, decision, mode) == (False, "would_block", "audit")
    assert "canonical" in reason


def test_decision_rejected_in_enforce(monkeypatch):
    monkeypatch.setenv("MORI_TIER_ENFORCE", "enforce")
    reject, decision, mode, reason = tier_decision(MCP, "canonical")
    assert (reject, decision, mode) == (True, "rejected", "enforce")


# --- _write enforcement (SQLite) ---------------------------------------------


def test_enforce_rejects_unauthorized_canonical_and_persists_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("MORI_TIER_ENFORCE", "enforce")
    s = _store(tmp_path)
    r = s._write(name="reject-me", title="t", body="b", tier="canonical", provenance=MCP)
    assert r.disposition is Disposition.REJECTED
    assert "canonical" in r.reason
    # R2: REJECT persists nothing — the row must not exist.
    assert s.get_memory("reject-me") is None


def test_enforce_allows_authorized_write(tmp_path, monkeypatch):
    monkeypatch.setenv("MORI_TIER_ENFORCE", "enforce")
    s = _store(tmp_path)
    r = s._write(name="ok", title="t", body="b", tier="working", provenance=MCP)
    assert r.disposition is Disposition.ACCEPTED
    assert s.get_memory("ok") is not None


def test_enforce_allows_governed_promotion_to_canonical(tmp_path, monkeypatch):
    monkeypatch.setenv("MORI_TIER_ENFORCE", "enforce")
    s = _store(tmp_path)
    r = s._write(name="canon", title="t", body="b", tier="canonical", provenance=GOVERNED_PROMOTION)
    assert r.disposition is Disposition.ACCEPTED
    assert s.get_memory("canon") is not None


def test_per_actor_flip_spares_unlisted_actor(tmp_path, monkeypatch):
    # enforce:mcp → mcp rejects, but ingestion (not listed) stays audit → would-block + ACCEPTED.
    monkeypatch.setenv("MORI_TIER_ENFORCE", "enforce:mcp")
    s = _store(tmp_path)
    assert s._write(name="m", body="b", tier="canonical", provenance=MCP).disposition is (
        Disposition.REJECTED
    )
    r = s._write(name="i", body="b", tier="canonical", provenance=INGEST)
    assert r.disposition is Disposition.ACCEPTED  # observed, not enforced for ingestion yet


def test_audit_default_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("MORI_TIER_ENFORCE", raising=False)
    s = _store(tmp_path)
    r = s._write(name="seam", title="t", body="b", tier="canonical", provenance=MCP)
    assert r.disposition is Disposition.ACCEPTED  # audit-mode: observed, not enforced
    assert s.get_memory("seam") is not None


# --- Postgres parity ---------------------------------------------------------


@requires_pg
def test_pg_enforce_rejects_unauthorized_canonical(monkeypatch):
    from mori_advisor.store.postgres_store import PostgresStore

    monkeypatch.setenv("MORI_TIER_ENFORCE", "enforce")

    async def run():
        s = PostgresStore(PG_URL)
        await s.bootstrap()
        async with s.pool.acquire() as conn:
            await conn.execute("TRUNCATE memories CASCADE")
        try:
            r = await s._write(
                name="pg-reject", title="t", body="b", tier="canonical", provenance=MCP
            )
            assert r.disposition is Disposition.REJECTED
            assert await s.get_memory("pg-reject") is None
        finally:
            if s.pool:
                await s.pool.close()

    asyncio.run(run())
