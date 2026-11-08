"""v2.3.17 — the doors report what the store did (items 1–3 of the ruled spec; design §1–§3).

Item 1: DELETE and restore answer from a STRUCTURED outcome, never by matching strings: a queued
canonical delete is 202 pending (with the proposal's own operation and proposer), a refused one 409,
a refused canonical restore 409 canon-authority, a database error 500. Soft delete and restore need
only `write`; hard delete stays `dreamer`. Host mode is pinned to the v2.3.16 erratum.
Item 2: the canaries — `cd-canary-*` names are reserved for canon authority; a protected canary is
still deleted by an approval made by mistake (the fail-closed sequence); the review view flags it.
Item 3: a rejection needs a reason, enforced at the store; REST 422, MCP refusal, no audit row.
"""

from __future__ import annotations

import inspect
import json
import uuid

import pytest

from tests.test_canon_lock import (
    DREAM_KEY,
    OTHER_KEY,
    READ_KEY,
    WRITE_KEY,
    _active,
    _audit,
    _ms,
    _pending,
    _req,
    _rows,
    _seed_canon,
    _seed_working,
    as_key,
)
from tests.test_mcp_tools import BACKENDS, _run_with_backend


async def _a(x):
    return await x if inspect.isawaitable(x) else x


@pytest.fixture
def api_mode(monkeypatch):
    """api mode with the shared test keys (as tests/test_canon_lock.py's fixture)."""
    import mori_advisor.policy as pol

    monkeypatch.setattr(pol, "_TD_MODE", "api")
    monkeypatch.setattr(
        pol,
        "_ROLES",
        {WRITE_KEY: "write", OTHER_KEY: "write", DREAM_KEY: "dreamer", READ_KEY: "read"},
    )
    return pol


def _request(method: str, name: str, body: dict | None = None, query: str = ""):
    """A Starlette request for the per-memory REST routes (method, path param, query, JSON body)."""
    from starlette.datastructures import State
    from starlette.requests import Request

    from mori_advisor.policy import current_actor

    raw = json.dumps(body).encode() if body is not None else b""
    scope = {
        "type": "http",
        "method": method,
        "path": f"/api/memories/{name}",
        "query_string": query.encode(),
        "headers": [(b"content-type", b"application/json")],
        "path_params": {"name": name},
    }

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    req = Request(scope, receive)
    req.scope["state"] = {}
    req._state = State()
    req.state.actor = current_actor.get()
    return req


def _uniq(prefix: str) -> str:
    return (
        f"{prefix}-{uuid.uuid4().hex[:8]}"  # write_audit and pending rows persist across PG tests
    )


async def _delete(name: str, key: str, hard: bool = False):
    from mori_advisor.main import delete_memory_rest

    with as_key(key):
        r = await delete_memory_rest(_request("DELETE", name, query="hard=true" if hard else ""))
    return r.status_code, json.loads(r.body)


async def _restore(name: str, key: str):
    from mori_advisor.main import restore_memory_rest

    with as_key(key):
        r = await restore_memory_rest(_request("POST", name, body={}))
    return r.status_code, json.loads(r.body)


async def _tombstone(store, name: str, canonical: bool):
    """A soft-deleted row of *name*, made with canon authority (as Fred's review key would)."""
    from mori_advisor.canon_authority import CanonAuthority
    from mori_advisor.provenance import SYSTEM

    if canonical:
        await _seed_canon(store, name)
    else:  # with authority: a cd-canary-* name is reserved even at the working tier
        r = await _a(
            _ms(store)._write(
                name=name,
                title=f"{name} title",
                tier="working",
                body="working body",
                provenance=SYSTEM,
                _canon_authority=CanonAuthority("t"),
            )
        )
        assert r.accepted, r
    await _a(_ms(store).soft_delete(name, actor="test", _canon_authority=CanonAuthority("t")))
    assert await _active(store, name) is None


