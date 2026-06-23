"""WriteResult — the structured chokepoint outcome (Phase 2 step 1).

Locks the backward-compat contract: ACCEPTED -> require_accepted() returns the name;
non-ACCEPTED -> raises TierDowngradedError carrying the result; disposition survives
serialization (not a str-subclass that drops it).
"""

import json

import pytest

from mori_advisor.write_result import (
    Disposition,
    TierDowngradedError,
    WriteResult,
    accepted,
)


def test_accepted_require_returns_name():
    r = accepted("conv-x", "working", audit_id=7)
    assert r.accepted
    assert r.require_accepted() == "conv-x"
    assert r.stored_tier == "working"


def test_downgraded_require_raises_with_result():
    r = WriteResult(
        memory_name="canon-y",
        intended_tier="canonical",
        stored_tier="pending",
        disposition=Disposition.DOWNGRADED_TO_PENDING,
        pending_id=42,
        reason="actor 'mcp' may not target canonical",
    )
    assert not r.accepted
    with pytest.raises(TierDowngradedError) as ei:
        r.require_accepted()
    # the exception carries the full result — no string parsing needed
    assert ei.value.result is r
    assert ei.value.result.pending_id == 42
    assert "canonical" in str(ei.value)


def test_rejected_is_not_accepted():
    r = WriteResult("m", "canonical", "", Disposition.REJECTED, reason="missing provenance")
    assert not r.accepted
    with pytest.raises(TierDowngradedError):
        r.require_accepted()


def test_disposition_survives_serialization():
    # the str-subclass shim the board rejected would drop `disposition` here.
    r = WriteResult("m", "canonical", "pending", Disposition.DOWNGRADED_TO_PENDING, pending_id=1)
    d = r.to_dict()
    assert d["disposition"] == "downgraded_to_pending"
    round_tripped = json.loads(json.dumps(d))
    assert round_tripped["disposition"] == "downgraded_to_pending"
    assert round_tripped["pending_id"] == 1


def test_disposition_is_str_enum_for_comparisons():
    assert Disposition.ACCEPTED == "accepted"
    assert accepted("m", "working").disposition == "accepted"


def test_bool_is_true_only_when_accepted():
    # GLM bool-coercion: a handler that does `if result:` must NOT treat a downgrade/reject
    # as success. ACCEPTED is truthy; everything else is falsy.
    assert bool(accepted("m", "working")) is True
    downgraded = WriteResult(
        "m", "canonical", "pending", Disposition.DOWNGRADED_TO_PENDING, pending_id=1
    )
    rejected = WriteResult("m", "canonical", "", Disposition.REJECTED, reason="nope")
    assert bool(downgraded) is False
    assert bool(rejected) is False
