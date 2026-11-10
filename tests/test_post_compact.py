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
    from mori_advisor.canon_authority import CanonAuthority
    from mori_advisor.provenance import SYSTEM

    # Canonical: v2.3.18's global lane admits canonical rows only; these tests are about scope.
    s._write(
        name="glob",
        title="Global",
        description="d",
        body="z",
        type="pattern",
        tier="canonical",
        tags=["scope:global"],
        provenance=SYSTEM,
        _canon_authority=CanonAuthority("test-seed"),
    )
    s.write(
        name="other", title="Other", body="w", type="project", tier="working", tags=["project:zzz"]
    )


def _member(rows, project, strict=True):
    from mori_advisor.resolver import brief_membership

    return {m["name"] for m in rows if brief_membership(m, project, strict_global=strict)}


# B1 (2026-11-01): the store returns the WINDOW only; membership is the brief's rule (resolver.brief_membership),
# applied by the caller. These replace the get_memories_changed_since tests, whose unscoped case
# ("returns_all") encoded the post-compact leak as the specification.


def test_changed_window_returns_every_live_change(store):
    _seed(store)
    names = {m["name"] for m in store.get_memories_changed_window("1h")}
    assert names == {"demo-a", "demo-b", "glob", "other"}  # window only: membership is the caller's


def test_changed_window_future_returns_nothing(store):
    _seed(store)
    assert store.get_memories_changed_window("2099-01-01T00:00:00Z") == []


def test_changed_window_unparseable_returns_empty(store):
    _seed(store)
    assert store.get_memories_changed_window("not-a-date") == []


def test_membership_project_safe_is_project_plus_explicit_global(store):
    _seed(store)
    assert _member(store.get_memories_changed_window("1h"), "demo") == {"demo-a", "demo-b", "glob"}


def test_membership_unscoped_safe_is_the_global_lane_only(store):
    _seed(store)
    assert _member(store.get_memories_changed_window("1h"), None) == {
        "glob"
    }  # was "returns all": the leak


def test_changed_window_excludes_superseded_but_scope_rows_include_them(store):
    _seed(store)
    conn = store._get_conn()
    conn.execute("UPDATE memories SET superseded_by = 'x' WHERE name = 'demo-a'")
    conn.commit()
    conn.close()
    assert "demo-a" not in {m["name"] for m in store.get_memories_changed_window("1h")}
    assert store.get_scope_rows(["demo-a"])["demo-a"]["tags"] == ["project:demo"]


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