# ── item 1: DELETE ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_write_key_canonical_delete_is_202_pending_with_the_rows_own_proposer(
    backend, tmp_path, monkeypatch, api_mode
):
    name = _uniq("canon-del")

    async def run(store):
        await _seed_canon(store, name, body="keep me")
        code, body = await _delete(name, WRITE_KEY)
        assert code == 202, body
        assert body["status"] == "pending" and body["name"] == name
        assert isinstance(body["pending_id"], int) and not isinstance(body["pending_id"], bool)
        assert body["operation"] == "delete" and body["proposed_by"] == WRITE_KEY
        assert body.get("detail")
        assert (await _active(store, name))["body"] == "keep me"
        # again: the SAME open id (a content-equal re-proposal is a no-op), still one open row
        code2, body2 = await _delete(name, WRITE_KEY)
        assert code2 == 202 and body2["pending_id"] == body["pending_id"]
        assert len([p for p in await _pending(store, name)]) == 1

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_another_keys_open_delete_makes_it_409_refused_never_202(
    backend, tmp_path, monkeypatch, api_mode
):
    name = _uniq("canon-del2")

    async def run(store):
        await _seed_canon(store, name)
        c1, b1 = await _delete(name, OTHER_KEY)
        assert c1 == 202
        code, body = await _delete(name, WRITE_KEY)
        assert code == 409, body
        assert body["status"] == "refused" and body["pending_id"] == b1["pending_id"]
        assert "proposed_by" not in body  # the 409 never names another key
        assert [p["proposed_by"] for p in await _pending(store, name)] == [OTHER_KEY]

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_cap_refuses_with_409_and_is_counted_per_proposer(
    backend, tmp_path, monkeypatch, api_mode
):
    """P1-3 (disproved, test kept): another proposer's open proposals don't use this key's cap."""
    from mori_advisor.canon_authority import REQUEST_KEY_OPEN_CAP

    tag = uuid.uuid4().hex[:6]

    async def run(store):
        for i in range(REQUEST_KEY_OPEN_CAP):  # OTHER_KEY fills ITS cap
            n = f"cap-other-{tag}-{i}"
            await _seed_canon(store, n)
            assert (await _delete(n, OTHER_KEY))[0] == 202
        mine = f"cap-mine-{tag}"
        await _seed_canon(store, mine)
        code, body = await _delete(mine, WRITE_KEY)  # WRITE_KEY's cap is untouched
        assert code == 202, body
        for i in range(REQUEST_KEY_OPEN_CAP - 1):  # now fill WRITE_KEY's
            n = f"cap-w-{tag}-{i}"
            await _seed_canon(store, n)
            assert (await _delete(n, WRITE_KEY))[0] == 202
        over = f"cap-over-{tag}"
        await _seed_canon(store, over)
        code, body = await _delete(over, WRITE_KEY)
        assert code == 409 and body["status"] == "refused" and body["reason"] == "cap", body
        assert await _active(store, over) is not None

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_write_key_deletes_a_working_row_directly(backend, tmp_path, monkeypatch, api_mode):
    name = _uniq("work-del")

    async def run(store):
        await _seed_working(store, name)
        code, body = await _delete(name, WRITE_KEY)
        assert code == 200 and body["status"] == "soft_deleted", body
        assert await _active(store, name) is None

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_read_key_gets_403_from_the_route(backend, tmp_path, monkeypatch, api_mode):
    name = _uniq("work-del-r")

    async def run(store):
        await _seed_working(store, name)
        assert (await _delete(name, READ_KEY))[0] == 403
        assert (await _restore(name, READ_KEY))[0] == 403

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_hard_delete_stays_dreamer(backend, tmp_path, monkeypatch, api_mode):
    name = _uniq("hard")

    async def run(store):
        await _seed_working(store, name)
        assert (await _delete(name, WRITE_KEY, hard=True))[0] == 403
        assert await _active(store, name) is not None
        code, body = await _delete(name, DREAM_KEY, hard=True)
        assert code == 200 and body["status"] == "hard_deleted"

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_host_mode_pins_the_v2316_erratum(backend, tmp_path, monkeypatch):
    """In host mode the route's role check does nothing and the lock still queues or refuses.
    v2.3.16 reported both as done (200); v2.3.17 reports them truthfully."""
    import mori_advisor.policy as pol

    monkeypatch.setattr(pol, "_TD_MODE", "host")
    monkeypatch.setattr(pol, "_ROLES", {})
    live, gone, hard = _uniq("host-live"), _uniq("host-gone"), _uniq("host-hard")

    async def run(store):
        await _seed_canon(store, live)
        code, body = await _delete(live, WRITE_KEY)
        assert code == 202 and body["status"] == "pending", body
        assert await _active(store, live) is not None
        await _tombstone(store, gone, canonical=True)
        code, body = await _restore(gone, WRITE_KEY)
        assert code == 409 and body["reason"] == "canon-authority", body
        assert await _active(store, gone) is None
        await _seed_canon(store, hard)
        code, body = await _delete(hard, WRITE_KEY, hard=True)
        assert code == 409 and body["status"] == "refused", body
        assert body["reason"] == "canon-authority"
        assert await _active(store, hard) is not None

    _run_with_backend(backend, tmp_path, monkeypatch, run)


