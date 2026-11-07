"""MORI_TIER_ENFORCE is retired (v2.3.16, the canon lock — ruling answer 7).

The tier gate it switched judged only the REQUESTED tier, so it could not stop a ``working``
write from replacing a canonical body. The canon lock replaces it: a change to canon without
canon authority is stored as a proposal, whatever the flag says. Set, the flag logs one
deprecation line at startup and is otherwise ignored.
"""

import asyncio
import logging
import os

import pytest

from mori_advisor.memory_store import MemoryStore
from mori_advisor.provenance import Provenance
from mori_advisor.store.migrations import MIGRATIONS, apply_sqlite
from mori_advisor.write_result import Disposition

PG_URL = os.environ.get("MORI_TEST_DATABASE_URL", "")
requires_pg = pytest.mark.skipif(not PG_URL, reason="MORI_TEST_DATABASE_URL not set")

MCP = Provenance(actor="mcp", actor_detail="dev-host", source="main:memory_write")


def _store(tmp_path):
    db = tmp_path / "m.db"
    MemoryStore.bootstrap_schema(db)
    apply_sqlite(db, tuple(m for m in MIGRATIONS if m.target == "memories"))
    return MemoryStore(db)


@pytest.mark.parametrize("flag", ["", "audit", "enforce", "enforce:mcp"])
def test_any_flag_value_queues_a_canonical_write(tmp_path, monkeypatch, flag):
    if flag:
        monkeypatch.setenv("MORI_TIER_ENFORCE", flag)
    else:
        monkeypatch.delenv("MORI_TIER_ENFORCE", raising=False)
    s = _store(tmp_path)
    r = s._write(name="seam", title="t", body="b", tier="canonical", provenance=MCP)
    assert r.disposition is Disposition.DOWNGRADED_TO_PENDING and r.pending_id
    assert s.get_memory("seam") is None


def test_working_writes_apply_whatever_the_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("MORI_TIER_ENFORCE", "enforce")
    s = _store(tmp_path)
    assert s._write(name="ok", title="t", body="b", tier="working", provenance=MCP).accepted


def test_flag_set_logs_one_deprecation(monkeypatch, caplog):
    from mori_advisor.main import _log_retired_settings

    monkeypatch.setenv("MORI_TIER_ENFORCE", "enforce")
    monkeypatch.delenv("MORI_SUPERSESSION_MODE", raising=False)
    with caplog.at_level(logging.WARNING):
        _log_retired_settings()
    lines = [r.getMessage() for r in caplog.records if "MORI_TIER_ENFORCE" in r.getMessage()]
    assert len(lines) == 1 and "DEPRECATED" in lines[0]


def test_flag_unset_logs_nothing(monkeypatch, caplog):
    from mori_advisor.main import _log_retired_settings

    monkeypatch.delenv("MORI_TIER_ENFORCE", raising=False)
    monkeypatch.delenv("MORI_SUPERSESSION_MODE", raising=False)
    with caplog.at_level(logging.WARNING):
        _log_retired_settings()
    assert not [r for r in caplog.records if "DEPRECATED" in r.getMessage()]


@requires_pg
def test_pg_enforce_flag_still_queues(monkeypatch):
    from mori_advisor.store.postgres_store import PostgresStore

    monkeypatch.setenv("MORI_TIER_ENFORCE", "enforce")

    async def run():
        s = PostgresStore(PG_URL)
        await s.bootstrap()
        async with s.pool.acquire() as conn:
            await conn.execute("TRUNCATE memories, pending_writes CASCADE")
        try:
            r = await s._write(name="seam", title="t", body="b", tier="canonical", provenance=MCP)
            assert r.disposition is Disposition.DOWNGRADED_TO_PENDING and r.pending_id
            assert await s.get_memory("seam") is None
        finally:
            await s.pool.close()

    asyncio.run(run())
