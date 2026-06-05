"""POST /api/memories idempotency wire-in (#23 C).

Exercises post_memory through the Idempotency-Key path on BOTH backends. The
idempotency store itself is in-memory (backend-agnostic) and unit-tested in
test_throttle.py; here we prove the *route* behaviour: a replay returns the
cached response and the underlying write runs exactly once.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from mori_advisor.policy import Actor, current_actor
from mori_advisor.throttle import InMemoryIdempotencyStore

PG_URL = os.environ.get("MORI_TEST_DATABASE_URL", "")
requires_pg = pytest.mark.skipif(not PG_URL, reason="MORI_TEST_DATABASE_URL not set")

BACKENDS = ["sqlite"]
if PG_URL:
    BACKENDS.append("postgres")


# ── store harness (mirrors test_audit_softdelete.py) ─────────────────────────


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
    monkeypatch.setattr(m, "store", store)
    monkeypatch.setattr(m, "memory_store", memory_store)
    # Fresh idempotency store per test — module global must not leak across tests.
    monkeypatch.setattr(m, "idempotency_store", InMemoryIdempotencyStore())


def _run_with_backend(backend, tmp_path, monkeypatch, coro_fn):
    async def run():
        store = _make_sqlite_store(tmp_path) if backend == "sqlite" else await _make_pg_store()
        try:
            _apply_store(monkeypatch, store)
            return await coro_fn(store)
        finally:
            if hasattr(store, "pool") and store.pool:
                await store.pool.close()

    return asyncio.run(run())


def _patch_policy(monkeypatch, mode):
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


def _fake_post(payload: dict, idem_key: str | None = None):
    from starlette.datastructures import State
    from starlette.requests import Request

    raw = json.dumps(payload).encode()
    headers = [(b"content-type", b"application/json")]
    if idem_key is not None:
        headers.append((b"idempotency-key", idem_key.encode()))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/memories",
        "query_string": b"",
        "headers": headers,
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
    actor = current_actor.get()
    if actor is not None:
        req.state.actor = actor
    return req


async def _audit_rows(store, name):
    rows = store.get_audit_log(memory_name=name)
    if inspect.isawaitable(rows):
        rows = await rows
    return rows


# ── tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_no_key_behaves_normally(backend, tmp_path, monkeypatch):
    from mori_advisor.main import post_memory

    _patch_policy(monkeypatch, "api")

    async def run(store):
        with _actor_context(Actor("w", "write")):
            resp = await post_memory(_fake_post({"name": "plain", "body": "x"}))
        assert resp.status_code == 201
        assert json.loads(resp.body)["status"] == "created"
        assert "idempotency-replay" not in {k.lower() for k in resp.headers}

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_replay_returns_cached_and_writes_once(backend, tmp_path, monkeypatch):
    """The crux: replay returns the identical response AND the write runs once."""
    from mori_advisor.main import post_memory

    _patch_policy(monkeypatch, "api")

    async def run(store):
        payload = {"name": "idem-1", "title": "T", "body": "hello"}
        with _actor_context(Actor("w", "write")):
            first = await post_memory(_fake_post(payload, idem_key="K1"))
            second = await post_memory(_fake_post(payload, idem_key="K1"))

        assert first.status_code == 201
        assert second.status_code == 201  # replayed, same status
        assert second.body == first.body  # byte-identical cached body
        assert "idempotency-replay" in {k.lower() for k in second.headers}

        # The write ran exactly ONCE — only one propose_new audit row.
        rows = await _audit_rows(store, "idem-1")
        assert sum(1 for r in rows if r["op"] == "propose_new") == 1, rows

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_same_key_different_body_is_422(backend, tmp_path, monkeypatch):
    from mori_advisor.main import post_memory

    _patch_policy(monkeypatch, "api")

    async def run(store):
        with _actor_context(Actor("w", "write")):
            r1 = await post_memory(_fake_post({"name": "idem-2", "body": "a"}, idem_key="K2"))
            r2 = await post_memory(
                _fake_post({"name": "idem-2", "body": "DIFFERENT"}, idem_key="K2")
            )
        assert r1.status_code == 201
        assert r2.status_code == 422

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_in_progress_claim_is_409(backend, tmp_path, monkeypatch):
    """A second request while a claim is held (not completed) → 409 + Retry-After."""
    import mori_advisor.main as m
    from mori_advisor.main import post_memory

    _patch_policy(monkeypatch, "api")

    async def run(store):
        payload = {"name": "idem-3", "body": "z"}
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        # Pre-occupy the claim with the exact key+digest post_memory will compute.
        await m.idempotency_store.begin("w:K3", digest, 30, 3600)

        with _actor_context(Actor("w", "write")):
            resp = await post_memory(_fake_post(payload, idem_key="K3"))
        assert resp.status_code == 409
        assert "retry-after" in {k.lower() for k in resp.headers}

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_distinct_keys_are_independent(backend, tmp_path, monkeypatch):
    from mori_advisor.main import post_memory

    _patch_policy(monkeypatch, "api")

    async def run(store):
        with _actor_context(Actor("w", "write")):
            a = await post_memory(_fake_post({"name": "ka", "body": "1"}, idem_key="A"))
            b = await post_memory(_fake_post({"name": "kb", "body": "2"}, idem_key="B"))
        assert a.status_code == 201 and b.status_code == 201
        assert json.loads(a.body)["name"] == "ka"
        assert json.loads(b.body)["name"] == "kb"

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_same_key_distinct_actors_dont_collide(backend, tmp_path, monkeypatch):
    """Idempotency keys are scoped per actor — same key, different actors → separate."""
    from mori_advisor.main import post_memory

    _patch_policy(monkeypatch, "api")

    async def run(store):
        # alice creates 'shared' with key K; bob uses the SAME key K but a
        # different body — must NOT 422 (different actor scope), and because
        # 'shared' is now working owned by alice, bob's write is queued pending.
        with _actor_context(Actor("alice", "write")):
            ra = await post_memory(_fake_post({"name": "shared", "body": "a"}, idem_key="K"))
        with _actor_context(Actor("bob", "write")):
            rb = await post_memory(_fake_post({"name": "shared", "body": "b"}, idem_key="K"))
        assert ra.status_code == 201
        assert rb.status_code in (200, 202)  # not 422 — bob's key K is a separate scope

    _run_with_backend(backend, tmp_path, monkeypatch, run)