def test_a_database_error_on_soft_delete_is_500_never_200(tmp_path, monkeypatch, api_mode):
    import sqlite3

    name = _uniq("dberr")

    async def run(store):
        await _seed_working(store, name)

        def boom(*a, **k):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(type(_ms(store)), "_active_target", boom)
        code, body = await _delete(name, WRITE_KEY)
        assert code == 500, body
        assert body.get("status") != "soft_deleted"

    _run_with_backend("sqlite", tmp_path, monkeypatch, run)


# ── item 1: restore ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_write_key_restore_splits_canonical_from_working(backend, tmp_path, monkeypatch, api_mode):
    canon, work, live = _uniq("rs-canon"), _uniq("rs-work"), _uniq("rs-live")

    async def run(store):
        await _tombstone(store, canon, canonical=True)
        code, body = await _restore(canon, WRITE_KEY)
        assert code == 409, body
        assert body["status"] == "refused" and body["reason"] == "canon-authority"
        assert await _active(store, canon) is None
        await _tombstone(store, work, canonical=False)
        code, body = await _restore(work, WRITE_KEY)
        assert code == 200 and body["status"] == "restored", body
        assert await _active(store, work) is not None
        await _seed_working(store, live)
        assert (await _restore(live, WRITE_KEY))[0] == 404

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_restore_conflict_is_its_own_status(backend, tmp_path, monkeypatch, api_mode):
    """The collision path is a different status word, so the contract can't mistake it for the lock."""
    from mori_advisor.canon_authority import RestoreOutcome

    name = _uniq("rs-conf")

    async def run(store):
        target = _ms(store)

        async def conflict(*a, **k):
            return RestoreOutcome(kind="conflict", name=name, message="name collision")

        def conflict_sync(*a, **k):
            return RestoreOutcome(kind="conflict", name=name, message="name collision")

        is_async = inspect.iscoroutinefunction(type(target).restore_outcome)
        monkeypatch.setattr(
            type(target), "restore_outcome", conflict if is_async else conflict_sync
        )
        code, body = await _restore(name, WRITE_KEY)
        assert code == 409 and body["status"] == "conflict", body
        assert body.get("reason") != "canon-authority"

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── item 1: the protect route (Q3) ─────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_protect_route_is_dreamer_only_and_audited(backend, tmp_path, monkeypatch, api_mode):
    from mori_advisor.main import protect_memory_rest

    name = _uniq("prot")

    async def run(store):
        await _seed_canon(store, name)
        with as_key(WRITE_KEY):
            r = await protect_memory_rest(_request("POST", name, body={"protected": True}))
        assert r.status_code == 403
        with as_key(DREAM_KEY):
            r = await protect_memory_rest(_request("POST", name, body={"protected": True}))
        assert r.status_code == 200, r.body
        assert (await _active(store, name))["protected"] in (1, True)
        assert any(
            a["actor_key_name"] == DREAM_KEY and a["memory_name"] == name
            for a in await _audit(store, "protect")
        )

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── item 2: the canaries ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_approving_a_delete_by_mistake_removes_a_protected_canary(
    backend, tmp_path, monkeypatch, api_mode
):
    """The pinned fail-closed sequence: protection does not block approval, so the next contract
    delete probe sees 404 and fails, and CD's write key cannot recreate the canary."""
    from mori_advisor.main import approve_memory, post_memory
    from tests.test_write_api import _fake_request

    name = _uniq("cd-canary-live")

    async def run(store):
        await _seed_canon(store, name, protected=True)
        code, body = await _delete(name, WRITE_KEY)
        assert code == 202
        with as_key(DREAM_KEY):
            r = await approve_memory(
                _fake_request({"write_id": body["pending_id"]}, path_params={"name": name})
            )
        assert r.status_code == 200, r.body
        assert await _active(store, name) is None  # deleted_at set; protection didn't block
        assert (await _delete(name, WRITE_KEY))[0] == 404  # the next probe fails closed
        with as_key(WRITE_KEY):  # and the write key can't put it back
            r = await post_memory(
                _fake_request(
                    {"name": name, "body": "x", "tier": "canonical", "tags": ["cd-canary"]}
                )
            )
        assert r.status_code in (202, 409)
        assert await _active(store, name) is None

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_cd_canary_names_are_reserved_for_canon_authority(backend, tmp_path, monkeypatch, api_mode):
    """P2-2: a non-canonical write or a restore of a cd-canary-* name without canon authority is
    refused, so the tombstone's name can't be squatted (both orders tested)."""
    from mori_advisor.write_result import Disposition

    squat, tomb = _uniq("cd-canary-squat"), _uniq("cd-canary-tomb")

    async def run(store):
        r = await _a(
            _ms(store)._write(
                name=squat, title="t", body="b", tier="working", provenance=_req(WRITE_KEY)
            )
        )
        assert r.disposition is Disposition.REJECTED, r
        assert await _active(store, squat) is None
        # order 1: the lock answers first for a canonical tombstone
        await _tombstone(store, tomb, canonical=True)
        code, body = await _restore(tomb, WRITE_KEY)
        assert code == 409 and body["reason"] == "canon-authority"
        # order 2: a working tombstone of a reserved name is refused too (only authority restores)
        wt = _uniq("cd-canary-wt")
        await _tombstone(store, wt, canonical=False)
        code, body = await _restore(wt, WRITE_KEY)
        assert code == 409 and body["status"] == "refused" and body["reason"] == "reserved-name"
        assert await _active(store, wt) is None

    _run_with_backend(backend, tmp_path, monkeypatch, run)


