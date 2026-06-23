"""Phase 2 step 1b — the store.write chokepoint now returns a WriteResult internally
(_write), with a behaviour-preserving message adapter (write()). No enforcement yet:
every write is ACCEPTED.
"""

from mori_advisor.memory_store import MemoryStore
from mori_advisor.store.migrations import MIGRATIONS, apply_sqlite
from mori_advisor.write_result import Disposition


def _store(tmp_path):
    db = tmp_path / "m.db"
    MemoryStore.bootstrap_schema(db)
    apply_sqlite(db, tuple(m for m in MIGRATIONS if m.target == "memories"))
    return MemoryStore(db)


def test_write_internal_returns_writeresult_accepted(tmp_path):
    s = _store(tmp_path)
    r = s._write(name="m1", title="t", body="b", tier="working")
    assert r.disposition is Disposition.ACCEPTED
    assert r.memory_name == "m1"
    assert r.stored_tier == "working"
    assert r.require_accepted() == "m1"


def test_write_adapter_preserves_legacy_message(tmp_path):
    # The public write() must return the exact same status string as before the split.
    s = _store(tmp_path)
    assert s.write(name="m2", title="t", body="b") == "Memory 'm2' written."
