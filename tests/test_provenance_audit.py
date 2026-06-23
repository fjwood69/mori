"""Identity-aware chokepoint, Phase 1 (B+C) — the universal in-transaction audit at
store.write records WHO (actor_detail, specific) + WHAT (op, caller disposition), for
every writer, atomically. Closes the v2.2.26 per-caller audit drift.
"""

import sqlite3

from mori_advisor.memory_store import MemoryStore
from mori_advisor.provenance import DREAMER, request_provenance
from mori_advisor.store.migrations import MIGRATIONS, apply_sqlite


def _store(tmp_path):
    db = tmp_path / "memories.db"
    MemoryStore.bootstrap_schema(db)
    apply_sqlite(db, tuple(m for m in MIGRATIONS if m.target == "memories"))
    return MemoryStore(db), db


def _audit_rows(db):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT actor_key_name, op, memory_name FROM write_audit ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def test_dreamer_write_is_audited_at_chokepoint(tmp_path):
    # The dreamer was the unaudited bulk writer (v2.2.26 gap) — now audited at the door.
    st, db = _store(tmp_path)
    st.write(name="m1", body="b", tier="working", provenance=DREAMER)
    assert ("dreamer", "write", "m1") in _audit_rows(db)


def test_request_write_records_specific_actor_and_op(tmp_path):
    # WHO = the specific key (actor_detail) for the ledger; WHAT = the caller's disposition.
    st, db = _store(tmp_path)
    st.write(
        name="m2",
        body="b",
        tier="working",
        provenance=request_provenance("mcp", "dev-host", "rest.write", op="propose_new"),
    )
    assert ("dev-host", "propose_new", "m2") in _audit_rows(db)


def test_unmigrated_caller_still_audits_as_legacy(tmp_path):
    # No provenance → LEGACY default still audits (flagged loud) — no coverage gap.
    st, db = _store(tmp_path)
    st.write(name="m3", body="b", tier="working")
    assert ("legacy", "write", "m3") in _audit_rows(db)


def test_audit_is_in_the_write_transaction(tmp_path):
    # Atomic: the audit row lands on the same commit as the memory (count matches writes).
    st, db = _store(tmp_path)
    st.write(name="a", body="x", provenance=DREAMER)
    st.write(name="b", body="y", provenance=DREAMER)
    rows = _audit_rows(db)
    assert len([r for r in rows if r[2] in ("a", "b")]) == 2
