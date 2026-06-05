"""Tests for issue #23 A+B — persistent audit table + soft-delete.

Acceptance criteria:
1. Audit row written on every governed op (write/approve/reject/soft_delete/hard_delete/restore).
2. Soft-deleted memory hidden from get/list/FTS search.
3. Partial unique index: a new active row can reuse a tombstoned name (supersession).
4. Restore with collision renames to {name}_restored_{ts}.
5. Hard-delete removes the row permanently (neither active nor tombstoned visible).
6. Role enforcement: read key denied soft/hard delete + audit + restore;
   write key allowed soft-delete; dreamer required for hard-delete, restore, audit.

Both backends via @requires_pg for store-mutating tests.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from mori_advisor.policy import Actor, current_actor

# ── Backend parametrisation ──────────────────────────────────────────────────

PG_URL = os.environ.get("MORI_TEST_DATABASE_URL", "")
requires_pg = pytest.mark.skipif(not PG_URL, reason="MORI_TEST_DATABASE_URL not set")

BACKENDS = ["sqlite"]
if PG_URL:
    BACKENDS.append("postgres")


# ── Store helpers (mirror test_write_api.py) ─────────────────────────────────


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
            "dream_state, dreamer_config, msg_log, write_audit CASCADE"
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


def _patch_policy(monkeypatch, mode: str):
    import mori_advisor.policy as pol

    monkeypatch.setattr(pol, "_TD_MODE", mode)
    monkeypatch.setattr(pol, "_LOCAL_FULL_ACCESS", False)


@contextmanager
def _actor_context(actor):
    token = current_actor.set(actor)
    try:
        yield
    finally:
        current_actor.reset(token)


def _fake_delete_request(name: str, hard: bool = False):
    from starlette.datastructures import State
    from starlette.requests import Request

    qs = b"hard=true" if hard else b""
    scope = {
        "type": "http",
        "method": "DELETE",
        "path": f"/api/memories/{name}",
        "query_string": qs,
        "headers": [],
        "path_params": {"name": name},
    }

    class _Recv:
        async def __call__(self):
            return {"type": "http.disconnect"}

    req = Request(scope, receive=_Recv())
    req._state = State()
    actor = current_actor.get()
    if actor is not None:
        req.state.actor = actor
    return req


def _fake_post_request(path: str, path_params: dict, body: dict | None = None):
    import json

    from starlette.datastructures import State
    from starlette.requests import Request

    raw = json.dumps(body or {}).encode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "path_params": path_params,
    }

    class _Recv:
        _sent = False

        async def __call__(self):
            if not self._sent:
                self._sent = True
                return {"type": "http.request", "body": raw, "more_body": False}
            return {"type": "http.disconnect"}

    req = Request(scope, receive=_Recv())
    req._state = State()
    actor = current_actor.get()
    if actor is not None:
        req.state.actor = actor
    return req


def _fake_get_request(path: str, query: str = "", path_params: dict | None = None):
    from starlette.datastructures import State
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": query.encode(),
        "headers": [],
        "path_params": path_params or {},
    }

    class _Recv:
        async def __call__(self):
            return {"type": "http.disconnect"}

    req = Request(scope, receive=_Recv())
    req._state = State()
    actor = current_actor.get()
    if actor is not None:
        req.state.actor = actor
    return req


# ── A. Audit table ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_audit_row_on_write(backend, tmp_path, monkeypatch):
    """Audit row inserted when a new memory is created."""
    from mori_advisor.main import post_memory

    _patch_policy(monkeypatch, "api")

    async def run(store):
        import json

        from starlette.datastructures import State
        from starlette.requests import Request

        body = {"name": "audit-write-test", "title": "Audit write", "body": "hello"}
        raw = json.dumps(body).encode()
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/memories",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "path_params": {},
        }

        class _Recv:
            _sent = False

            async def __call__(self):
                if not self._sent:
                    self._sent = True
                    return {"type": "http.request", "body": raw, "more_body": False}
                return {"type": "http.disconnect"}

        req = Request(scope, receive=_Recv())
        req._state = State()
        with _actor_context(Actor("writer", "write")):
            req2 = Request(scope, receive=_Recv())
            req2._state = State()
            resp = await post_memory(req2)
        assert resp.status_code in (200, 201, 202)

        rows = (
            await asyncio.coroutine(lambda: store.get_audit_log(memory_name="audit-write-test"))()
            if asyncio.iscoroutinefunction(store.get_audit_log)
            else store.get_audit_log(memory_name="audit-write-test")
        )
        assert len(rows) >= 1
        assert rows[0]["memory_name"] == "audit-write-test"
        assert rows[0]["op"] in ("propose_new", "update_working", "propose_pending")

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_audit_row_on_soft_delete(backend, tmp_path, monkeypatch):
    """Audit row inserted on soft-delete; row type is 'soft_delete'."""
    from mori_advisor.main import delete_memory_rest

    _patch_policy(monkeypatch, "api")

    async def run(store):
        mem = store._mem if hasattr(store, "_mem") else store
        r = mem.write(name="audit-del-test", title="X", body="y")
        if asyncio.iscoroutine(r):
            await r

        with _actor_context(Actor("dreamer", "dreamer")):
            req = _fake_delete_request("audit-del-test", hard=False)
            resp = await delete_memory_rest(req)
        assert resp.status_code == 200

        rows = (
            await asyncio.coroutine(lambda: store.get_audit_log(memory_name="audit-del-test"))()
            if asyncio.iscoroutinefunction(store.get_audit_log)
            else store.get_audit_log(memory_name="audit-del-test")
        )
        ops = [r["op"] for r in rows]
        assert "soft_delete" in ops

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_audit_get_endpoint_dreamer_only(backend, tmp_path, monkeypatch):
    """GET /api/audit returns 403 for non-dreamer and 200 for dreamer."""
    from mori_advisor.main import get_audit_log_rest

    _patch_policy(monkeypatch, "api")

    async def run(store):
        # Read key → 403
        with _actor_context(Actor("reader", "read")):
            req = _fake_get_request("/api/audit")
            resp = await get_audit_log_rest(req)
        assert resp.status_code == 403

        # Dreamer → 200
        with _actor_context(Actor("dreamer", "dreamer")):
            req = _fake_get_request("/api/audit")
            resp = await get_audit_log_rest(req)
        assert resp.status_code == 200

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── B. Soft-delete ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_soft_delete_hides_from_read(backend, tmp_path, monkeypatch):
    """Soft-deleted memory not returned by memory_read or get_memory."""
    _patch_policy(monkeypatch, "api")

    async def run(store):
        mem = store._mem if hasattr(store, "_mem") else store
        r = mem.write(name="sd-read-test", title="X", body="y")
        if asyncio.iscoroutine(r):
            await r

        r2 = store.soft_delete("sd-read-test")
        if asyncio.iscoroutine(r2):
            await r2

        # read() returns "not found"
        result = mem.read("sd-read-test")
        if asyncio.iscoroutine(result):
            result = await result
        assert "not found" in result.lower()

        # get_memory() returns None
        m = mem.get_memory("sd-read-test") if hasattr(mem, "get_memory") else None
        if asyncio.iscoroutine(m):
            m = await m
        assert m is None

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_soft_delete_hides_from_list(backend, tmp_path, monkeypatch):
    """Soft-deleted memory not returned by list()."""
    _patch_policy(monkeypatch, "api")

    async def run(store):
        mem = store._mem if hasattr(store, "_mem") else store
        r = mem.write(name="sd-list-test", title="To be deleted", body="body")
        if asyncio.iscoroutine(r):
            await r

        r2 = store.soft_delete("sd-list-test")
        if asyncio.iscoroutine(r2):
            await r2

        listing = mem.list()
        if asyncio.iscoroutine(listing):
            listing = await listing
        assert "sd-list-test" not in listing

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_soft_delete_hides_from_fts_search(backend, tmp_path, monkeypatch):
    """Soft-deleted memory must not appear in search results (FTS or LIKE)."""
    _patch_policy(monkeypatch, "api")

    async def run(store):
        mem = store._mem if hasattr(store, "_mem") else store

        unique_kw = "xyzzy9182837465"
        r = mem.write(name="sd-fts-test", title="FTS target", body=unique_kw)
        if asyncio.iscoroutine(r):
            await r

        r2 = store.soft_delete("sd-fts-test")
        if asyncio.iscoroutine(r2):
            await r2

        if hasattr(mem, "search"):
            result = mem.search(unique_kw)
            if asyncio.iscoroutine(result):
                result = await result
            assert "sd-fts-test" not in result

        if hasattr(store, "search_json"):
            hits = store.search_json(query=unique_kw)
            if asyncio.iscoroutine(hits):
                hits = await hits
            assert not any(h["name"] == "sd-fts-test" for h in hits)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_partial_unique_index_allows_reuse_of_tombstoned_name(backend, tmp_path, monkeypatch):
    """After soft-delete, a new active row can take the same name (supersession)."""
    _patch_policy(monkeypatch, "api")

    async def run(store):
        mem = store._mem if hasattr(store, "_mem") else store

        r = mem.write(name="reuse-name-test", title="Old", body="old body")
        if asyncio.iscoroutine(r):
            await r

        r2 = store.soft_delete("reuse-name-test")
        if asyncio.iscoroutine(r2):
            await r2

        # New write with the same name must succeed, not conflict.
        r3 = mem.write(name="reuse-name-test", title="New", body="new body")
        if asyncio.iscoroutine(r3):
            await r3

        # Active row should be the new one.
        m = mem.get_memory("reuse-name-test") if hasattr(mem, "get_memory") else None
        if asyncio.iscoroutine(m):
            m = await m
        assert m is not None
        assert m["title"] == "New"

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_hard_delete_purges_row(backend, tmp_path, monkeypatch):
    """hard_delete removes the row entirely; no tombstone remains."""
    _patch_policy(monkeypatch, "api")

    async def run(store):
        mem = store._mem if hasattr(store, "_mem") else store

        r = mem.write(name="hard-del-test", title="X", body="y")
        if asyncio.iscoroutine(r):
            await r

        r2 = store.hard_delete("hard-del-test")
        if asyncio.iscoroutine(r2):
            await r2

        # Should be gone — even export_all won't find it.
        result = mem.read("hard-del-test")
        if asyncio.iscoroutine(result):
            result = await result
        assert "not found" in result.lower()

        # hard-deleting again should return not-found (not error).
        r3 = store.hard_delete("hard-del-test")
        if asyncio.iscoroutine(r3):
            r3 = await r3
        assert "not found" in r3.lower()

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_restore_simple(backend, tmp_path, monkeypatch):
    """Restore a tombstoned memory; it becomes active again."""
    _patch_policy(monkeypatch, "api")

    async def run(store):
        mem = store._mem if hasattr(store, "_mem") else store

        r = mem.write(name="restore-test", title="Restore me", body="original")
        if asyncio.iscoroutine(r):
            await r

        r2 = store.soft_delete("restore-test")
        if asyncio.iscoroutine(r2):
            await r2

        final_name, msg = store.restore_memory("restore-test")
        if asyncio.iscoroutine((final_name, msg)):
            final_name, msg = await store.restore_memory("restore-test")
        assert final_name == "restore-test"
        assert "restored" in msg.lower()

        m = mem.get_memory("restore-test") if hasattr(mem, "get_memory") else None
        if asyncio.iscoroutine(m):
            m = await m
        assert m is not None

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_restore_with_collision_renames(backend, tmp_path, monkeypatch):
    """Restore renames to {name}_restored_{ts} when a new active row holds the name."""
    _patch_policy(monkeypatch, "api")

    async def run(store):
        mem = store._mem if hasattr(store, "_mem") else store

        # 1. Write and tombstone original.
        r = mem.write(name="collision-test", title="Old", body="old")
        if asyncio.iscoroutine(r):
            await r

        r2 = store.soft_delete("collision-test")
        if asyncio.iscoroutine(r2):
            await r2

        # 2. New active row takes the name (supersession).
        r3 = mem.write(name="collision-test", title="New", body="new")
        if asyncio.iscoroutine(r3):
            await r3

        # 3. Restore — must rename since the name is taken.
        final_name, msg = store.restore_memory("collision-test")
        if asyncio.iscoroutine((final_name, msg)):
            final_name, msg = await store.restore_memory("collision-test")

        assert final_name != "collision-test"
        assert "_restored_" in final_name
        assert "name taken" in msg.lower() or "renamed" in msg.lower() or "restored" in msg.lower()

        # Original new row still active under original name.
        m = mem.get_memory("collision-test") if hasattr(mem, "get_memory") else None
        if asyncio.iscoroutine(m):
            m = await m
        assert m is not None
        assert m["title"] == "New"

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_restore_endpoint_role_enforcement(backend, tmp_path, monkeypatch):
    """POST /api/memories/{name}/restore requires dreamer role."""
    from mori_advisor.main import restore_memory_rest

    _patch_policy(monkeypatch, "api")

    async def run(store):
        # Read actor → 403
        with _actor_context(Actor("reader", "read")):
            req = _fake_post_request("/api/memories/x/restore", {"name": "x"}, body={})
            resp = await restore_memory_rest(req)
        assert resp.status_code == 403

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_delete_endpoint_soft_by_default(backend, tmp_path, monkeypatch):
    """DELETE /api/memories/{name} soft-deletes by default; ?hard=true hard-deletes."""
    from mori_advisor.main import delete_memory_rest

    _patch_policy(monkeypatch, "api")

    async def run(store):
        import json

        mem = store._mem if hasattr(store, "_mem") else store
        r = mem.write(name="del-soft-test", title="X", body="y")
        if asyncio.iscoroutine(r):
            await r

        with _actor_context(Actor("dreamer", "dreamer")):
            req = _fake_delete_request("del-soft-test", hard=False)
            resp = await delete_memory_rest(req)

        assert resp.status_code == 200
        data = json.loads(resp.body)
        assert data.get("status") == "soft_deleted"

        # Still in DB — restore is possible.
        final_name, msg = store.restore_memory("del-soft-test")
        if asyncio.iscoroutine((final_name, msg)):
            final_name, msg = await store.restore_memory("del-soft-test")
        assert final_name == "del-soft-test"

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_delete_endpoint_hard_purges(backend, tmp_path, monkeypatch):
    """DELETE /api/memories/{name}?hard=true permanently removes the row."""
    from mori_advisor.main import delete_memory_rest

    _patch_policy(monkeypatch, "api")

    async def run(store):
        import json

        mem = store._mem if hasattr(store, "_mem") else store
        r = mem.write(name="del-hard-test", title="X", body="y")
        if asyncio.iscoroutine(r):
            await r

        with _actor_context(Actor("dreamer", "dreamer")):
            req = _fake_delete_request("del-hard-test", hard=True)
            resp = await delete_memory_rest(req)

        assert resp.status_code == 200
        data = json.loads(resp.body)
        assert data.get("status") == "hard_deleted"

        # Cannot restore — gone.
        final_name, msg = store.restore_memory("del-hard-test")
        if asyncio.iscoroutine((final_name, msg)):
            final_name, msg = await store.restore_memory("del-hard-test")
        assert "not found" in msg.lower() or "not deleted" in msg.lower()

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── Postgres-only tests ──────────────────────────────────────────────────────


@requires_pg
def test_pg_soft_delete_hides_from_fts(tmp_path, monkeypatch):
    """Postgres tsvector search excludes tombstoned rows."""
    _patch_policy(monkeypatch, "api")
    unique_kw = "uniqueterm7f3a9b2c"

    async def run():
        store = await _make_pg_store()
        try:
            _apply_store(monkeypatch, store)
            await store.write(
                name="pg-fts-del",
                title="Postgres FTS test",
                body=unique_kw,
            )
            await store.soft_delete("pg-fts-del")

            results = await store.search_json(query=unique_kw)
            assert not any(r["name"] == "pg-fts-del" for r in results)
        finally:
            await store.pool.close()

    asyncio.run(run())


@requires_pg
def test_pg_partial_unique_allows_reuse(tmp_path, monkeypatch):
    """Postgres partial unique index allows a new active row to reuse a tombstoned name."""
    _patch_policy(monkeypatch, "api")

    async def run():
        store = await _make_pg_store()
        try:
            _apply_store(monkeypatch, store)
            await store.write(name="pg-reuse", title="Old", body="old")
            await store.soft_delete("pg-reuse")
            # Must not raise a unique-violation.
            await store.write(name="pg-reuse", title="New", body="new")
            m = await store.get_memory("pg-reuse")
            assert m is not None
            assert m["title"] == "New"
        finally:
            await store.pool.close()

    asyncio.run(run())
