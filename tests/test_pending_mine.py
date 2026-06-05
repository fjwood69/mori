"""Tests for GET /api/pending/mine — agent self-view of own proposals (#16 prereq).

Acceptance criteria:

1. A write-role actor sees ONLY its own proposals (not another actor's).
2. A read-role actor is denied (403).
3. No actor (ContextVar unset in api mode) returns an empty list (not a 500).
4. The status filter works: passing "pending" narrows results; omitting returns all.
5. The existing GET /api/pending/json (dreamer) behaviour is not regressed.

Both SQLite (always) and Postgres (if MORI_TEST_DATABASE_URL is set) via @requires_pg.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from mori_advisor.policy import Actor, current_actor

# ── Backend parametrisation ───────────────────────────────────────────────────

PG_URL = os.environ.get("MORI_TEST_DATABASE_URL", "")
requires_pg = pytest.mark.skipif(not PG_URL, reason="MORI_TEST_DATABASE_URL not set")

BACKENDS = ["sqlite"]
if PG_URL:
    BACKENDS.append("postgres")


# ── Store / module helpers ────────────────────────────────────────────────────


async def _a(val):
    return await val if inspect.isawaitable(val) else val


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


def _fake_get_request(query_string: bytes = b""):
    """Build a minimal Starlette GET Request for testing the /api/pending/mine handler."""
    from starlette.datastructures import State
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/pending/mine",
        "query_string": query_string,
        "headers": [],
        "path_params": {},
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


# ── Helper: seed a pending write for a named proposer ────────────────────────


async def _seed_pending(store, name: str, title: str, proposer: str) -> None:
    """Queue a pending write attributed to *proposer*."""
    await _a(
        store.queue_pending_write(
            name=name,
            title=title,
            body=f"body for {name}",
            tier="canonical",
            source="test",
            proposed_by=proposer,
        )
    )


# ── 1. Actor sees only its own proposals ──────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_write_actor_sees_only_own_proposals(backend, tmp_path, monkeypatch):
    """A write-role actor must see only rows it proposed — not another actor's rows."""
    from mori_advisor.main import get_pending_mine

    _patch_policy(monkeypatch, "api")

    async def run(store):
        # Seed proposals from two different proposers.
        await _seed_pending(store, "agent-alpha-mem", "Alpha memory", proposer="agent-alpha")
        await _seed_pending(store, "agent-beta-mem", "Beta memory", proposer="agent-beta")

        with _actor_context(Actor("agent-alpha", "write")):
            req = _fake_get_request()
            resp = await get_pending_mine(req)

        assert resp.status_code == 200
        data = json.loads(resp.body)
        assert data["proposer"] == "agent-alpha"
        assert data["count"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["name"] == "agent-alpha-mem"
        # Must not contain the other actor's row.
        assert all(item["proposed_by"] == "agent-alpha" for item in data["items"])

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@requires_pg
def test_write_actor_sees_only_own_proposals_pg(tmp_path, monkeypatch):
    """Postgres variant — write actor sees only its own proposals."""
    test_write_actor_sees_only_own_proposals("postgres", tmp_path, monkeypatch)


# ── 2. Read-role actor is denied (403) ───────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_read_actor_denied(backend, tmp_path, monkeypatch):
    """A read-role actor must receive 403 from GET /api/pending/mine."""
    from mori_advisor.main import get_pending_mine

    _patch_policy(monkeypatch, "api")

    async def run(store):
        with _actor_context(Actor("ci-reader", "read")):
            req = _fake_get_request()
            resp = await get_pending_mine(req)

        assert resp.status_code == 403
        data = json.loads(resp.body)
        assert "forbidden" in data.get("error", "").lower() or "role" in str(data).lower()

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@requires_pg
def test_read_actor_denied_pg(tmp_path, monkeypatch):
    """Postgres variant — read actor is denied."""
    test_read_actor_denied("postgres", tmp_path, monkeypatch)


# ── 3. No actor → empty list (not a 500) ─────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_no_actor_returns_empty_list(backend, tmp_path, monkeypatch):
    """In host mode (no enforcement) a missing actor should return an empty list."""
    from mori_advisor.main import get_pending_mine

    # Use host mode so require_role passes even without an actor.
    _patch_policy(monkeypatch, "host")

    async def run(store):
        # current_actor is None (ContextVar default — no _actor_context wrapper).
        assert current_actor.get() is None
        req = _fake_get_request()
        resp = await get_pending_mine(req)

        assert resp.status_code == 200
        data = json.loads(resp.body)
        assert data["proposer"] == ""
        assert data["count"] == 0
        assert data["items"] == []

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@requires_pg
def test_no_actor_returns_empty_list_pg(tmp_path, monkeypatch):
    """Postgres variant — missing actor returns empty list."""
    test_no_actor_returns_empty_list("postgres", tmp_path, monkeypatch)


# ── 4. Status filter ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_status_filter_pending_only(backend, tmp_path, monkeypatch):
    """?status=pending returns only pending rows; approved rows are excluded."""
    from mori_advisor.main import get_pending_mine

    _patch_policy(monkeypatch, "api")

    async def run(store):
        mem = store._mem if hasattr(store, "_mem") else store

        # Seed a pending proposal and immediately approve it.
        await _seed_pending(store, "approved-mem", "Will be approved", proposer="agent-gamma")
        items = await _a(store.pending_list_json(status="pending", proposed_by="agent-gamma"))
        write_id = items[0]["id"]
        await _a(mem.approve(write_id, note="ok", reviewer="td"))

        # Seed a second proposal that stays pending.
        await _seed_pending(store, "still-pending", "Stays pending", proposer="agent-gamma")

        with _actor_context(Actor("agent-gamma", "write")):
            # No status filter → all statuses.
            req_all = _fake_get_request(b"")
            resp_all = await get_pending_mine(req_all)
            data_all = json.loads(resp_all.body)

            # status=pending → pending rows only.
            req_pending = _fake_get_request(b"status=pending")
            resp_pending = await get_pending_mine(req_pending)
            data_pending = json.loads(resp_pending.body)

        assert data_all["status"] == "all"
        all_names = {i["name"] for i in data_all["items"]}
        assert "approved-mem" in all_names
        assert "still-pending" in all_names

        assert data_pending["status"] == "pending"
        pending_names = {i["name"] for i in data_pending["items"]}
        assert "approved-mem" not in pending_names
        assert "still-pending" in pending_names

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@requires_pg
def test_status_filter_pending_only_pg(tmp_path, monkeypatch):
    """Postgres variant — status filter works correctly."""
    test_status_filter_pending_only("postgres", tmp_path, monkeypatch)


# ── 5. GET /api/pending/json (dreamer) is not regressed ──────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_get_pending_json_dreamer_not_regressed(backend, tmp_path, monkeypatch):
    """GET /api/pending/json with dreamer role must still return ALL proposers' rows."""
    from mori_advisor.main import get_pending_json

    _patch_policy(monkeypatch, "api")

    async def run(store):
        await _seed_pending(store, "dreamer-alpha", "Alpha", proposer="alpha")
        await _seed_pending(store, "dreamer-beta", "Beta", proposer="beta")

        with _actor_context(Actor("td-reviewer", "dreamer")):
            scope = {
                "type": "http",
                "method": "GET",
                "path": "/api/pending/json",
                "query_string": b"status=pending",
                "headers": [],
                "path_params": {},
            }
            from starlette.datastructures import State
            from starlette.requests import Request

            class _Recv:
                async def __call__(self):
                    return {"type": "http.disconnect"}

            req = Request(scope, receive=_Recv())
            req._state = State()
            req.state.actor = Actor("td-reviewer", "dreamer")
            resp = await get_pending_json(req)

        assert resp.status_code == 200
        data = json.loads(resp.body)
        names = {i["name"] for i in data["items"]}
        # Both proposers' rows must be visible to the dreamer.
        assert "dreamer-alpha" in names
        assert "dreamer-beta" in names

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@requires_pg
def test_get_pending_json_dreamer_not_regressed_pg(tmp_path, monkeypatch):
    """Postgres variant — dreamer review queue not regressed."""
    test_get_pending_json_dreamer_not_regressed("postgres", tmp_path, monkeypatch)
