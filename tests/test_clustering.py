"""Unit tests for the TD review roll-up clustering core.

Fixtures are drawn from the real layer-1 benchmark output (the games corpus
distilled by the new archivist prompt), which is where the near-dup collisions
were observed: `*-game-state-contract` across three games, `*-player-feedback-
channel` across two, and distinct cross-cutting conventions that must NOT merge.
"""

from __future__ import annotations

from mori_advisor.clustering import cluster_index, cluster_keys, roll_up


def test_cross_game_convention_collapses():
    keys = [
        "lineup4-game-state-contract",
        "greedy-pig-game-state-contract",
        "prisoners-dilemma-game-state-contract",
    ]
    m = cluster_keys(keys)
    assert len(set(m.values())) == 1
    assert set(m.values()) == {"game-state-contract"}


def test_two_game_convention_collapses():
    keys = ["lineup4-player-feedback-channel", "greedy-pig-player-feedback-channel"]
    m = cluster_keys(keys)
    assert len(set(m.values())) == 1
    assert set(m.values()) == {"player-feedback-channel"}


def test_distinct_conventions_do_not_merge():
    # All end in "-contract" but differ at >=2 trailing segments -> must stay apart.
    keys = [
        "basegame-subclass-contract",
        "reward-schema-frontend-contract",
        "lineup4-board-coordinate-contract",
        "game-frontend-manifest-contract",
    ]
    m = cluster_keys(keys)
    assert len(set(m.values())) == len(keys)  # every key is its own cluster
    for k in keys:
        assert m[k] == k


def test_singletons_are_self_keyed():
    keys = ["random-seed-reset-anti-manipulation", "user-code-exec-with-import-rewrite"]
    m = cluster_keys(keys)
    assert m == {k: k for k in keys}


def test_mixed_set_rolls_up_correctly():
    keys = [
        "lineup4-game-state-contract",
        "greedy-pig-game-state-contract",
        "prisoners-dilemma-game-state-contract",
        "lineup4-player-feedback-channel",
        "greedy-pig-player-feedback-channel",
        "basegame-subclass-contract",  # distinct singleton
        "dynamic-game-module-resolution",  # distinct singleton
    ]
    m = cluster_keys(keys)
    # 2 real conventions + 2 singletons = 4 clusters
    assert len(set(m.values())) == 4
    assert m["lineup4-game-state-contract"] == "game-state-contract"
    assert m["greedy-pig-player-feedback-channel"] == "player-feedback-channel"
    assert m["basegame-subclass-contract"] == "basegame-subclass-contract"


def test_roll_up_groups_and_sorts():
    cands = [
        {"name": "lineup4-game-state-contract"},
        {"name": "greedy-pig-game-state-contract"},
        {"name": "prisoners-dilemma-game-state-contract"},
        {"name": "basegame-subclass-contract"},
    ]
    clusters = roll_up(cands)
    # largest cluster first
    assert clusters[0]["cluster_key"] == "game-state-contract"
    assert clusters[0]["size"] == 3
    assert len(clusters[0]["members"]) == 3
    # the singleton is present as its own cluster
    keys = {c["cluster_key"] for c in clusters}
    assert "basegame-subclass-contract" in keys
    assert sum(c["size"] for c in clusters) == 4  # nothing dropped


def test_cluster_index_is_lightweight_and_multi_only():
    # member payloads must NOT be embedded — only ids — and singletons omitted.
    cands = [
        {"id": "a", "name": "lineup4-game-state-contract"},
        {"id": "b", "name": "greedy-pig-game-state-contract"},
        {"id": "c", "name": "basegame-subclass-contract"},  # singleton -> omitted
    ]
    idx = cluster_index(cands, id_field="id", name_field="name")
    assert len(idx) == 1
    assert idx[0]["cluster_key"] == "game-state-contract"
    assert idx[0]["size"] == 2
    assert sorted(idx[0]["members"]) == ["a", "b"]


def test_cluster_index_by_stable_key_intake_shape():
    # intake path: cluster on stable_key, identify members by candidate id.
    cands = [
        {"id": "1", "stable_key": "learned-feedback-channel-pattern"},
        {"id": "2", "stable_key": "fact-feedback-channel-pattern"},
        {"id": "3", "stable_key": "learned-unrelated-thing"},
    ]
    idx = cluster_index(cands, id_field="id", name_field="stable_key")
    assert len(idx) == 1
    assert idx[0]["cluster_key"] == "feedback-channel-pattern"
    assert sorted(idx[0]["members"]) == ["1", "2"]


def test_roll_up_falls_back_to_path_last_segment():
    cands = [
        {"path": "project/games/lineup4/game-state-contract"},
        {"path": "project/games/greedy-pig/game-state-contract"},
    ]
    clusters = roll_up(cands)
    assert len(clusters) == 1
    assert clusters[0]["cluster_key"] == "game-state-contract"
    assert clusters[0]["size"] == 2
