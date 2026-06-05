"""Write REST API tests — governed write/approve/reject/delete endpoints (#14).

Acceptance criteria:

1. Capability enforcement on the REST routes:
   - read actor denied POST /api/memories (403)
   - read actor denied GET /api/pending (403)
   - write actor allowed to POST /api/memories but denied approve/reject/delete
   - dreamer actor allowed all operations

2. Propose-not-overwrite semantics:
   - proposing a NEW name → working row created (201)
   - proposing over a CANONICAL name → pending proposal created (202), canonical unchanged
   - proposing over a WORKING name with same actor → updated (200)
   - proposing over a WORKING name with different actor → pending proposal (202)

3. Contextvar-missing fail-closed:
   - require_role raises PermissionDenied when current_actor is unset in api mode
   - a missing actor must be a denial, never a silent pass

4. Input validation:
   - too-long body → 400
   - invalid name → 400
   - unexpected fields → 400
   - missing name → 400

Both backends via @requires_pg for store-mutating tests.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from mori_advisor.policy import Actor, PermissionDenied, current_actor

# ── Backend parametrisation ───────────────────────────────────────────────────

PG_URL = os.environ.get("MORI_TEST_DATABASE_URL", "")
requires_pg = pytest.mark.skipif(not PG_URL, reason="MORI_TEST_DATABASE_URL not set")

BACKENDS = ["sqlite"]
if PG_URL:
    BACKENDS.append("postgres")


# ── Store / module helpers ────────────────────────────────────────────────────


def _make_sqlite_store(tmp_path: Path):
    from mori_advisor.store import get_store

    s = get_store(tmp_path / "memories.db")
    s.bootstrap()
    return s


async def _make_pg_store():
    from mori_advisor.store.postgres_store import PostgresStore

    s = PostgresStore(PG_URL)
    await s.bootstrap()
    async with s.pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE memories, memory_versions, pending_writes, "
            "eviction_queue, ingestion_log, session_events, "
            "dream_state, dreamer_config, msg_log CASCADE"
        )
    return s


def _apply_store(monkeypatch, store):
    import mori_advisor.main as m

    memory_store = store._mem if hasattr(store, "_mem") else store
    session_log = store._log if hasattr(store, "_log") else store
    monkeypatch.setattr(m, "store", store)
    monkeypatch.setattr(m, "memory_store", memory_store)
    monkeypatch.setattr(m, "session_log", session_log)


def _run_with_backend(backend: str, tmp_path: Path, monkeypatch, coro_fn):
    async def run():
        if backend == "sqlite":
            store = _make_sqlite_store(tmp_path)
        else:
            store = await _make_pg_store()
        try:
            _apply_store(monkeypatch, store)
            return await coro_fn(store)
        finally:
            if hasattr(store, "pool") and store.pool:
                await store.pool.close()

    return asyncio.run(run())


def _patch_policy(monkeypatch, mode: str, local_full_access: bool = False):
    import mori_advisor.policy as pol

    monkeypatch.setattr(pol, "_TD_MODE", mode)
    monkeypatch.setattr(pol, "_LOCAL_FULL_ACCESS", local_full_access)


@contextmanager
def _actor_context(actor):
    token = current_actor.set(actor)
    try:
        yield
    finally:
        current_actor.reset(token)


# ── Fake Request helper ───────────────────────────────────────────────────────


def _fake_request(body: dict | None = None, path_params: dict | None = None):
    """Build a minimal Starlette-compatible fake Request for testing REST handlers.

    Sets request.state.actor from current_actor ContextVar so tests can inject
    the actor via _actor_context without a full ASGI stack.
    """
    import json

    from starlette.datastructures import State
    from starlette.requests import Request

    raw_body = json.dumps(body or {}).encode() if body is not None else b""

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/memories",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "path_params": path_params or {},
    }

    class _Receive:
        _sent = False

        async def __call__(self):
            if not self._sent:
                self._sent = True
                return {"type": "http.request", "body": raw_body, "more_body": False}
            return {"type": "http.disconnect"}

    req = Request(scope, receive=_Receive())
    req._state = State()
    actor = current_actor.get()
    if actor is not None:
        req.state.actor = actor
    return req


# ── 1. Capability enforcement ─────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_post_memory_read_actor_denied(backend, tmp_path, monkeypatch):
    """POST /api/memories requires write role — read actor must get 403."""
    from mori_advisor.main import post_memory

    _patch_policy(monkeypatch, "api")

    async def run(store):
        req = _fake_request({"name": "test-denied", "title": "x", "body": "y"})
        with _actor_context(Actor("ci", "read")):
            req = _fake_request({"name": "test-denied", "title": "x", "body": "y"})
            resp = await post_memory(req)
        assert resp.status_code == 403
        import json

        body = json.loads(resp.body)
        assert "forbidden" in body.get("error", "").lower() or "role" in str(body).lower()

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_get_pending_read_actor_denied(backend, tmp_path, monkeypatch):
    """GET /api/pending requires write role — read actor must get 403."""
    from starlette.requests import Request

    from mori_advisor.main import get_pending

    _patch_policy(monkeypatch, "api")

    async def run(store):
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/pending",
            "query_string": b"",
            "headers": [],
        }

        class _Recv:
            async def __call__(self):
                return {"type": "http.disconnect"}

        req = Request(scope, receive=_Recv())
        with _actor_context(Actor("ci", "read")):
            resp = await get_pending(req)
        assert resp.status_code == 403

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_post_memory_write_actor_allowed(backend, tmp_path, monkeypatch):
    """POST /api/memories allows write actor to create new working memories."""
    import json

    from mori_advisor.main import post_memory

    _patch_policy(monkeypatch, "api")

    async def run(store):
        with _actor_context(Actor("nuc", "write")):
            req = _fake_request(
                {"name": "write-test-new", "title": "New Memory", "body": "content"}
            )
            resp = await post_memory(req)
        assert resp.status_code in (200, 201), f"Got {resp.status_code}: {resp.body}"
        data = json.loads(resp.body)
        assert data.get("name") == "write-test-new"
        assert data.get("status") in ("created", "updated")

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_approve_write_actor_denied(backend, tmp_path, monkeypatch):
    """POST /api/memories/{name}/approve requires dreamer — write actor must get 403."""
    from mori_advisor.main import approve_memory

    _patch_policy(monkeypatch, "api")

    async def run(store):
        with _actor_context(Actor("nuc", "write")):
            req = _fake_request({"write_id": 99999}, path_params={"name": "any"})
            req._state.actor = Actor("nuc", "write")  # type: ignore[attr-defined]
            resp = await approve_memory(req)
        assert resp.status_code == 403

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_delete_write_actor_denied(backend, tmp_path, monkeypatch):
    """DELETE /api/memories/{name} requires dreamer — write actor must get 403."""
    from starlette.datastructures import State
    from starlette.requests import Request

    from mori_advisor.main import delete_memory_rest

    _patch_policy(monkeypatch, "api")

    async def run(store):
        scope = {
            "type": "http",
            "method": "DELETE",
            "path": "/api/memories/any",
            "query_string": b"",
            "headers": [],
            "path_params": {"name": "any"},
        }

        class _Recv:
            async def __call__(self):
                return {"type": "http.disconnect"}

        req = Request(scope, receive=_Recv())
        req._state = State()
        with _actor_context(Actor("nuc", "write")):
            resp = await delete_memory_rest(req)
        assert resp.status_code == 403

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_dreamer_actor_can_delete(backend, tmp_path, monkeypatch):
    """DELETE /api/memories/{name} allows dreamer actor (returns 200 or 404, not 403)."""
    import json

    from starlette.datastructures import State
    from starlette.requests import Request

    from mori_advisor.main import delete_memory_rest

    _patch_policy(monkeypatch, "api")

    async def run(store):
        # First write a memory to delete
        mem = store._mem if hasattr(store, "_mem") else store
        import inspect

        result = mem.write(name="dreamer-delete-test", title="To delete", body="bye")
        if inspect.isawaitable(result):
            await result

        scope = {
            "type": "http",
            "method": "DELETE",
            "path": "/api/memories/dreamer-delete-test",
            "query_string": b"",
            "headers": [],
            "path_params": {"name": "dreamer-delete-test"},
        }

        class _Recv:
            async def __call__(self):
                return {"type": "http.disconnect"}

        req = Request(scope, receive=_Recv())
        req._state = State()
        with _actor_context(Actor("gce", "dreamer")):
            resp = await delete_memory_rest(req)
        assert resp.status_code in (200, 404), f"Expected 200/404, got {resp.status_code}"
        if resp.status_code == 200:
            data = json.loads(resp.body)
            assert data.get("status") == "soft_deleted"  # default DELETE is soft (A+B / #23)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── 2. Propose-not-overwrite semantics ───────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_propose_new_name_creates_working(backend, tmp_path, monkeypatch):
    """Proposing a NEW name creates a working-tier memory directly (status=created, 201)."""
    import json

    from mori_advisor.main import post_memory

    _patch_policy(monkeypatch, "api")

    async def run(store):
        mem = store._mem if hasattr(store, "_mem") else store
        with _actor_context(Actor("nuc", "write")):
            req = _fake_request(
                {
                    "name": "brand-new-memory",
                    "title": "Brand New",
                    "body": "first write",
                    "origin_clients": ["nuc"],
                }
            )
            resp = await post_memory(req)

        assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.body}"
        data = json.loads(resp.body)
        assert data["status"] == "created"

        # Verify the memory exists in the store as working
        import inspect

        result = mem.get_memory("brand-new-memory")
        if inspect.isawaitable(result):
            result = await result
        assert result is not None
        assert result["tier"] == "working"

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_propose_over_canonical_creates_pending(backend, tmp_path, monkeypatch):
    """Proposing over a CANONICAL name creates a pending proposal; canonical row unchanged."""
    import inspect
    import json

    from mori_advisor.main import post_memory

    _patch_policy(monkeypatch, "api")

    async def run(store):
        mem = store._mem if hasattr(store, "_mem") else store

        # Seed a canonical memory
        r = mem.write(
            name="canonical-target",
            title="Original canonical",
            body="original body",
            tier="canonical",
            tags=["scope:global"],
        )
        if inspect.isawaitable(r):
            await r

        original = mem.get_memory("canonical-target")
        if inspect.isawaitable(original):
            original = await original
        assert original is not None
        assert original["tier"] == "canonical"

        # Propose an update over it
        with _actor_context(Actor("nuc", "write")):
            req = _fake_request(
                {
                    "name": "canonical-target",
                    "title": "Proposed update",
                    "body": "new body",
                }
            )
            resp = await post_memory(req)

        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.body}"
        data = json.loads(resp.body)
        assert data["status"] == "pending"

        # Canonical row must be unchanged
        after = mem.get_memory("canonical-target")
        if inspect.isawaitable(after):
            after = await after
        assert after is not None
        assert after["tier"] == "canonical"
        assert after["title"] == "Original canonical"
        assert after["body"] == "original body"

        # The proposal must be audited (regression: this _write_audit was un-awaited).
        rows = store.get_audit_log(memory_name="canonical-target")
        if inspect.isawaitable(rows):
            rows = await rows
        assert any(r["op"] == "propose_pending" for r in rows), (
            f"expected a propose_pending audit row, got {[r['op'] for r in rows]}"
        )

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_propose_working_same_actor_updates(backend, tmp_path, monkeypatch):
    """Proposing over a WORKING memory with the same actor in origin_clients → update (200)."""
    import inspect
    import json

    from mori_advisor.main import post_memory

    _patch_policy(monkeypatch, "api")

    async def run(store):
        mem = store._mem if hasattr(store, "_mem") else store

        # Seed a working memory owned by "nuc"
        r = mem.write(
            name="working-same-actor",
            title="Working memory",
            body="v1 body",
            tier="working",
            origin_clients=["nuc"],
        )
        if inspect.isawaitable(r):
            await r

        # Same actor proposes update
        with _actor_context(Actor("nuc", "write")):
            req = _fake_request(
                {
                    "name": "working-same-actor",
                    "title": "Updated working",
                    "body": "v2 body",
                    "origin_clients": ["nuc"],
                }
            )
            resp = await post_memory(req)

        assert resp.status_code in (200, 201), f"Got {resp.status_code}: {resp.body}"
        data = json.loads(resp.body)
        assert data["status"] in ("updated", "created")

        # Memory should be updated
        after = mem.get_memory("working-same-actor")
        if inspect.isawaitable(after):
            after = await after
        assert after is not None
        assert after["title"] == "Updated working"

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_propose_working_different_actor_creates_pending(backend, tmp_path, monkeypatch):
    """Proposing over a WORKING memory owned by a different actor → pending proposal (202)."""
    import inspect
    import json

    from mori_advisor.main import post_memory

    _patch_policy(monkeypatch, "api")

    async def run(store):
        mem = store._mem if hasattr(store, "_mem") else store

        # Seed a working memory owned by "alice"
        r = mem.write(
            name="working-other-actor",
            title="Alice memory",
            body="alice content",
            tier="working",
            origin_clients=["alice"],
        )
        if inspect.isawaitable(r):
            await r

        # Bob proposes an update
        with _actor_context(Actor("bob", "write")):
            req = _fake_request(
                {
                    "name": "working-other-actor",
                    "title": "Bob proposal",
                    "body": "bob content",
                    "origin_clients": ["bob"],
                }
            )
            resp = await post_memory(req)

        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.body}"
        data = json.loads(resp.body)
        assert data["status"] == "pending"

        # Original memory must be unchanged
        after = mem.get_memory("working-other-actor")
        if inspect.isawaitable(after):
            after = await after
        assert after is not None
        assert after["title"] == "Alice memory"

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── 3. Contextvar-missing fail-closed ─────────────────────────────────────────


def test_require_role_missing_actor_fails_closed(monkeypatch):
    """require_role MUST raise PermissionDenied when current_actor is unset in api mode.

    A missing actor is a denial, never a silent pass. This is the fail-closed contract.
    """
    from mori_advisor.policy import require_role

    _patch_policy(monkeypatch, "api", local_full_access=False)

    # Do NOT use _actor_context — ContextVar stays at its default (None).
    # Verify current_actor is actually None before proceeding.
    assert current_actor.get() is None, "Test precondition: current_actor must be None"

    with pytest.raises(PermissionDenied) as exc_info:
        require_role("write")

    detail = str(exc_info.value)
    assert "actor" in detail.lower() or "key" in detail.lower() or "required" in detail.lower(), (
        f"PermissionDenied message should mention actor/key/required, got: {detail}"
    )


def test_require_role_missing_actor_dreamer_fails_closed(monkeypatch):
    """require_role('dreamer') also fails closed when actor is None in api mode."""
    from mori_advisor.policy import require_role

    _patch_policy(monkeypatch, "api", local_full_access=False)
    assert current_actor.get() is None

    with pytest.raises(PermissionDenied):
        require_role("dreamer")


def test_require_role_missing_actor_host_mode_is_noop(monkeypatch):
    """In host mode, a missing actor never raises (backward compat)."""
    from mori_advisor.policy import require_role

    _patch_policy(monkeypatch, "host")
    assert current_actor.get() is None

    # Must not raise
    require_role("write")
    require_role("dreamer")


# ── 4. Input validation ───────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_too_long_body_returns_400(backend, tmp_path, monkeypatch):
    """A body exceeding 64 KB must return 400."""
    import json

    from mori_advisor.main import post_memory

    _patch_policy(monkeypatch, "api")

    async def run(store):
        huge_body = "x" * (65 * 1024)
        with _actor_context(Actor("nuc", "write")):
            req = _fake_request({"name": "valid-name", "title": "t", "body": huge_body})
            resp = await post_memory(req)
        assert resp.status_code == 400
        data = json.loads(resp.body)
        assert "body" in data.get("error", "").lower() or "size" in data.get("error", "").lower()

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_invalid_name_returns_400(backend, tmp_path, monkeypatch):
    """A name with forbidden characters must return 400."""
    import json

    from mori_advisor.main import post_memory

    _patch_policy(monkeypatch, "api")

    async def run(store):
        for bad_name in ["has space", "../traversal", "a" * 200, "", "has/slash"]:
            with _actor_context(Actor("nuc", "write")):
                req = _fake_request({"name": bad_name, "title": "t", "body": "b"})
                resp = await post_memory(req)
            assert resp.status_code == 400, (
                f"Expected 400 for name={bad_name!r}, got {resp.status_code}"
            )
            data = json.loads(resp.body)
            assert (
                "name" in data.get("error", "").lower() or "field" in data.get("error", "").lower()
            )

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_unexpected_fields_returns_400(backend, tmp_path, monkeypatch):
    """Unexpected fields in the POST body must return 400."""
    import json

    from mori_advisor.main import post_memory

    _patch_policy(monkeypatch, "api")

    async def run(store):
        with _actor_context(Actor("nuc", "write")):
            req = _fake_request(
                {
                    "name": "valid-name",
                    "title": "t",
                    "body": "b",
                    "secret_field": "injected",
                }
            )
            resp = await post_memory(req)
        assert resp.status_code == 400
        data = json.loads(resp.body)
        assert (
            "unexpected" in data.get("error", "").lower()
            or "field" in data.get("error", "").lower()
        )

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_missing_name_returns_400(backend, tmp_path, monkeypatch):
    """Missing 'name' field must return 400."""
    import json

    from mori_advisor.main import post_memory

    _patch_policy(monkeypatch, "api")

    async def run(store):
        with _actor_context(Actor("nuc", "write")):
            req = _fake_request({"title": "no name", "body": "content"})
            resp = await post_memory(req)
        assert resp.status_code == 400
        data = json.loads(resp.body)
        assert "name" in data.get("error", "").lower()

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── 5. _validate_write_payload unit tests (pure, no store) ───────────────────


def test_validate_write_payload_good():
    """Valid payload passes validation with no error."""
    from mori_advisor.main import _validate_write_payload

    err, code = _validate_write_payload(
        {
            "name": "valid-name-123",
            "title": "Good",
            "body": "content",
            "tags": ["foo", "bar"],
        }
    )
    assert err is None
    assert code == 0


def test_validate_write_payload_empty_name():
    from mori_advisor.main import _validate_write_payload

    err, code = _validate_write_payload({"name": "", "body": "x"})
    assert err is not None
    assert code == 400


def test_validate_write_payload_bad_name_chars():
    from mori_advisor.main import _validate_write_payload

    for bad in ["has space", "has/slash", "a" * 200]:
        err, code = _validate_write_payload({"name": bad, "body": "x"})
        assert err is not None and code == 400, f"Should have failed for {bad!r}"


def test_validate_write_payload_oversized_body():
    from mori_advisor.main import _BODY_MAX_BYTES, _validate_write_payload

    huge = "x" * (_BODY_MAX_BYTES + 1)
    err, code = _validate_write_payload({"name": "good-name", "body": huge})
    assert err is not None
    assert code == 400


def test_validate_write_payload_unexpected_field():
    from mori_advisor.main import _validate_write_payload

    err, code = _validate_write_payload({"name": "n", "body": "b", "injected": "bad"})
    assert err is not None
    assert code == 400
    assert "unexpected" in err.lower()


def test_validate_write_payload_tags_not_list():
    from mori_advisor.main import _validate_write_payload

    err, code = _validate_write_payload({"name": "n", "body": "b", "tags": "not-a-list"})
    assert err is not None
    assert code == 400
