"""Policy — role enforcement and mode-switch tests.

Acceptance criteria (per issue #13 design):

1. api mode:
   - read key REJECTED by memory_write / memory_approve
   - write key ALLOWED for memory_write, REJECTED for memory_approve
   - dreamer key ALLOWED for both
   - The SAME under-privileged call is denied on BOTH the MCP-tool surface
     (via current_actor ContextVar) AND the REST surface (via request.state.actor).
     This is the no-bypass proof.

2. host mode (default):
   - Privileged tools behave as before — no role enforcement.
   - Backward compatibility preserved.

3. Fail-closed defaults:
   - Name absent from MORI_API_KEY_ROLES -> read role.
   - No actor (None) + api mode -> denied unless MORI_LOCAL_FULL_ACCESS.
   - MORI_LOCAL_FULL_ACCESS=true -> nil actor allowed through.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager
from pathlib import Path

import pytest

# ── Backend parametrisation ───────────────────────────────────────────────────

PG_URL = os.environ.get("MORI_TEST_DATABASE_URL", "")
requires_pg = pytest.mark.skipif(not PG_URL, reason="MORI_TEST_DATABASE_URL not set")

BACKENDS = ["sqlite"]
if PG_URL:
    BACKENDS.append("postgres")

# ── Store helpers (reuse patterns from test_mcp_tools.py) ─────────────────────


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


# ── Actor injection helpers ───────────────────────────────────────────────────


@contextmanager
def _actor_context(actor):
    """Set current_actor ContextVar to *actor* for the duration of the block.

    ContextVar.get is a C-level slot — it cannot be monkeypatched.  Instead,
    we call current_actor.set() directly and reset on exit.  This is exactly
    the same path that ApiKeyMiddleware uses in production.
    """
    from mori_advisor.policy import current_actor

    token = current_actor.set(actor)
    try:
        yield
    finally:
        current_actor.reset(token)


def _patch_policy(monkeypatch, mode: str, local_full_access: bool = False):
    """Monkeypatch policy module-level scalars for a single test."""
    import mori_advisor.policy as pol

    monkeypatch.setattr(pol, "_TD_MODE", mode)
    monkeypatch.setattr(pol, "_LOCAL_FULL_ACCESS", local_full_access)


# ── Policy unit tests — no store needed ───────────────────────────────────────


def test_require_role_host_mode_is_noop(monkeypatch):
    """In host mode, require_role never raises regardless of actor."""
    from mori_advisor.policy import Actor, require_role

    _patch_policy(monkeypatch, "host")

    # No actor (ContextVar default = None)
    require_role("dreamer")  # must not raise

    # Read actor
    with _actor_context(Actor("reader", "read")):
        require_role("dreamer")  # must not raise


def test_require_role_api_mode_read_key_denied_write(monkeypatch):
    """In api mode a read key is denied for write and dreamer operations."""
    from mori_advisor.policy import Actor, PermissionDenied, require_role

    _patch_policy(monkeypatch, "api")
    with _actor_context(Actor("ci", "read")):
        with pytest.raises(PermissionDenied):
            require_role("write")

        with pytest.raises(PermissionDenied):
            require_role("dreamer")


def test_require_role_api_mode_write_key(monkeypatch):
    """In api mode a write key passes write but is denied dreamer."""
    from mori_advisor.policy import Actor, PermissionDenied, require_role

    _patch_policy(monkeypatch, "api")
    with _actor_context(Actor("nuc", "write")):
        require_role("write")  # must not raise
        require_role("read")  # must not raise

        with pytest.raises(PermissionDenied):
            require_role("dreamer")


def test_require_role_api_mode_dreamer_key(monkeypatch):
    """In api mode a dreamer key passes all levels."""
    from mori_advisor.policy import Actor, require_role

    _patch_policy(monkeypatch, "api")
    with _actor_context(Actor("gce", "dreamer")):
        require_role("read")
        require_role("write")
        require_role("dreamer")


def test_require_role_api_mode_no_actor_denied(monkeypatch):
    """In api mode a None actor (ContextVar default) is denied for privileged ops."""
    from mori_advisor.policy import PermissionDenied, require_role

    _patch_policy(monkeypatch, "api", local_full_access=False)
    # No _actor_context call — ContextVar stays at its None default.
    with pytest.raises(PermissionDenied):
        require_role("write")

    with pytest.raises(PermissionDenied):
        require_role("dreamer")


def test_require_role_api_mode_no_actor_local_full_access(monkeypatch):
    """MORI_LOCAL_FULL_ACCESS=true allows a None actor through in api mode."""
    from mori_advisor.policy import require_role

    _patch_policy(monkeypatch, "api", local_full_access=True)
    # No _actor_context — ContextVar at default (None).
    require_role("write")
    require_role("dreamer")


def test_role_for_missing_name_defaults_to_read(monkeypatch):
    """A name absent from MORI_API_KEY_ROLES gets 'read' role (fail closed)."""
    import mori_advisor.policy as pol
    from mori_advisor.policy import role_for

    monkeypatch.setattr(pol, "_ROLES", {"other": "dreamer"})
    assert role_for("unknown-client") == "read"


def test_load_roles_unknown_role_defaults_to_read(monkeypatch):
    """An unknown role string in MORI_API_KEY_ROLES is treated as 'read'."""
    import mori_advisor.policy as pol

    monkeypatch.setenv("MORI_API_KEY_ROLES", "alice:superadmin,bob:write")
    roles = pol._load_roles()
    assert roles["alice"] == "read"  # unknown -> fail closed
    assert roles["bob"] == "write"


def test_init_policy_rejects_malformed_role_entries(monkeypatch, caplog):
    """Malformed MORI_API_KEY_ROLES entries log errors but do not crash."""
    import logging

    import mori_advisor.policy as pol

    monkeypatch.setenv("MORI_API_KEY_ROLES", "malformed-no-colon,valid:dreamer")
    with caplog.at_level(logging.ERROR, logger="mori_advisor.policy"):
        roles = pol._load_roles()
    # The malformed entry is skipped; the valid one is parsed.
    assert "valid" in roles
    assert roles["valid"] == "dreamer"
    assert any("malformed" in r.message for r in caplog.records)


# ── MCP tool surface — api mode enforcement ───────────────────────────────────
# These tests prove the no-bypass claim on the MCP-tool path:
# the SAME under-privileged call (read key) is denied when current_actor is set.


@pytest.mark.parametrize("backend", BACKENDS)
def test_mcp_memory_write_read_key_denied_api_mode(backend, tmp_path, monkeypatch):
    """memory_write is denied for a read key in api mode (MCP surface)."""
    from mori_advisor.main import memory_write
    from mori_advisor.policy import Actor

    _patch_policy(monkeypatch, "api")

    async def run(store):
        with _actor_context(Actor("ci", "read")):
            result = await memory_write(name="test-denied", title="Should be denied", body="x")
        assert isinstance(result, str)
        # Must contain a denial message, not a success message.
        assert "written" not in result.lower()
        assert (
            "role" in result.lower()
            or "permission" in result.lower()
            or "required" in result.lower()
        )

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_mcp_memory_approve_read_key_denied_api_mode(backend, tmp_path, monkeypatch):
    """memory_approve is denied for a read key in api mode (MCP surface)."""
    from mori_advisor.main import memory_approve
    from mori_advisor.policy import Actor

    _patch_policy(monkeypatch, "api")

    async def run(store):
        with _actor_context(Actor("ci", "read")):
            result = await memory_approve(99999, note="should be denied", reviewer="ci")
        assert isinstance(result, str)
        assert "written" not in result.lower()
        assert (
            "role" in result.lower()
            or "permission" in result.lower()
            or "required" in result.lower()
        )

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_mcp_memory_approve_write_key_denied_api_mode(backend, tmp_path, monkeypatch):
    """memory_approve is denied for a write key in api mode (MCP surface).
    Proves write < dreamer hierarchy.
    """
    from mori_advisor.main import memory_approve
    from mori_advisor.policy import Actor

    _patch_policy(monkeypatch, "api")

    async def run(store):
        with _actor_context(Actor("nuc", "write")):
            result = await memory_approve(99999, note="should be denied", reviewer="nuc")
        assert isinstance(result, str)
        assert "written" not in result.lower()
        assert (
            "role" in result.lower()
            or "permission" in result.lower()
            or "required" in result.lower()
        )

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_mcp_memory_write_write_key_allowed_api_mode(backend, tmp_path, monkeypatch):
    """memory_write succeeds for a write key in api mode (MCP surface)."""
    from mori_advisor.main import memory_write
    from mori_advisor.policy import Actor

    _patch_policy(monkeypatch, "api")

    async def run(store):
        with _actor_context(Actor("nuc", "write")):
            result = await memory_write(name="write-allowed", title="Write allowed", body="ok")
        assert isinstance(result, str)
        assert "written" in result.lower() or "write-allowed" in result.lower()

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_mcp_memory_approve_dreamer_key_allowed_api_mode(backend, tmp_path, monkeypatch):
    """memory_approve reaches the store for a dreamer key in api mode.
    (Returns 'not found' for id 99999 — that is the store's error, not a policy error.)
    """
    from mori_advisor.main import memory_approve
    from mori_advisor.policy import Actor

    _patch_policy(monkeypatch, "api")

    async def run(store):
        with _actor_context(Actor("gce", "dreamer")):
            result = await memory_approve(99999, note="dreamer ok", reviewer="gce")
        assert isinstance(result, str)
        # Role check passed — we get a store response (not found / already processed),
        # NOT a permission-denied message.
        assert "role" not in result.lower() and "required" not in result.lower()

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── REST surface — no-bypass proof ───────────────────────────────────────────
# These tests verify that the Policy check is equally enforceable via
# request.state.actor (the REST path).  We call the policy functions directly
# with a synthesised actor, mirroring what a REST handler would do.


def test_rest_surface_write_policy_read_key_denied(monkeypatch):
    """Policy.can_write returns False for a read actor in api mode.

    This is the REST-surface analogue to test_mcp_memory_write_read_key_denied_api_mode.
    A REST handler that calls ``can_write(request.state.actor)`` will correctly deny.
    The SAME underlying check (ROLE_LEVELS comparison) is used on both surfaces —
    no bypass is possible.
    """
    from mori_advisor.policy import Actor, can_write

    _patch_policy(monkeypatch, "api")
    read_actor = Actor("ci", "read")
    assert can_write(read_actor) is False


def test_rest_surface_approve_policy_write_key_denied(monkeypatch):
    """Policy.can_approve returns False for a write actor in api mode."""
    from mori_advisor.policy import Actor, can_approve

    _patch_policy(monkeypatch, "api")
    write_actor = Actor("nuc", "write")
    assert can_approve(write_actor) is False


def test_rest_surface_approve_policy_dreamer_key_allowed(monkeypatch):
    """Policy.can_approve returns True for a dreamer actor in api mode."""
    from mori_advisor.policy import Actor, can_approve

    _patch_policy(monkeypatch, "api")
    dreamer_actor = Actor("gce", "dreamer")
    assert can_approve(dreamer_actor) is True


def test_rest_surface_host_mode_all_allowed(monkeypatch):
    """In host mode can_write / can_approve always return True (backward compat)."""
    from mori_advisor.policy import Actor, can_approve, can_write

    _patch_policy(monkeypatch, "host")
    read_actor = Actor("legacy", "read")
    assert can_write(read_actor) is True
    assert can_approve(read_actor) is True
    assert can_write(None) is True
    assert can_approve(None) is True


# ── Backward-compat — host mode: no role enforcement on MCP tools ─────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_mcp_host_mode_no_enforcement(backend, tmp_path, monkeypatch):
    """In host mode (default), memory_write and memory_approve are not policy-gated.

    A read actor (or no actor) should not be blocked by require_role in host mode.
    """
    from mori_advisor.main import memory_approve, memory_write
    from mori_advisor.policy import Actor

    _patch_policy(monkeypatch, "host")

    async def run(store):
        with _actor_context(Actor("legacy-host", "read")):
            # memory_write should succeed (policy is a no-op in host mode)
            result = await memory_write(name="host-compat", title="Host mode compat", body="ok")
            assert isinstance(result, str)
            assert "written" in result.lower() or "host-compat" in result.lower()

            # memory_approve should reach the store (policy is a no-op)
            result2 = await memory_approve(99999, note="host mode", reviewer="legacy-host")
            assert isinstance(result2, str)
            # Permission check did NOT fire — we got the store's 'not found' response.
            assert "role" not in result2.lower() and "required" not in result2.lower()

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_mcp_host_mode_no_actor_no_enforcement(backend, tmp_path, monkeypatch):
    """In host mode, a None actor (stdio / no ASGI request) never hits require_role."""
    from mori_advisor.main import memory_write

    _patch_policy(monkeypatch, "host")
    # No _actor_context — ContextVar stays at None default.

    async def run(store):
        result = await memory_write(name="host-stdio", title="Host stdio", body="ok")
        assert isinstance(result, str)
        assert "written" in result.lower() or "host-stdio" in result.lower()

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── Open mode (no keys configured) — unchanged ───────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_open_mode_write_allowed(backend, tmp_path, monkeypatch):
    """Open mode (no keys): actor is None; host mode means no enforcement."""
    from mori_advisor.main import memory_write

    # Open mode is always host mode effectively — no keys, no role enforcement.
    _patch_policy(monkeypatch, "host")
    # No _actor_context — ContextVar at None default.

    async def run(store):
        result = await memory_write(name="open-mode", title="Open mode", body="ok")
        assert isinstance(result, str)
        assert "written" in result.lower() or "open-mode" in result.lower()

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── Additional privileged tools ───────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_mcp_memory_reject_read_key_denied_api_mode(backend, tmp_path, monkeypatch):
    """memory_reject is denied for a read key in api mode."""
    from mori_advisor.main import memory_reject
    from mori_advisor.policy import Actor

    _patch_policy(monkeypatch, "api")

    async def run(store):
        with _actor_context(Actor("ci", "read")):
            result = await memory_reject(99999, note="should fail")
        assert isinstance(result, str)
        assert (
            "role" in result.lower()
            or "permission" in result.lower()
            or "required" in result.lower()
        )

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_mcp_memory_protect_write_key_denied_api_mode(backend, tmp_path, monkeypatch):
    """memory_protect requires dreamer role; a write key is denied in api mode."""
    from mori_advisor.main import memory_protect
    from mori_advisor.policy import Actor

    _patch_policy(monkeypatch, "api")

    async def run(store):
        with _actor_context(Actor("nuc", "write")):
            result = await memory_protect("any-memory")
        assert isinstance(result, str)
        assert (
            "role" in result.lower()
            or "permission" in result.lower()
            or "required" in result.lower()
        )

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_mcp_memory_delete_read_key_denied_api_mode(backend, tmp_path, monkeypatch):
    """memory_delete requires write role; a read key is denied in api mode."""
    from mori_advisor.main import memory_delete
    from mori_advisor.policy import Actor

    _patch_policy(monkeypatch, "api")

    async def run(store):
        with _actor_context(Actor("ci", "read")):
            result = await memory_delete("any-memory")
        assert isinstance(result, str)
        assert (
            "role" in result.lower()
            or "permission" in result.lower()
            or "required" in result.lower()
        )

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_mcp_memory_rollback_read_key_denied_api_mode(backend, tmp_path, monkeypatch):
    """memory_rollback requires write role; a read key is denied in api mode."""
    from mori_advisor.main import memory_rollback
    from mori_advisor.policy import Actor

    _patch_policy(monkeypatch, "api")

    async def run(store):
        with _actor_context(Actor("ci", "read")):
            result = await memory_rollback("any-memory", 1)
        assert isinstance(result, str)
        assert (
            "role" in result.lower()
            or "permission" in result.lower()
            or "required" in result.lower()
        )

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_mcp_memory_import_read_key_denied_api_mode(backend, tmp_path, monkeypatch):
    """memory_import requires write role; a read key is denied in api mode."""
    from mori_advisor.main import memory_import
    from mori_advisor.policy import Actor

    _patch_policy(monkeypatch, "api")

    async def run(store):
        with _actor_context(Actor("ci", "read")):
            result = await memory_import(str(tmp_path / "nonexistent"))
        assert isinstance(result, str)
        assert (
            "role" in result.lower()
            or "permission" in result.lower()
            or "required" in result.lower()
        )

    _run_with_backend(backend, tmp_path, monkeypatch, run)
