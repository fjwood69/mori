"""Tests for the post-compact delta brief (`/brief --post-compact`).

Covers the `since` normaliser, the `get_memories_changed_since` store method
(boundary + scoping), and the `brief(post_compact=True)` control flow — in
particular the guarantee that the lean path never runs the expensive per-memory
`check_freshness` LLM scan.
"""

import asyncio

import pytest

from mori_advisor.memory_store import MemoryStore, normalise_since
from mori_advisor.store.migrations import MIGRATIONS, apply_sqlite
from mori_advisor.store.sqlite_store import SQLiteStore

# ── normalise_since ────────────────────────────────────────────────────────


def test_normalise_since_relative_shape():
    out = normalise_since("6h")
    assert len(out) == 19
    assert out[4] == "-" and out[7] == "-" and out[10] == " "


def test_normalise_since_iso_t_and_z():
    # 'T' separator + 'Z' zulu → stored space-separated UTC form
    assert normalise_since("2026-06-04T12:34:56Z") == "2026-06-04 12:34:56"


def test_normalise_since_iso_offset_converts_to_utc():
    # +01:00 local → 11:34:56 UTC
    assert normalise_since("2026-06-04T12:34:56+01:00") == "2026-06-04 11:34:56"


def test_normalise_since_already_spaced_passthrough():
    assert normalise_since("2026-06-04 12:34:56") == "2026-06-04 12:34:56"


def test_normalise_since_empty_raises():
    with pytest.raises(ValueError):
        normalise_since("")


# ── get_memories_changed_since ─────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "memories.db"
    MemoryStore.bootstrap_schema(db)
    # Apply migrations so the store has the current schema (deleted_at etc.) —
    # bootstrap_schema is only the frozen baseline; real stores are also migrated.
    apply_sqlite(db, tuple(m for m in MIGRATIONS if m.target == "memories"))
    return MemoryStore(db)


def _seed(s):
    s.write(
        name="demo-a",
        title="Demo A",
        body="x",
        type="project",
        tier="working",
        tags=["project:demo"],
    )
    s.write(
        name="demo-b",
        title="Demo B",
        body="y",
        type="decision",
        tier="working",
        tags=["project:demo"],
    )
    s.write(name="glob", title="Global", body="z", type="pattern", tags=["scope:global"])
    s.write(
        name="other", title="Other", body="w", type="project", tier="working", tags=["project:zzz"]
    )


def test_changed_since_past_returns_project_plus_global(store):
    _seed(store)
    names = {m["name"] for m in store.get_memories_changed_since("1h", project="demo")}
    assert names == {"demo-a", "demo-b", "glob"}  # excludes other-project


def test_changed_since_future_returns_nothing(store):
    _seed(store)
    assert store.get_memories_changed_since("2099-01-01T00:00:00Z", project="demo") == []


def test_changed_since_excludes_global_when_disabled(store):
    _seed(store)
    names = {
        m["name"]
        for m in store.get_memories_changed_since("1h", project="demo", include_global=False)
    }
    assert names == {"demo-a", "demo-b"}


def test_changed_since_unscoped_returns_all(store):
    _seed(store)
    names = {m["name"] for m in store.get_memories_changed_since("1h")}
    assert names == {"demo-a", "demo-b", "glob", "other"}


def test_changed_since_respects_limit(store):
    for i in range(5):
        store.write(
            name=f"m{i}",
            title=f"M{i}",
            body="b",
            type="project",
            tier="working",
            tags=["project:demo"],
        )
    assert len(store.get_memories_changed_since("1h", project="demo", limit=3)) == 3


def test_changed_since_unparseable_returns_empty(store):
    _seed(store)
    assert store.get_memories_changed_since("not-a-date", project="demo") == []


# ── brief(post_compact=True) ───────────────────────────────────────────────


def _wired_store(tmp_path, monkeypatch):
    import mori_advisor.main as m

    s = SQLiteStore(tmp_path / "memories.db", msg_db_path=tmp_path / "msg.db")
    s.bootstrap()
    monkeypatch.setattr(m, "store", s)
    monkeypatch.setattr(m, "memory_store", s._mem)
    return m, s


def test_brief_post_compact_skips_freshness(tmp_path, monkeypatch):
    m, s = _wired_store(tmp_path, monkeypatch)
    s.write(
        name="demo-a",
        title="Demo A",
        body="x",
        type="decision",
        tier="working",
        tags=["project:demo"],
    )

    def _boom(*a, **k):
        raise AssertionError("check_freshness must not run on the post-compact path")

    monkeypatch.setattr(s._mem, "check_freshness", _boom)

    out = asyncio.run(m.brief(post_compact=True, project="demo", since="1h"))
    assert "post-compact delta" in out
    assert "Freshness check" not in out
    assert "demo-a" in out


def test_brief_post_compact_reports_superseded(tmp_path, monkeypatch):
    m, s = _wired_store(tmp_path, monkeypatch)
    s.write(
        name="old-decision",
        title="Old decision",
        body="x",
        type="decision",
        tier="working",
        tags=["project:demo"],
    )
    conn = s._mem._get_conn()
    conn.execute(
        "UPDATE memories SET superseded_by = ?, updated_at = datetime('now') WHERE name = ?",
        ("new-decision", "old-decision"),
    )
    conn.commit()
    conn.close()

    out = asyncio.run(m.brief(post_compact=True, project="demo", since="1h"))
    assert "Superseded since" in out
    assert "old-decision" in out
    # superseded memories must NOT also appear under "Changed memories"
    assert "Changed memories" not in out


def test_brief_post_compact_nothing_changed(tmp_path, monkeypatch):
    m, s = _wired_store(tmp_path, monkeypatch)
    out = asyncio.run(m.brief(post_compact=True, project="demo", since="1h"))
    assert "Nothing changed in shared state" in out
