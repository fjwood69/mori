"""MCP tool coverage across SQLite and Postgres backends.

Exercises every store-touching MCP tool in ``mori_advisor/main.py`` to catch
the class of bug where a tool works on SQLite but crashes or leaks a coroutine
on Postgres.

**Motivating bug (issue #12):** ``memory_req`` raised
``TypeError: 'coroutine' object is not iterable`` on Postgres because
``PostgresStore.parse_tags`` was ``async def`` with no awaits — the caller
iterated the coroutine directly.  SQLite's ``parse_tags`` is sync so SQLite
passed.  Nothing tested the MCP tool surface against Postgres.  Fixed in
v2.1.32 / commit 07b0e18.

Design:
- Direct invocation of the tool functions in ``mori_advisor/main.py``.  The
  transport (MCP HTTP/SSE) is **not** driven.
- Store globals are swapped via ``monkeypatch.setattr`` for each parametrised
  backend.
- Core assertion: ``assert_no_coroutines()`` — a recursive helper that
  traverses the return value and fails if any leaf is an awaitable.  This is
  the primary check; it would have caught issue #12.
- External services (NATS, LLM/bifrost, ingestion pipeline) are monkeypatched
  at the module boundary so the store-interaction + arg-parsing path still
  runs and the coroutine scan still applies.

# TODO: refactor store as a proper dependency injection target so the global
# monkeypatch approach can be retired.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path
from typing import Any

import pytest

# ── Backend parametrisation ───────────────────────────────────────────────────

PG_URL = os.environ.get("MORI_TEST_DATABASE_URL", "")
requires_pg = pytest.mark.skipif(not PG_URL, reason="MORI_TEST_DATABASE_URL not set")

BACKENDS = ["sqlite"]
if PG_URL:
    BACKENDS.append("postgres")


# ── Core helper: recursive coroutine / async-generator scan ──────────────────


def assert_no_coroutines(obj: Any, path: str = "result") -> None:
    """Recursively traverse *obj* and fail if any leaf is a coroutine or async
    generator.

    This is the acceptance criterion for the test suite: a coroutine
    appearing anywhere in the return value means the caller forgot an
    ``await`` — exactly the bug in issue #12.
    """
    if inspect.isawaitable(obj):
        raise AssertionError(
            f"Unawaited coroutine found at {path}: {obj!r}\n"
            "A tool function returned a coroutine instead of a plain value. "
            "This is the exact bug class described in issue #12."
        )
    if inspect.isasyncgen(obj):
        raise AssertionError(f"Async generator found at {path}: {obj!r}")
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert_no_coroutines(v, path=f"{path}[{k!r}]")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            assert_no_coroutines(v, path=f"{path}[{i}]")


# ── Store / globals bootstrap ─────────────────────────────────────────────────


def _make_sqlite_store(tmp_path: Path):
    """Create and bootstrap a fresh SQLiteStore backed by *tmp_path*."""
    from mori_advisor.store import get_store

    s = get_store(tmp_path / "memories.db")
    s.bootstrap()
    return s


async def _make_pg_store():
    """Create and bootstrap a fresh PostgresStore, then TRUNCATE all tables so
    tests start from a clean slate without recreating the schema on every run.
    """
    from mori_advisor.store.postgres_store import PostgresStore

    s = PostgresStore(PG_URL)
    await s.bootstrap()
    # Wipe all rows for isolation; preserve schema.
    async with s.pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE memories, memory_versions, pending_writes, "
            "eviction_queue, ingestion_log, session_events, "
            "dream_state, dreamer_config, msg_log CASCADE"
        )
    return s


def _derived_globals(store):
    """Return (memory_store, session_log) mirrors of what main.py computes."""
    memory_store = store._mem if hasattr(store, "_mem") else store
    session_log = store._log if hasattr(store, "_log") else store
    return memory_store, session_log


def _apply_store(monkeypatch, store):
    """Monkeypatch the module-level store globals in mori_advisor.main."""
    import mori_advisor.main as m

    memory_store, session_log = _derived_globals(store)
    monkeypatch.setattr(m, "store", store)
    monkeypatch.setattr(m, "memory_store", memory_store)
    monkeypatch.setattr(m, "session_log", session_log)


# ── Shared fixture ────────────────────────────────────────────────────────────


@pytest.fixture
def sqlite_store(tmp_path):
    return _make_sqlite_store(tmp_path)


@pytest.fixture
def pg_store():
    if not PG_URL:
        pytest.skip("MORI_TEST_DATABASE_URL not set")
    return asyncio.run(_make_pg_store())


# ── Seed helpers ──────────────────────────────────────────────────────────────


async def _seed_memory(store, name="test-mem", **kwargs):
    """Write a minimal memory into *store*.  Works for both backends."""
    defaults = dict(
        title="Test memory",
        body="Test body content",
        type="project",
        tier="working",
        tags=["test"],
    )
    defaults.update(kwargs)
    result = store.write(name=name, **defaults)
    if inspect.isawaitable(result):
        result = await result
    return result


async def _seed_requirement(store, name="req-test"):
    """Write a requirement memory (needed by memory_req)."""
    result = store.write(
        name=name,
        title="Test requirement",
        body="FR: must work on both backends",
        type="requirement",
        tier="working",
        tags=["project-test", "status-pending", "fr"],
    )
    if inspect.isawaitable(result):
        result = await result
    return result


# ── Parametrised tool tests ───────────────────────────────────────────────────
#
# Each test is parametrised over *backend* ("sqlite" | "postgres").  The test
# body is backend-agnostic: it calls ``asyncio.run()`` on an inner async
# function that (1) obtains the right store, (2) monkeypatches globals,
# (3) invokes the tool, (4) runs the coroutine scan, (5) does light shape checks.


def _run_with_backend(backend: str, tmp_path: Path, monkeypatch, coro_fn):
    """Set up the correct store for *backend*, run *coro_fn(store)* via
    asyncio.run(), and tear down."""

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


# ── memory_write ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_memory_write(backend, tmp_path, monkeypatch):
    """memory_write must return a str on both backends."""
    from mori_advisor.main import memory_write

    async def run(store):
        result = await memory_write(
            name="write-test",
            title="Write test",
            body="Hello world",
            type="project",
            tier="working",
            tags=["test"],
        )
        assert_no_coroutines(result)
        assert isinstance(result, str)
        return result

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── memory_read ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_memory_read(backend, tmp_path, monkeypatch):
    """memory_read must return a str (hits and misses) on both backends."""
    from mori_advisor.main import memory_read

    async def run(store):
        await _seed_memory(store, name="read-test")

        result = await memory_read("read-test")
        assert_no_coroutines(result)
        assert isinstance(result, str)
        assert "read-test" in result

        miss = await memory_read("does-not-exist")
        assert_no_coroutines(miss)
        assert isinstance(miss, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── memory_list ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_memory_list(backend, tmp_path, monkeypatch):
    """memory_list must return a str on both backends."""
    from mori_advisor.main import memory_list

    async def run(store):
        await _seed_memory(store, name="list-test", tags=["infra"])

        result = await memory_list(tag="infra")
        assert_no_coroutines(result)
        assert isinstance(result, str)

        # Unfiltered list
        all_result = await memory_list()
        assert_no_coroutines(all_result)
        assert isinstance(all_result, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── memory_search ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_memory_search(backend, tmp_path, monkeypatch):
    """memory_search must return a str on both backends."""
    from mori_advisor.main import memory_search

    async def run(store):
        await _seed_memory(store, name="search-test", body="peculiar deployment quirk")

        result = await memory_search(query="peculiar")
        assert_no_coroutines(result)
        assert isinstance(result, str)

        empty = await memory_search(query="zzznomatch")
        assert_no_coroutines(empty)
        assert isinstance(empty, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── memory_req (regression for issue #12) ────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_memory_req(backend, tmp_path, monkeypatch):
    """memory_req must not crash on Postgres (regression for issue #12).

    This is the primary acceptance criterion: the coroutine scan applied to
    the return value would trip if parse_tags returned a coroutine.
    """
    from mori_advisor.main import memory_req

    async def run(store):
        await _seed_requirement(store, name="req-issue12")

        result = await memory_req(project="test")
        assert_no_coroutines(result)
        assert isinstance(result, str)
        # Either the table header or "No requirements found."
        assert "req-issue12" in result or "requirement" in result.lower() or "No" in result

        # Call with no filters — should also not crash
        all_result = await memory_req()
        assert_no_coroutines(all_result)
        assert isinstance(all_result, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── memory_history ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_memory_history(backend, tmp_path, monkeypatch):
    """memory_history must return a str after a seed write."""
    from mori_advisor.main import memory_history

    async def run(store):
        await _seed_memory(store, name="hist-test")

        result = await memory_history("hist-test")
        assert_no_coroutines(result)
        assert isinstance(result, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── memory_diff ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_memory_diff(backend, tmp_path, monkeypatch):
    """memory_diff must return a str; with invalid version IDs it returns an
    error string rather than raising."""
    from mori_advisor.main import memory_diff

    async def run(store):
        await _seed_memory(store, name="diff-test", body="first body")
        # Overwrite to generate a second version
        v2 = store.write(
            name="diff-test",
            title="Diff test",
            body="second body",
            type="project",
            tier="working",
            tags=[],
        )
        if inspect.isawaitable(v2):
            await v2

        result = await memory_diff("diff-test", 1, 2)
        assert_no_coroutines(result)
        assert isinstance(result, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── memory_rollback ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_memory_rollback(backend, tmp_path, monkeypatch):
    """memory_rollback must return a str (pass or graceful error)."""
    from mori_advisor.main import memory_rollback

    async def run(store):
        await _seed_memory(store, name="rollback-test")

        result = await memory_rollback("rollback-test", 1)
        assert_no_coroutines(result)
        assert isinstance(result, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── memory_export ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_memory_export(backend, tmp_path, monkeypatch):
    """memory_export must return a str and write a file to the given path."""
    from mori_advisor.main import memory_export

    async def run(store):
        await _seed_memory(store, name="export-test")
        out_path = str(tmp_path / "export-test.md")

        result = await memory_export("export-test", output_path=out_path)
        assert_no_coroutines(result)
        assert isinstance(result, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── memory_import ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_memory_import(backend, tmp_path, monkeypatch):
    """memory_import must return a str (even when the source directory is empty)."""
    from mori_advisor.main import memory_import

    import_dir = tmp_path / "imports"
    import_dir.mkdir()
    # Write a minimal .md file with YAML frontmatter
    (import_dir / "sample-import.md").write_text(
        "---\nname: sample-import\ntitle: Sample Import\ntype: project\ntier: working\ntags: []\n---\nBody content.\n",
        encoding="utf-8",
    )

    async def run(store):
        result = await memory_import(str(import_dir))
        assert_no_coroutines(result)
        assert isinstance(result, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── memory_pending_list / memory_approve / memory_reject ─────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_memory_pending_list(backend, tmp_path, monkeypatch):
    """memory_pending_list must return a str on both backends."""
    from mori_advisor.main import memory_pending_list

    async def run(store):
        result = await memory_pending_list()
        assert_no_coroutines(result)
        assert isinstance(result, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_memory_approve_reject_nonexistent(backend, tmp_path, monkeypatch):
    """memory_approve and memory_reject must return a str even for unknown IDs."""
    from mori_advisor.main import memory_approve, memory_reject

    async def run(store):
        app_result = await memory_approve(99999, note="test", reviewer="test-host")
        assert_no_coroutines(app_result)
        assert isinstance(app_result, str)

        rej_result = await memory_reject(99999, note="test", reviewer="test-host")
        assert_no_coroutines(rej_result)
        assert isinstance(rej_result, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── memory_protect ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_memory_protect(backend, tmp_path, monkeypatch):
    """memory_protect must return a str on both backends."""
    from mori_advisor.main import memory_protect

    async def run(store):
        await _seed_memory(store, name="protect-test")

        result = await memory_protect("protect-test")
        assert_no_coroutines(result)
        assert isinstance(result, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── memory_session_summary ────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_memory_session_summary(backend, tmp_path, monkeypatch):
    """memory_session_summary must return a str for any session UUID."""
    from mori_advisor.main import memory_session_summary

    async def run(store):
        result = await memory_session_summary("00000000-0000-0000-0000-000000000001")
        assert_no_coroutines(result)
        assert isinstance(result, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── memory_export_all ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_memory_export_all(backend, tmp_path, monkeypatch):
    """memory_export_all must return a str and not raise on an empty store."""
    from mori_advisor.main import memory_export_all

    async def run(store):
        await _seed_memory(store, name="exportall-test")
        out_dir = str(tmp_path / "exports")

        result = await memory_export_all(output_dir=out_dir)
        assert_no_coroutines(result)
        assert isinstance(result, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── dream_status ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_dream_status(backend, tmp_path, monkeypatch):
    """dream_status must return a str and its dream_pipeline must use the
    patched store globals."""
    import mori_advisor.main as m
    from mori_advisor.dream import DreamPipeline
    from mori_advisor.main import dream_status

    async def run(store):
        # Rebuild dream_pipeline with the patched store
        monkeypatch.setattr(
            m,
            "dream_pipeline",
            DreamPipeline(
                db_path=tmp_path / "memories.db",
                bifrost_client=m.bifrost,
                store=store,
            ),
        )

        result = await dream_status()
        assert_no_coroutines(result)
        assert isinstance(result, str)
        assert "Dream State" in result

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── standards_reload ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_standards_reload(backend, tmp_path, monkeypatch, tmp_path_factory):
    """standards_reload must return a str even when MORI_STANDARDS_DIR is
    absent (returns a 'not configured' message) or when the directory is empty.
    """
    import mori_advisor.main as m
    from mori_advisor.main import standards_reload

    async def run(store):
        # Unset standards dir → graceful 'not configured' message
        monkeypatch.setattr(m, "STANDARDS_DIR", "")
        result = await standards_reload()
        assert_no_coroutines(result)
        assert isinstance(result, str)

        # Set to an empty temp dir → 'Imported 0 standards'
        standards_dir = tmp_path_factory.mktemp("standards")
        monkeypatch.setattr(m, "STANDARDS_DIR", str(standards_dir))
        result2 = await standards_reload()
        assert_no_coroutines(result2)
        assert isinstance(result2, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── key_generate ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_key_generate(backend, tmp_path, monkeypatch):
    """key_generate must return a str containing the name (no store interaction
    beyond import, but we still apply the store to catch any future regressions)."""
    from mori_advisor.main import key_generate

    async def run(store):
        result = await key_generate("test-client")
        assert_no_coroutines(result)
        assert isinstance(result, str)
        assert "test-client" in result

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── brief ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_brief_unscoped(backend, tmp_path, monkeypatch):
    """brief (unscoped) must return a str and not leak coroutines."""
    import mori_advisor.main as m
    from mori_advisor.dream import DreamPipeline
    from mori_advisor.main import brief

    async def run(store):
        await _seed_memory(store, name="brief-seed")

        # Rebuild dream_pipeline with the patched store
        monkeypatch.setattr(
            m,
            "dream_pipeline",
            DreamPipeline(
                db_path=tmp_path / "memories.db",
                bifrost_client=m.bifrost,
                store=store,
            ),
        )

        # Stub out check_freshness (would call bifrost LLM)
        memory_store, _ = _derived_globals(store)
        monkeypatch.setattr(
            memory_store,
            "check_freshness",
            lambda llm_consult, limit=20: {
                "checked": 0,
                "fresh": 0,
                "stale": 0,
                "no": 0,
                "errors": 0,
            },
        )

        result = await brief(
            project=None, include_global=True, include_index=True, post_compact=False
        )
        assert_no_coroutines(result)
        assert isinstance(result, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_brief_scoped(backend, tmp_path, monkeypatch):
    """brief with project scope must return a str."""
    import mori_advisor.main as m
    from mori_advisor.dream import DreamPipeline
    from mori_advisor.main import brief

    async def run(store):
        await _seed_memory(store, name="brief-scoped-seed", tags=["project:mori"])

        monkeypatch.setattr(
            m,
            "dream_pipeline",
            DreamPipeline(
                db_path=tmp_path / "memories.db",
                bifrost_client=m.bifrost,
                store=store,
            ),
        )

        memory_store, _ = _derived_globals(store)
        monkeypatch.setattr(
            memory_store,
            "check_freshness",
            lambda llm_consult, limit=20: {
                "checked": 0,
                "fresh": 0,
                "stale": 0,
                "no": 0,
                "errors": 0,
            },
        )

        result = await brief(
            project="mori", include_global=True, include_index=False, post_compact=False
        )
        assert_no_coroutines(result)
        assert isinstance(result, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


def _stub_brief_env(m, monkeypatch, store, tmp_path):
    from mori_advisor.dream import DreamPipeline

    monkeypatch.setattr(
        m,
        "dream_pipeline",
        DreamPipeline(db_path=tmp_path / "memories.db", bifrost_client=m.bifrost, store=store),
    )
    memory_store, _ = _derived_globals(store)
    monkeypatch.setattr(
        memory_store,
        "check_freshness",
        lambda llm_consult, limit=20: {"checked": 0, "fresh": 0, "stale": 0, "no": 0, "errors": 0},
    )


@pytest.mark.parametrize("backend", BACKENDS)
def test_brief_scoped_safe_blocks_type_global_leak(backend, tmp_path, monkeypatch):
    """Provenance: a memory mistyped 'pattern' (no explicit scope tag) leaks into EVERY
    project's brief via the legacy type-auto-global. Safe scope must exclude it; the
    out-of-project body must never surface during work on a different project."""
    import mori_advisor.main as m
    from mori_advisor.main import brief

    async def run(store):
        await _seed_memory(store, name="this-proj", tags=["project:mori"])
        # origin-bound knowledge mistyped as a transferable 'pattern' — the cross-project leak
        await _seed_memory(store, name="leaky-pattern", type="pattern", tags=[])
        _stub_brief_env(m, monkeypatch, store, tmp_path)

        legacy = await brief(project="mori", scope="all")
        assert "leaky-pattern" in legacy, "legacy auto-globalizes type=pattern (the leak we close)"

        safe = await brief(project="mori", scope="safe")
        assert "leaky-pattern" not in safe, (
            "safe scope must NOT surface a mistyped-pattern cross-project"
        )

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_brief_unscoped_safe_global_only(backend, tmp_path, monkeypatch):
    """Provenance: unscoped brief in safe scope surfaces the global lane only; project-bound
    memories are withheld (closes the unscoped leak)."""
    import mori_advisor.main as m
    from mori_advisor.main import brief

    async def run(store):
        await _seed_memory(store, name="proj-only", tags=["project:mori"])
        await _seed_memory(store, name="glob-mem", tags=["scope:global"])
        _stub_brief_env(m, monkeypatch, store, tmp_path)

        safe = await brief(project=None, scope="safe")
        assert "glob-mem" in safe, "global lane must surface"
        assert "proj-only" not in safe, "project-bound memory leaked in unscoped safe brief"
        assert "withheld" in safe.lower()

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_brief_post_compact(backend, tmp_path, monkeypatch):
    """brief with post_compact=True must return a str."""
    import mori_advisor.main as m
    from mori_advisor.dream import DreamPipeline
    from mori_advisor.main import brief

    async def run(store):
        monkeypatch.setattr(
            m,
            "dream_pipeline",
            DreamPipeline(
                db_path=tmp_path / "memories.db",
                bifrost_client=m.bifrost,
                store=store,
            ),
        )

        result = await brief(post_compact=True, since="6h")
        assert_no_coroutines(result)
        assert isinstance(result, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── pensieve ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_pensieve_search(backend, tmp_path, monkeypatch):
    """pensieve (search) must return a str on both backends."""
    from mori_advisor.main import pensieve

    async def run(store):
        await _seed_memory(store, name="pensieve-seed", body="quadlet deployment")

        result = await pensieve(query="quadlet")
        assert_no_coroutines(result)
        assert isinstance(result, str)

        # 'read <name>' path
        read_result = await pensieve(query="read pensieve-seed")
        assert_no_coroutines(read_result)
        assert isinstance(read_result, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── memory_delete ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_memory_delete(backend, tmp_path, monkeypatch):
    """memory_delete must return a str on both backends."""
    from mori_advisor.main import memory_delete

    async def run(store):
        await _seed_memory(store, name="delete-me")

        result = await memory_delete("delete-me")
        assert_no_coroutines(result)
        assert isinstance(result, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── mori_ingest_status ────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_mori_ingest_status(backend, tmp_path, monkeypatch):
    """mori_ingest_status must return a str on both backends (empty log is OK)."""
    from mori_advisor.main import mori_ingest_status

    async def run(store):
        result = await mori_ingest_status()
        assert_no_coroutines(result)
        assert isinstance(result, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── msg_recv / msg_thread (store-backed via DATA_DIR / msg.db) ────────────────
#
# msg_recv and msg_thread create their own MsgStore from DATA_DIR.  We patch
# DATA_DIR to point at tmp_path so they create a fresh DB there — no NATS
# interaction involved.


@pytest.mark.parametrize("backend", BACKENDS)
def test_msg_recv(backend, tmp_path, monkeypatch):
    """msg_recv must return a str even when the message log is empty."""
    import mori_advisor.main as m
    from mori_advisor.main import msg_recv

    async def run(store):
        monkeypatch.setattr(m, "DATA_DIR", tmp_path)

        result = await msg_recv()
        assert_no_coroutines(result)
        assert isinstance(result, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_msg_thread(backend, tmp_path, monkeypatch):
    """msg_thread must return a str for any ID (graceful not-found message)."""
    import mori_advisor.main as m
    from mori_advisor.main import msg_thread

    async def run(store):
        monkeypatch.setattr(m, "DATA_DIR", tmp_path)

        result = await msg_thread("00000000-0000-0000-0000-000000000000")
        assert_no_coroutines(result)
        assert isinstance(result, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── External-dependent tools (monkeypatched at module boundary) ───────────────
#
# For nats_*, consult_advisor, dream_run, mori_ingest, mori_ingest_preview,
# and msg_send the external client is replaced with a stub.  The
# store-interaction + arg-parsing path still runs and the coroutine scan
# still applies.


def _stub_nats_module(monkeypatch):
    """Replace the ``nats`` module in sys.modules with a lightweight stub.

    The nats tools do ``import nats`` inside their async body; the real library
    will hang on ``nats.connect()`` if no server is reachable.  We replace the
    entire module so arg-parsing and the return-value coroutine scan exercise
    the real tool code without touching the network.
    """
    import sys
    import types

    fake_nats = types.ModuleType("nats")

    class _FakeNC:
        connected_url = "nats://stub:4222"

        async def publish(self, *a, **kw):
            pass

        async def flush(self, *a, **kw):
            pass

        async def drain(self, *a, **kw):
            pass

        async def subscribe(self, *a, **kw):
            return _FakeSub()

    class _FakeSub:
        async def next_msg(self, timeout=0):
            raise asyncio.TimeoutError

        async def unsubscribe(self):
            pass

    async def _fake_connect(url, **kw):
        return _FakeNC()

    fake_nats.connect = _fake_connect

    monkeypatch.setitem(sys.modules, "nats", fake_nats)
    return fake_nats


@pytest.mark.parametrize("backend", BACKENDS)
def test_nats_ping_stubbed(backend, tmp_path, monkeypatch):
    """nats_ping must return a str with a stub NATS module (no real network call)."""
    from mori_advisor.main import nats_ping

    async def run(store):
        _stub_nats_module(monkeypatch)
        result = await nats_ping()
        assert_no_coroutines(result)
        assert isinstance(result, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_nats_pub_stubbed(backend, tmp_path, monkeypatch):
    """nats_pub must return a str with a stub NATS module."""
    from mori_advisor.main import nats_pub

    async def run(store):
        _stub_nats_module(monkeypatch)
        result = await nats_pub(message="hello from tests")
        assert_no_coroutines(result)
        assert isinstance(result, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_nats_sub_stubbed(backend, tmp_path, monkeypatch):
    """nats_sub must return a str with a stub NATS module."""
    from mori_advisor.main import nats_sub

    async def run(store):
        _stub_nats_module(monkeypatch)
        result = await nats_sub(wait=0)
        assert_no_coroutines(result)
        assert isinstance(result, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_consult_advisor_stubbed(backend, tmp_path, monkeypatch):
    """consult_advisor returns a job_id; consult_status yields advice when bifrost is stubbed.

    The store side-effect (CONSULT_CAPTURE) must also not leak a coroutine.
    """
    import json

    import mori_advisor.main as m
    from mori_advisor.main import consult_advisor, consult_status

    async def run(store):
        monkeypatch.setattr(m.bifrost, "consult", lambda **kw: "Advisor says: stub response")
        monkeypatch.setattr(m, "CONSULT_CAPTURE", False)

        submit = await consult_advisor(question="What is 2 + 2?", depth="quick")
        assert_no_coroutines(submit)
        data = json.loads(submit)
        assert data["status"] == "pending"
        job_id = data["job_id"]

        result = None
        for _ in range(100):
            status = json.loads(await consult_status(job_id))
            assert_no_coroutines(status)
            if status["status"] == "done":
                result = status["result"]
                break
            if status["status"] == "error":
                raise AssertionError(status.get("error"))
            await asyncio.sleep(0.02)
        assert isinstance(result, str)
        assert "stub response" in result

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_dream_run_stubbed(backend, tmp_path, monkeypatch):
    """dream_run must return a str when the dream pipeline is stubbed."""
    import mori_advisor.main as m
    from mori_advisor.main import dream_run

    async def run(store):
        # Stub the pipeline so no bifrost/NATS calls are made
        class _FakePipeline:
            async def run(self, dry_run=False):
                return []

            async def get_status(self):
                return "**Dream State**\n  Events total: 0"

        monkeypatch.setattr(m, "dream_pipeline", _FakePipeline())

        result = await dream_run(dry_run=True)
        assert_no_coroutines(result)
        assert isinstance(result, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_mori_ingest_stubbed(backend, tmp_path, monkeypatch):
    """mori_ingest must return a str when the ingestion pipeline is stubbed.

    The stub exercises arg-parsing + tier validation; the pipeline call is
    replaced so no LLM / bifrost calls are made.  If mori_ingest writes to
    the store *after* the pipeline call, the stub still returns a complete
    dict so the write path is exercised.
    """
    import mori_advisor.main as m
    from mori_advisor.main import mori_ingest

    async def run(store):
        class _FakePipeline:
            async def ingest(self, **kw):
                return {
                    "sources": 0,
                    "chunks": 0,
                    "skipped": 0,
                    "errors": 0,
                    "cost_estimate": 0.0,
                    "memories_written": 0,
                    "memories_candidates": 0,
                }

        monkeypatch.setattr(m, "ingestion_pipeline", _FakePipeline())

        result = await mori_ingest(source=[], dry_run=True)
        assert_no_coroutines(result)
        assert isinstance(result, str)

        # Invalid tier → early-return validation string
        bad_tier = await mori_ingest(source=[], tier="invalid")
        assert_no_coroutines(bad_tier)
        assert isinstance(bad_tier, str)
        assert "invalid" in bad_tier.lower() or "tier" in bad_tier.lower()

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_mori_ingest_preview(backend, tmp_path, monkeypatch):
    """mori_ingest_preview must return a str (no LLM calls — zero-cost path)."""
    from mori_advisor.main import mori_ingest_preview

    async def run(store):
        result = await mori_ingest_preview(source=[])
        assert_no_coroutines(result)
        assert isinstance(result, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_msg_send_stubbed(backend, tmp_path, monkeypatch):
    """msg_send must return a str when NATS publish is stubbed."""
    import mori_advisor.msg as msg_mod
    from mori_advisor.main import msg_send

    async def run(store):
        # Stub publish_message to avoid NATS connection
        async def _fake_publish(nats_url, msg):
            pass

        monkeypatch.setattr(msg_mod, "publish_message", _fake_publish)

        result = await msg_send(to="test-host", type="task", body="Test task")
        assert_no_coroutines(result)
        assert isinstance(result, str)

        # Invalid type
        bad = await msg_send(to="test-host", type="invalid-type", body="x")
        assert_no_coroutines(bad)
        assert isinstance(bad, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── update (no-store, config-driven) ─────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_update_unknown_device(backend, tmp_path, monkeypatch):
    """update with an unknown device must return a str listing available
    devices (or 'Unknown device')."""
    import mori_advisor.main as m
    from mori_advisor.main import update

    async def run(store):
        # Ensure DEVICE_PROFILES is non-empty so we get a useful message
        monkeypatch.setattr(
            m,
            "DEVICE_PROFILES",
            {"nuc": {"hostname": "nuc", "family": "linux", "profiles": [".claude"]}},
        )
        result = await update(device="nonexistent-device")
        assert_no_coroutines(result)
        assert isinstance(result, str)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── memory_review ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_memory_review(backend, tmp_path, monkeypatch):
    """memory_review must return a str on both backends (no memories is fine)."""
    from mori_advisor.main import memory_review

    async def run(store):
        result = await memory_review(orphan_days=30, dry_run=True)
        assert_no_coroutines(result)
        assert isinstance(result, str)
        assert "Memory Review" in result

    _run_with_backend(backend, tmp_path, monkeypatch, run)