def test_review_fields_flag_a_cd_canary_proposal():
    from mori_advisor.proposal_view import review_fields

    # a tagged row with an ordinary name: only the TARGET's tag can raise the flag (C2)
    target = {"name": "probe-row", "tags": '["cd-canary"]', "body": "b", "tier": "canonical"}
    item = {"name": "probe-row", "operation": "write", "tags": "[]", "body": "b"}
    assert review_fields(item, target, [])["is_cd_canary"] is True
    # C2: the flag must read the TARGET's tags, not only the proposal's
    other = {"name": "x", "operation": "write", "tags": '["cd-canary"]', "body": "b"}
    assert review_fields(other, None, [])["is_cd_canary"] is True
    plain = {"name": "y", "operation": "delete", "tags": '["test"]', "body": "b"}
    assert review_fields(plain, {"name": "y", "tags": '["test"]'}, [])["is_cd_canary"] is False


def test_review_page_renders_the_canary_banner():
    from pathlib import Path

    import mori_advisor

    html = (Path(mori_advisor.__file__).parent.parent / "dashboard" / "review.html").read_text()
    assert "CD DEPLOY CANARY — DO NOT APPROVE" in html
    assert "if (item.is_cd_canary) {" in html  # C3b: the banner shows when the flag is set


# ── item 3: a rejection needs a reason ─────────────────────────────────────────────────


