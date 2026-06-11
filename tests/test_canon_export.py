"""Unit tests for the structured canon export (canon_export + store.export_rows)."""

from __future__ import annotations

import json
import re

from mori_advisor import canon_export

SAMPLE = [
    {
        "name": "high-use-decision",
        "title": "High use",
        "type": "decision",
        "tier": "canonical",
        "tags": ["architecture"],
        "body": "use X here",
        "retrieval_count": 12,
        "confidence": 0.9,
        # provenance / PII — must NOT egress by default:
        "origin_clients": ["secret-internal-host"],
        "origin_session_ids": ["sess-abc-123"],
    },
    {
        "name": "low-use-decision",
        "title": "Low use",
        "type": "decision",
        "tier": "canonical",
        "tags": ["architecture"],
        "body": "use Y here",
        "retrieval_count": 3,
    },
    {
        "name": "a-pattern",
        "title": "A pattern",
        "type": "pattern",
        "tier": "canonical",
        "tags": ["patterns"],
        "body": "do Z",
        "retrieval_count": 7,
    },
    {
        "name": "untyped-misc",
        "title": "Misc",
        "type": "project",
        "tier": "canonical",
        "tags": [],
        "body": "misc note",
        "retrieval_count": 1,
    },
]


def test_standard_groups_and_sorts_by_retrieval():
    out = canon_export.build(SAMPLE, fmt="standard", meta={"version": "test", "instance": "h"})
    assert "# Mori Canon Export" in out
    assert "Version: test" in out  # pin header present
    assert "## Architecture & Decisions" in out
    # within a group, most-retrieved first
    assert out.index("high-use-decision") < out.index("low-use-decision")
    # project-typed row with no group tag falls through to Uncategorized
    assert "## Uncategorized" in out


def test_provenance_stripped_by_default():
    out = canon_export.build(SAMPLE, fmt="json", meta={})
    assert "secret-internal-host" not in out
    assert "sess-abc-123" not in out
    # ...but available when explicitly requested
    out2 = canon_export.build(SAMPLE, fmt="json", meta={}, include_provenance=True)
    assert "secret-internal-host" in out2


def test_json_shape():
    out = json.loads(canon_export.build(SAMPLE, fmt="json", meta={"version": "v"}))
    assert out["meta"]["version"] == "v"
    assert out["meta"]["canon_size"] == len(SAMPLE)
    assert {m["name"] for m in out["memories"]} == {r["name"] for r in SAMPLE}


def test_consult_carries_rubric_and_td_framing():
    out = canon_export.build(SAMPLE, fmt="consult", meta={})
    assert "Canon coherence review" in out
    assert "Trusted-Dreamer" in out
    assert "## Memories to review" in out


def test_rubric_avoids_truth_scoring():
    """The coherence rubric must never ask a reviewer to judge reality it cannot see."""
    banned = {
        "true",
        "valid",
        "correct",
        "accurate",
        "right",
        "wrong",
        "false",
        "stale",
        "outdated",
    }
    words = set(re.findall(r"[a-z]+", canon_export.COHERENCE_RUBRIC.lower()))
    leaked = words & banned
    assert not leaked, f"truth-scoring words leaked into the coherence rubric: {leaked}"


def test_project_filter():
    rows = [
        {
            "name": "m1",
            "type": "decision",
            "tier": "canonical",
            "tags": ["project:mori"],
            "body": "a",
        },
        {
            "name": "b1",
            "type": "decision",
            "tier": "canonical",
            "tags": ["project:bifrost"],
            "body": "b",
        },
    ]
    out = canon_export.build(rows, fmt="json", meta={}, project="mori")
    names = {m["name"] for m in json.loads(out)["memories"]}
    assert names == {"m1"}


def test_sanitise_normalises_string_tags():
    row = {"name": "x", "tags": '["a", "b"]', "body": "z"}
    assert canon_export.sanitise(row)["tags"] == ["a", "b"]
    row2 = {"name": "y", "tags": None, "body": "z"}
    assert canon_export.sanitise(row2)["tags"] == []


# ── schema-contract test: real sqlite store, NULL type / empty tags / timestamps ──


def test_export_rows_schema_contract(tmp_path):
    from mori_advisor.store.sqlite_store import SQLiteStore

    store = SQLiteStore(tmp_path / "m.db")
    store.bootstrap()
    store.write(
        name="a-decision",
        title="A",
        body="x",
        type="decision",
        tier="canonical",
        tags=["architecture"],
    )
    store.write(name="b-untyped", title="B", body="y", tier="canonical")  # default type, no tags
    store.write(name="c-working", title="C", body="z", tier="working")  # excluded by tier filter

    rows = store._mem.export_rows(tiers=("canonical",), limit=50)
    names = {r["name"] for r in rows}
    assert "a-decision" in names and "b-untyped" in names
    assert "c-working" not in names
    for r in rows:
        assert isinstance(r.get("tags"), list)  # normalised even when empty/absent
        assert "created_at" in r  # timestamp present (serialisation handled downstream)

    # type filter works
    only_dec = store._mem.export_rows(tiers=("canonical",), type_filter="decision", limit=50)
    assert {r["name"] for r in only_dec} == {"a-decision"}
