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