async def _queued(store, name: str) -> int:
    await _seed_canon(store, name)
    code, body = await _delete(name, WRITE_KEY)
    assert code == 202
    return body["pending_id"]


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("note", ["", "   ", None])
def test_the_store_refuses_a_rejection_without_a_reason(
    backend, tmp_path, monkeypatch, api_mode, note
):
    from mori_advisor.canon_authority import ReasonRequired

    name = _uniq("rej-store")

    async def run(store):
        pid = await _queued(store, name)
        with pytest.raises(ReasonRequired):
            await _a(_ms(store).reject(pid, note=note, reviewer=DREAM_KEY))
        row = [p for p in await _pending(store, name) if p["id"] == pid]
        assert row and row[0]["status"] == "pending"

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_rest_reject_without_a_reason_is_422_with_no_audit(
    backend, tmp_path, monkeypatch, api_mode
):
    from mori_advisor.main import reject_memory
    from tests.test_write_api import _fake_request

    name = _uniq("rej-rest")

    async def run(store):
        pid = await _queued(store, name)
        for payload in ({"write_id": pid}, {"write_id": pid, "note": "  "}):
            with as_key(DREAM_KEY):
                r = await reject_memory(_fake_request(payload, path_params={"name": name}))
            assert r.status_code == 422, r.body
        # REST audits under the memory's own name (MCP uses write:<id>)
        assert not [a for a in await _audit(store, "reject") if a["memory_name"] == name]
        assert [p["status"] for p in await _pending(store, name)] == ["pending"]
        with as_key(DREAM_KEY):
            r = await reject_memory(
                _fake_request(
                    {"write_id": pid, "note": "post-deploy check"}, path_params={"name": name}
                )
            )
        assert r.status_code == 200, r.body
        rows = await _rows(
            store,
            "SELECT status, review_note, reviewed_by FROM pending_writes WHERE id = ?",
            "SELECT status, review_note, reviewed_by FROM pending_writes WHERE id = $1",
            pid,
        )
        assert rows[0]["status"] == "rejected" and rows[0]["review_note"] == "post-deploy check"
        assert rows[0]["reviewed_by"] == DREAM_KEY

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_mcp_reject_without_a_reason_is_refused_with_no_audit(
    backend, tmp_path, monkeypatch, api_mode
):
    from mori_advisor.main import memory_reject

    name = _uniq("rej-mcp")

    async def run(store):
        pid = await _queued(store, name)
        with as_key(DREAM_KEY):
            out = await memory_reject(pid, note="", reason="stale")
        assert "reason" in out.lower() and "rejected" not in out.lower(), out
        assert not [a for a in await _audit(store, "reject") if a["memory_name"] == f"write:{pid}"]
        assert [p["status"] for p in await _pending(store, name)] == ["pending"]
        with as_key(DREAM_KEY):
            out = await memory_reject(pid, note="duplicate of x", reason="duplicate")
        assert "rejected" in out.lower(), out
        audits = [a for a in await _audit(store, "reject") if a["memory_name"] == f"write:{pid}"]
        assert [a["actor_key_name"] for a in audits] == [DREAM_KEY]

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_reject_returns_a_structured_outcome(backend, tmp_path, monkeypatch, api_mode):
    from mori_advisor.canon_authority import RejectOutcome

    name = _uniq("rej-out")

    async def run(store):
        pid = await _queued(store, name)
        out = await _a(_ms(store).reject(pid, note="not needed", reviewer=DREAM_KEY))
        assert isinstance(out, RejectOutcome) and out.kind == "rejected", out
        again = await _a(_ms(store).reject(pid, note="again", reviewer=DREAM_KEY))
        assert again.kind != "rejected"  # already reviewed: no second audit-worthy rejection
        missing = await _a(_ms(store).reject(10**9, note="x", reviewer=DREAM_KEY))
        assert missing.kind == "not_found"

    _run_with_backend(backend, tmp_path, monkeypatch, run)


