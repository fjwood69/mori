"""Phase 2 step 3 / E.0 — the REST write handlers now derive their response from
``WriteResult.disposition`` (not an assumed "created"). In audit-mode every write is
ACCEPTED so the response is byte-identical to before; these tests prove the handler ALSO
maps a DOWNGRADED/REJECTED result correctly, so the step-3 enforce flip needs no handler
change. The mapping is the inherited E.0 sub-task (GLM#5 + Fab serialization-boundary check).
"""

import json

from mori_advisor.main import _write_response
from mori_advisor.write_result import Disposition, WriteResult, accepted


def _body(resp):
    return json.loads(bytes(resp.body))


def test_accepted_maps_to_ok_status_and_code():
    r = accepted("m1", "working")
    resp = _write_response(r, ok_status="created", ok_code=201, name="m1")
    assert resp.status_code == 201
    assert _body(resp)["status"] == "created"
    assert _body(resp)["name"] == "m1"


def test_accepted_update_uses_caller_supplied_status_code():
    r = accepted("m2", "working")
    resp = _write_response(r, ok_status="updated", ok_code=200, name="m2")
    assert resp.status_code == 200
    assert _body(resp)["status"] == "updated"


def test_downgraded_maps_to_pending_202_with_pending_id():
    r = WriteResult(
        "canon-x",
        "canonical",
        "pending",
        Disposition.DOWNGRADED_TO_PENDING,
        pending_id=42,
        reason="actor 'rest' may not target tier 'canonical'",
    )
    resp = _write_response(r, ok_status="created", ok_code=201, name="canon-x")
    assert resp.status_code == 202
    body = _body(resp)
    assert body["status"] == "pending"
    assert body["pending_id"] == 42
    assert "canonical" in body["detail"]


def test_rejected_maps_to_422_with_error():
    r = WriteResult("m3", "canonical", "", Disposition.REJECTED, reason="missing provenance")
    resp = _write_response(r, ok_status="created", ok_code=201, name="m3")
    assert resp.status_code == 422
    assert _body(resp)["error"] == "missing provenance"
