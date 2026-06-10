"""Unit tests for the ingest-shape instrument (_shape_metrics). No DB/network."""

from __future__ import annotations

from mori_advisor.ingestion import _shape_metrics


def test_empty():
    assert _shape_metrics([]) == {
        "candidates_total": 0,
        "convention_ratio": 0.0,
        "anchorable_pct": 0.0,
    }


def test_convention_ratio_counts_near_dups():
    # two candidates share the 'game-state-contract' convention; one is a singleton
    cands = [
        {"name": "lineup4-game-state-contract", "body": "x"},
        {"name": "greedy-pig-game-state-contract", "body": "x"},
        {"name": "unrelated-thing-here", "body": "x"},
    ]
    s = _shape_metrics(cands)
    assert s["candidates_total"] == 3
    assert s["convention_ratio"] == round(2 / 3, 3)


def test_no_convention_when_all_distinct():
    cands = [
        {"name": "auto-discovered-game-registration", "body": "x"},
        {"name": "random-seed-reset-anti-manipulation", "body": "x"},
    ]
    assert _shape_metrics(cands)["convention_ratio"] == 0.0


def test_anchorable_pct():
    cands = [
        {"name": "a-thing", "body": "resolved under src/games/loader.py via a helper"},
        {"name": "b-thing", "body": "plain prose with no anchors at all"},
    ]
    assert _shape_metrics(cands)["anchorable_pct"] == 50.0


def test_handles_missing_name_and_body():
    cands = [{"body": "x"}, {"name": "only-name"}]
    s = _shape_metrics(cands)
    assert s["candidates_total"] == 2  # totals count all candidates
    assert s["convention_ratio"] == 0.0


# ── canon mortality (SQLite) ──────────────────────────────────────────────────


def test_canon_mortality_rate(tmp_path):
    from mori_advisor.store.sqlite_store import SQLiteStore

    store = SQLiteStore(tmp_path / "m.db")
    store.bootstrap()
    for n in ("old-unread", "old-read", "recent-unread"):
        store.write(name=n, title=n, body="x", type="project", tier="canonical")
    conn = store._mem._get_conn()
    conn.execute(
        "UPDATE memories SET created_at = datetime('now','-200 days') "
        "WHERE name IN ('old-unread','old-read')"
    )
    conn.execute("UPDATE memories SET retrieval_count = 3 WHERE name = 'old-read'")
    conn.commit()
    conn.close()
    # cohort = the two backdated canonical memories (recent excluded); 1 of 2 never read
    assert store.canon_mortality_rate(days=90) == 0.5


def test_canon_mortality_none_when_no_cohort(tmp_path):
    from mori_advisor.store.sqlite_store import SQLiteStore

    store = SQLiteStore(tmp_path / "m.db")
    store.bootstrap()
    store.write(name="fresh", title="F", body="x", type="project", tier="canonical")
    # created just now → not in the >90d cohort → None
    assert store.canon_mortality_rate(days=90) is None