def test_review_page_sends_a_required_reason():
    from pathlib import Path

    import mori_advisor

    html = (Path(mori_advisor.__file__).parent.parent / "dashboard" / "review.html").read_text()
    assert "note:" in html or "note :" in html  # the reject payload carries the reason
    assert "rejectReason" in html


def test_review_fields_flag_a_reserved_name_without_the_tag():
    """Consult C17, P3-3: a lookalike canonical create named cd-canary-* but untagged still gets the
    banner."""
    from mori_advisor.proposal_view import review_fields

    item = {"name": "cd-canary-evil", "operation": "write", "tags": "[]", "body": "b"}
    assert review_fields(item, None, [])["is_cd_canary"] is True


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_protect_route_reports_a_store_error_as_500(backend, tmp_path, monkeypatch, api_mode):
    """Consult C17, P2-2: success only on the store's positive message."""
    from mori_advisor.main import protect_memory_rest

    name = _uniq("prot-err")

    async def run(store):
        await _seed_canon(store, name)
        target = type(_ms(store))

        def broken(self, *a, **k):
            return "Database error: disk I/O error"

        async def broken_async(self, *a, **k):
            return "Database error: disk I/O error"

        monkeypatch.setattr(
            target,
            "protect",
            broken_async if inspect.iscoroutinefunction(target.protect) else broken,
        )
        with as_key(DREAM_KEY):
            r = await protect_memory_rest(_request("POST", name, body={"protected": True}))
        assert r.status_code == 500, r.body

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_rest_reject_reports_a_store_error_as_500(backend, tmp_path, monkeypatch, api_mode):
    """Consult C17, P3-1: a database error on reject is 500, not 'not found'."""
    from mori_advisor.canon_authority import RejectOutcome
    from mori_advisor.main import reject_memory
    from tests.test_write_api import _fake_request

    name = _uniq("rej-err")

    async def run(store):
        pid = await _queued(store, name)
        target = type(_ms(store))

        def broken(self, write_id, **k):
            return RejectOutcome(kind="error", write_id=write_id, message="Database error: x")

        async def broken_async(self, write_id, **k):
            return RejectOutcome(kind="error", write_id=write_id, message="Database error: x")

        monkeypatch.setattr(
            target, "reject", broken_async if inspect.iscoroutinefunction(target.reject) else broken
        )
        with as_key(DREAM_KEY):
            r = await reject_memory(
                _fake_request({"write_id": pid, "note": "a reason"}, path_params={"name": name})
            )
        assert r.status_code == 500, r.body

    _run_with_backend(backend, tmp_path, monkeypatch, run)


def test_review_page_keeps_the_key_in_memory_only():
    """Board, pre-push ruling (8 Nov): the dreamer key lives in a page variable only — never in
    localStorage or sessionStorage (both can reach disk) — the page asks for it on each load, and any
    copy an earlier version stored is removed on load."""
    import re
    from pathlib import Path

    import mori_advisor

    html = (Path(mori_advisor.__file__).parent.parent / "dashboard" / "review.html").read_text()
    assert not re.search(
        r"(localStorage|sessionStorage)\.(setItem|getItem)\(\s*'mori_review_key'", html
    )
    assert not re.search(
        r"sessionStorage\s*\.", html
    )  # no use of it at all (a comment may name it)
    assert "localStorage.removeItem('mori_review_key')" in html
    assert re.search(r"apiKey:\s*''", html)  # starts empty on every load
