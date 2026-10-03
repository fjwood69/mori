"""v2.3.10 — JSONB array columns on the Postgres write path, migration 16, orphan-scan parity.

2026-10-02 incident: ``PostgresStore._write``'s UPDATE branch ``json.dumps``-ed ``protected_domains``
as returned by asyncpg — a ``str``, since no JSONB codec is registered — so every UPDATE nested the
array one JSON-string level deeper, roughly doubling it. The deployment-contract probe row, updated
on every deploy since June, reached a ~1 GiB bind parameter and froze the 2 GB host.

Real engine only: asyncpg against a real Postgres (CI's Postgres job). A fake pool that returned
parsed lists would hide the bug class entirely. State is asserted IN SQL — the keep-``_tags_json``
mutation produces an array-shaped but wrong ``["[]"]`` that client-side checks would wave through.
Every negative test has a positive control showing the path it guards actually ran.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import pytest

from mori_advisor import metrics as mx
from mori_advisor.policy import Actor, current_actor
from mori_advisor.store.migrations import JSONB_ARRAY_COLUMNS, _jsonb_arrays_postgres
from mori_advisor.store.postgres_store import _jsonb_array
from mori_advisor.write_result import Disposition, WriteResult

PG_URL = os.environ.get("MORI_TEST_DATABASE_URL", "")
requires_pg = pytest.mark.skipif(not PG_URL, reason="MORI_TEST_DATABASE_URL not set")
REPO = Path(__file__).resolve().parent.parent
CONSTRAINTS = [f"memories_{c}_is_array" for c in JSONB_ARRAY_COLUMNS]


# Each JSON-string layer escapes every quote and backslash of the one below, so the text grows
# roughly 2x per level: 12 levels of [] is ~8 KB, 64 levels is ~2^64 characters. Building deep
# nests for real OOM-killed this test run twice on the NUC (2026-10-03, ~25 GB RSS) — the incident's
# own mechanism. Hence the hard ceiling; depth caps are tested by lowering the cap instead.
_NEST_CEILING = 12


def _nest(value, levels: int) -> str:
    """JSON text of ``value`` wrapped in ``levels`` JSON-string layers (the bug's shape)."""
    assert levels <= _NEST_CEILING, "deep nesting is exponential — lower the cap under test instead"
    text = json.dumps(value)
    for _ in range(levels):
        text = json.dumps(text)
    return text


def _unwrapped(column: str) -> float:
    return (
        mx.prom_registry.get_sample_value("mori_jsonb_unwrapped_total", {"column": column}) or 0.0
    )


# ── D1: the decoder ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected,unwraps",
    [
        (None, [], 0),
        ([], [], 0),
        (["a"], ["a"], 0),
        ('["a", "b"]', ["a", "b"], 0),  # the normal asyncpg str: one json.loads, no unwrap
        (_nest([], 1), [], 1),
        (_nest(["d1"], 2), ["d1"], 2),
        (_nest(["d1", "d2"], 10), ["d1", "d2"], 10),
        (_nest([], 12), [], 12),
    ],
)
def test_jsonb_array_decodes_and_counts_legacy_nesting(caplog, value, expected, unwraps):
    before = _unwrapped("protected_domains")
    with caplog.at_level(logging.WARNING):
        assert _jsonb_array(value, column="protected_domains", memory_name="m") == expected
    assert _unwrapped("protected_domains") == before + (1 if unwraps else 0)
    warned = [r.getMessage() for r in caplog.records if "JSONB-UNWRAP" in r.getMessage()]
    if unwraps:
        assert warned and f"depth={unwraps}" in warned[0] and "memory=m" in warned[0]
    else:
        assert not warned


@pytest.mark.parametrize(
    "value",
    [
        '{"a": 1}',  # object
        "42",  # scalar
        '"plain string"',  # a string that is not JSON inside
        json.dumps("x" * 1_000_001),  # larger than the decoded cap
        "not json",
    ],
)
def test_jsonb_array_rejects_everything_else(value):
    with pytest.raises(ValueError):
        _jsonb_array(value, column="protected_domains")


def test_jsonb_array_depth_cap_is_enforced(monkeypatch):
    """The 64-level cap, exercised at a small cap: real 65-level nesting is ~2^65 characters."""
    import mori_advisor.store.postgres_store as pg

    monkeypatch.setattr(pg, "_JSONB_MAX_UNWRAP", 3)
    assert pg._jsonb_array(_nest([], 3), column="protected_domains") == []  # at the cap: ok
    with pytest.raises(ValueError, match="nested more than 3"):
        pg._jsonb_array(_nest([], 5), column="protected_domains")


def test_jsonb_array_byte_cap_is_enforced_before_decoding(monkeypatch):
    import mori_advisor.store.postgres_store as pg

    monkeypatch.setattr(pg, "_JSONB_DECODED_CAP", 1000)
    assert pg._jsonb_array(json.dumps(["a"] * 100), column="tags") == ["a"] * 100  # 500 chars: ok
    with pytest.raises(ValueError, match="cap 1,000"):
        pg._jsonb_array(json.dumps(["a"] * 300), column="tags")


def test_unwrap_counter_exists_at_zero_on_a_fresh_registry():
    code = (
        "import json; from mori_advisor import metrics as m; print(json.dumps([m.prom_registry."
        "get_sample_value('mori_jsonb_unwrapped_total', {'column': c}) for c in m.JSONB_UNWRAP_COLUMNS]))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=120
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert json.loads(out.stdout.strip().splitlines()[-1]) == [0.0, 0.0, 0.0]


# ── Postgres harness ──────────────────────────────────────────────────────────


@asynccontextmanager
async def _pg_store():
    from mori_advisor.store.postgres_store import PostgresStore

    store = PostgresStore(PG_URL)
    await store.bootstrap()  # applies migration 16 if this DB has not had it
    try:
        yield store
    finally:
        await store.pool.close()


@asynccontextmanager
async def _without_constraints(store):
    """Drop the array CHECKs so a test can seed the legacy corruption shapes; on exit, run
    migration 16's body again (repair + re-add + validate) so the shared DB is left valid."""
    async with store.pool.acquire() as conn:
        for name in CONSTRAINTS:
            await conn.execute(f"ALTER TABLE memories DROP CONSTRAINT IF EXISTS {name}")
    try:
        yield
    finally:
        async with store.pool.acquire() as conn:
            async with conn.transaction():
                await _jsonb_arrays_postgres(conn)


async def _cell(store, name: str, column: str) -> dict:
    async with store.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT jsonb_typeof({column}) AS t, {column}::text AS v, pg_column_size({column}) AS size "
            "FROM memories WHERE name = $1 AND deleted_at IS NULL",
            name,
        )
    return dict(row) if row else {}


@contextmanager
def _actor(actor):
    token = current_actor.set(actor)
    try:
        yield
    finally:
        current_actor.reset(token)


def _post_request(body: dict):
    from starlette.datastructures import State
    from starlette.requests import Request

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
        sent = False

        async def __call__(self):
            if not self.sent:
                self.sent = True
                return {"type": "http.request", "body": raw, "more_body": False}
            return {"type": "http.disconnect"}

    req = Request(scope, receive=_Recv())
    req._state = State()
    req.state.actor = current_actor.get()
    return req


def _use_store(monkeypatch, store):
    import mori_advisor.main as m
    import mori_advisor.policy as pol

    monkeypatch.setattr(m, "store", store)
    monkeypatch.setattr(m, "memory_store", store)
    monkeypatch.setattr(pol, "_TD_MODE", "api")
    monkeypatch.setattr(pol, "_LOCAL_FULL_ACCESS", False)


def _name(tag: str) -> str:
    return f"jsonb-{tag}-{uuid.uuid4().hex[:8]}"


# ── D1 through the REST write path the deployment contract uses ──────────────


@requires_pg
def test_rest_updates_keep_protected_domains_exactly_an_empty_array(monkeypatch):
    """The 2026-10-02 shape: one row written through POST /api/memories six times."""
    from mori_advisor.main import post_memory

    async def run():
        async with _pg_store() as store:
            _use_store(monkeypatch, store)
            name = _name("rest")
            sizes = []
            with _actor(Actor("writer", "write")):
                for i in range(6):  # 1 insert + 5 updates
                    resp = await post_memory(
                        _post_request({"name": name, "title": "probe", "body": f"v{i}"})
                    )
                    assert resp.status_code in (200, 201), resp.body
                    cell = await _cell(store, name, "protected_domains")
                    assert cell["t"] == "array" and cell["v"] == "[]", (i, cell)
                    sizes.append(cell["size"])
            assert len(set(sizes)) == 1, sizes  # constant: nothing grows
            # Positive control: the six calls really were writes (the body moved on each).
            async with store.pool.acquire() as conn:
                assert (
                    await conn.fetchval("SELECT body FROM memories WHERE name = $1", name) == "v5"
                )

    asyncio.run(run())


@requires_pg
def test_real_protected_domains_survive_updates_exactly():
    async def run():
        async with _pg_store() as store:
            name = _name("domains")
            await store.write(name=name, title="t", body="b0", tags=["x"])
            await store.protect(name, ["d1", "d2"])
            for i in range(3):
                r = await store._write(
                    name=name, title="t", body=f"b{i + 1}", tags=["x"], _skip_protection=True
                )
                assert r.disposition is Disposition.ACCEPTED, r.reason
            cell = await _cell(store, name, "protected_domains")
            assert cell["t"] == "array"
            async with store.pool.acquire() as conn:
                assert await conn.fetchval(
                    "SELECT protected_domains = '[\"d1\", \"d2\"]'::jsonb AND body = 'b3' FROM memories WHERE name = $1",
                    name,
                )

    asyncio.run(run())


@requires_pg
def test_nested_origin_session_ids_never_contribute_characters_to_the_merge(caplog):
    async def run():
        async with _pg_store() as store:
            name = _name("merge")
            await store.write(name=name, title="t", body="b", origin_session_ids=["s1"])
            async with _without_constraints(store):
                async with store.pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE memories SET origin_session_ids = $1::jsonb WHERE name = $2",
                        _nest(["s1"], 1),
                        name,
                    )
                before = _unwrapped("origin_session_ids")
                with caplog.at_level(logging.WARNING):
                    r = await store._write(
                        name=name, title="t", body="b2", origin_session_ids=["s2"]
                    )
                assert r.disposition is Disposition.ACCEPTED, r.reason
                cell = await _cell(store, name, "origin_session_ids")
                assert cell["t"] == "array" and json.loads(cell["v"]) == ["s1", "s2"], cell
                assert _unwrapped("origin_session_ids") == before + 1
                assert any(
                    "JSONB-UNWRAP column=origin_session_ids" in r.getMessage()
                    for r in caplog.records
                )

    asyncio.run(run())


@requires_pg
def test_an_update_heals_a_legacy_nested_protected_domains_to_exactly_empty():
    """D1 is self-healing: the next UPDATE rewrites a legacy-nested value in correct form. Kills the
    keep-``_tags_json`` mutation, which turns '"[]"' into the array-shaped but wrong '["[]"]'."""

    async def run():
        async with _pg_store() as store:
            name = _name("heal")
            await store.write(name=name, title="t", body="b")
            async with _without_constraints(store):
                async with store.pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE memories SET protected_domains = $1::jsonb WHERE name = $2",
                        _nest([], 2),
                        name,
                    )
                r = await store._write(name=name, title="t", body="b2")
                assert r.disposition is Disposition.ACCEPTED, r.reason
                async with store.pool.acquire() as conn:
                    assert await conn.fetchval(
                        "SELECT protected_domains = '[]'::jsonb AND jsonb_typeof(protected_domains) = 'array' "
                        "FROM memories WHERE name = $1",
                        name,
                    )

    asyncio.run(run())


@requires_pg
def test_a_value_the_decoder_rejects_is_a_rejected_write_not_an_exception():
    async def run():
        async with _pg_store() as store:
            name = _name("reject")
            await store.write(name=name, title="t", body="b")
            async with _without_constraints(store):
                async with store.pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE memories SET protected_domains = '{\"a\": 1}'::jsonb WHERE name = $1",
                        name,
                    )
                r = await store._write(name=name, title="t", body="b2")
                assert r.disposition is Disposition.REJECTED and "protected_domains" in r.reason
                async with store.pool.acquire() as conn:  # nothing was written
                    assert (
                        await conn.fetchval("SELECT body FROM memories WHERE name = $1", name)
                        == "b"
                    )

    asyncio.run(run())


# ── R5.6: approve never marks a write that did not happen ─────────────────────


@requires_pg
def test_approve_leaves_the_pending_row_pending_when_the_canon_write_is_rejected(monkeypatch):
    async def run():
        async with _pg_store() as store:
            name = _name("approve")
            await store.queue_pending_write(name=name, title="t", body="b", proposed_by="writer")
            async with store.pool.acquire() as conn:
                wid = await conn.fetchval(
                    "SELECT max(id) FROM pending_writes WHERE memory_name = $1", name
                )

            real_write = store._write

            async def _rejecting(**kw):
                return WriteResult(
                    memory_name=kw["name"],
                    intended_tier="working",
                    stored_tier="",
                    disposition=Disposition.REJECTED,
                    reason="injected",
                )

            monkeypatch.setattr(store, "_write", _rejecting)
            msg = await store.approve(wid, note="n", reviewer="r")
            assert "NOT approved" in msg and "injected" in msg
            async with store.pool.acquire() as conn:
                assert (
                    await conn.fetchval("SELECT status FROM pending_writes WHERE id = $1", wid)
                    == "pending"
                )
                assert (
                    await conn.fetchval("SELECT count(*) FROM memories WHERE name = $1", name) == 0
                )

            monkeypatch.setattr(store, "_write", real_write)  # positive control
            msg = await store.approve(wid, note="n", reviewer="r")
            async with store.pool.acquire() as conn:
                assert (
                    await conn.fetchval("SELECT status FROM pending_writes WHERE id = $1", wid)
                    == "approved"
                ), msg
                assert (
                    await conn.fetchval("SELECT count(*) FROM memories WHERE name = $1", name) == 1
                )
                # Condition 1 (build ruling): approve now calls _write(), which IS the
                # identity-aware chokepoint (write() is a thin adapter over it). The promotion
                # must still land its atomic audit row with the governed-promotion provenance.
                audit = await conn.fetch(
                    "SELECT actor_key_name, op, detail FROM write_audit WHERE memory_name = $1",
                    name,
                )
                assert [(r["actor_key_name"], r["op"]) for r in audit] == [
                    ("governed-promotion", "approve")
                ], [dict(r) for r in audit]
                assert audit[0]["detail"] == "store:approve"

    asyncio.run(run())


# ── D2: migration 16 ──────────────────────────────────────────────────────────


@requires_pg
def test_migration_16_repairs_every_shape_and_makes_non_arrays_unrepresentable():
    import asyncpg

    async def run():
        async with _pg_store() as store:
            a, b, c, d = (_name(x) for x in ("a", "b", "c", "d"))
            for n in (a, b, c, d):
                await store.write(name=n, title="t", body="b")
            async with _without_constraints(store):
                # The oversized value is seeded as the real corruption shape (a nested STRING),
                # made incompressible so its stored size is well over the 4 KB guard.
                bloat = _nest([os.urandom(8000).hex()], 3)
                async with store.pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE memories SET protected_domains = $1::jsonb WHERE name = $2",
                        _nest([], 3),
                        a,
                    )
                    await conn.execute(
                        "UPDATE memories SET protected_domains = $1::jsonb WHERE name = $2",
                        _nest(["d1"], 2),
                        b,
                    )
                    await conn.execute(
                        "UPDATE memories SET protected_domains = $1::jsonb WHERE name = $2",
                        bloat,
                        c,
                    )
                    await conn.execute(
                        "UPDATE memories SET tags = '{\"k\": 1}'::jsonb WHERE name = $1", d
                    )
                    assert (
                        await conn.fetchval(
                            "SELECT pg_column_size(protected_domains) FROM memories WHERE name = $1",
                            c,
                        )
                        > 4096
                    )

                    notices: list[str] = []

                    def _listen(_conn, msg):
                        notices.append(msg.message)

                    conn.add_log_listener(_listen)
                    async with conn.transaction():
                        await _jsonb_arrays_postgres(conn)
                    conn.remove_log_listener(_listen)

                    for n, col, want in (
                        (a, "protected_domains", "[]"),
                        (b, "protected_domains", '["d1"]'),
                        (c, "protected_domains", "[]"),
                        (d, "tags", "[]"),
                    ):
                        got = await conn.fetchval(
                            f"SELECT {col}::text FROM memories WHERE name = $1 AND jsonb_typeof({col}) = 'array'",
                            n,
                        )
                        assert got == want, (n, col, got)
                    # (a) took the oversized row out BEFORE (b) read anything: (b) unwrapped only
                    # a and b — the 3-level string and the buried ["d1"] — and blanked nothing.
                    pd = [m for m in notices if "protected_domains" in m]
                    assert pd and "2 unwrapped, 0 blanked" in pd[-1], notices
                    assert any("tags" in m and "0 unwrapped, 1 blanked" in m for m in notices), (
                        notices
                    )

                    rows = await conn.fetch(
                        "SELECT conname, convalidated FROM pg_constraint WHERE conrelid = 'memories'::regclass "
                        "AND conname = ANY($1::text[])",
                        CONSTRAINTS,
                    )
                    assert {r["conname"]: r["convalidated"] for r in rows} == {
                        n: True for n in CONSTRAINTS
                    }
                    with pytest.raises(asyncpg.CheckViolationError):
                        await conn.execute(
                            "UPDATE memories SET protected_domains = '\"x\"'::jsonb WHERE name = $1",
                            a,
                        )

                    # Re-running is a no-op: values and validated constraints unchanged.
                    snapshot = await conn.fetch(
                        "SELECT name, protected_domains::text, tags::text FROM memories WHERE name = ANY($1::text[]) ORDER BY name",
                        [a, b, c, d],
                    )
                    async with conn.transaction():
                        await _jsonb_arrays_postgres(conn)
                    again = await conn.fetch(
                        "SELECT name, protected_domains::text, tags::text FROM memories WHERE name = ANY($1::text[]) ORDER BY name",
                        [a, b, c, d],
                    )
                    assert [tuple(r) for r in snapshot] == [tuple(r) for r in again]

    asyncio.run(run())


@requires_pg
def test_a_failing_migration_16_fails_bootstrap_and_startup(monkeypatch):
    """Before-deploy item 1: start-up must refuse to serve on an unmigrated schema."""
    import mori_advisor.main as main_mod
    import mori_advisor.store.migrations as mig

    async def boom(conn):
        raise RuntimeError("migration 16 injected failure")

    async def run():
        async with _pg_store() as store:
            async with store.pool.acquire() as conn:
                await conn.execute("DELETE FROM schema_migrations WHERE id = 16")
            monkeypatch.setattr(mig, "_jsonb_arrays_postgres", boom)
            with pytest.raises(RuntimeError, match="injected"):
                await store.bootstrap()
            async with store.pool.acquire() as conn:  # rolled back: not recorded as applied
                assert (
                    await conn.fetchval("SELECT count(*) FROM schema_migrations WHERE id = 16") == 0
                )

            monkeypatch.setattr(main_mod, "store", store)
            with pytest.raises(RuntimeError, match="injected"):  # the lifespan does not swallow it
                async with main_mod._lifespan(None):
                    pass

            monkeypatch.undo()  # positive control: the real migration applies on the next start
            await store.bootstrap()
            async with store.pool.acquire() as conn:
                assert (
                    await conn.fetchval("SELECT count(*) FROM schema_migrations WHERE id = 16") == 1
                )

    asyncio.run(run())


@requires_pg
def test_fresh_schema_has_the_constraints_and_rejects_a_string():
    import asyncpg

    async def run():
        async with _pg_store() as store:
            async with store.pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT conname, convalidated FROM pg_constraint WHERE conrelid = 'memories'::regclass "
                    "AND conname = ANY($1::text[])",
                    CONSTRAINTS,
                )
                assert {r["conname"]: r["convalidated"] for r in rows} == {
                    n: True for n in CONSTRAINTS
                }
                name = _name("fresh")
                await store.write(name=name, title="t", body="b")
                for col in JSONB_ARRAY_COLUMNS:
                    with pytest.raises(asyncpg.CheckViolationError):
                        await conn.execute(
                            f"UPDATE memories SET {col} = '\"x\"'::jsonb WHERE name = $1", name
                        )

    asyncio.run(run())


# ── D7: orphan scan — strict SQLite parity on both backends ──────────────────

_ORPHAN_ROWS = {
    # name-suffix: (tier, protected, last_retrieved days ago or None)  → flagged?
    "working-stale": ("working", False, 40, True),
    "ephemeral-stale": ("ephemeral", False, 40, True),
    "working-never": ("working", False, None, False),  # never retrieved is NOT an orphan
    "canonical-stale": ("canonical", False, 40, False),
    "working-recent": ("working", False, 2, False),
    "protected-stale": ("working", True, 40, False),
}


async def _seed_orphans(store, prefix: str, set_sql) -> None:
    for suffix, (tier, protected, days, _flag) in _ORPHAN_ROWS.items():
        name = f"{prefix}-{suffix}"
        r = store.write(name=name, title=suffix, body="b", tier=tier, _skip_protection=True)
        if inspect.isawaitable(r):
            await r
        if protected:
            p = store.protect(name)
            if inspect.isawaitable(p):
                await p
        if days is not None:
            await set_sql(name, days)


@pytest.mark.parametrize(
    "backend",
    ["sqlite", pytest.param("pg", marks=requires_pg)],
)
def test_orphan_scan_queues_never_deletes_with_the_sqlite_predicate(backend, tmp_path):
    prefix = f"orph{uuid.uuid4().hex[:8]}"
    expected = {f"{prefix}-{s}" for s, v in _ORPHAN_ROWS.items() if v[3]}

    async def run():
        if backend == "sqlite":
            from mori_advisor.store.sqlite_store import SQLiteStore

            store = SQLiteStore(tmp_path / "m.db")
            store.bootstrap()

            async def set_sql(name, days):
                conn = store.get_conn()
                conn.execute(
                    "UPDATE memories SET last_retrieved_at = datetime('now', ?) WHERE name = ?",
                    (f"-{days} days", name),
                )
                conn.commit()
                conn.close()

            def q(sql, *args):
                conn = store.get_conn()
                try:
                    return conn.execute(sql.replace("$1", "?"), args).fetchall()
                finally:
                    conn.close()

            await _seed_orphans(store, prefix, set_sql)
            dry = store.scan_orphans(days=30, dry_run=True)
            assert (
                q("SELECT count(*) FROM eviction_queue WHERE memory_name LIKE $1", f"{prefix}-%")[
                    0
                ][0]
                == 0
            )
            out = store.scan_orphans(days=30, dry_run=False)
            queued = {
                r[0]
                for r in q(
                    "SELECT memory_name FROM eviction_queue WHERE reason = 'orphan' AND memory_name LIKE $1",
                    f"{prefix}-%",
                )
            }
            alive = q(
                "SELECT count(*) FROM memories WHERE name LIKE $1 AND deleted_at IS NULL",
                f"{prefix}-%",
            )[0][0]
            return dry, out, queued, alive

        async with _pg_store() as store:

            async def set_sql(name, days):
                async with store.pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE memories SET last_retrieved_at = now() - make_interval(days => $1) WHERE name = $2",
                        days,
                        name,
                    )

            await _seed_orphans(store, prefix, set_sql)
            dry = await store.scan_orphans(days=30, dry_run=True)
            async with store.pool.acquire() as conn:
                assert (
                    await conn.fetchval(
                        "SELECT count(*) FROM eviction_queue WHERE memory_name LIKE $1",
                        f"{prefix}-%",
                    )
                    == 0
                )
            out = await store.scan_orphans(days=30, dry_run=False)
            async with store.pool.acquire() as conn:
                queued = {
                    r["memory_name"]
                    for r in await conn.fetch(
                        "SELECT memory_name FROM eviction_queue WHERE reason = 'orphan' AND memory_name LIKE $1",
                        f"{prefix}-%",
                    )
                }
                alive = await conn.fetchval(
                    "SELECT count(*) FROM memories WHERE name LIKE $1 AND deleted_at IS NULL",
                    f"{prefix}-%",
                )
            return dry, out, queued, alive

    dry, out, queued, alive = asyncio.run(run())
    assert queued == expected  # exactly the stale non-canonical, unprotected, once-retrieved rows
    assert alive == len(_ORPHAN_ROWS)  # nothing deleted
    for n in expected:
        assert n in dry and n in out
    assert f"{prefix}-working-never" not in out


# ── The deployment contract's probe-row canary ────────────────────────────────


def _contract_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "verify_deployment", REPO / "scripts" / "verify-deployment.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@requires_pg
def test_contract_canary_requires_this_runs_write_and_an_empty_array(monkeypatch):
    import asyncpg

    mod = _contract_module()
    monkeypatch.setenv("MORI_DATABASE_URL", PG_URL)
    name = _name("canary")

    async def clock():
        conn = await asyncpg.connect(PG_URL, ssl=False)
        try:
            return await conn.fetchval("SELECT now()")
        finally:
            await conn.close()

    async def write_row():
        async with _pg_store() as store:
            await store.write(name=name, title="t", body=f"b-{uuid.uuid4().hex}")

    async def nest_row():  # constraints dropped; left dropped until repair()
        conn = await asyncpg.connect(PG_URL, ssl=False)
        try:
            for c in CONSTRAINTS:
                await conn.execute(f"ALTER TABLE memories DROP CONSTRAINT IF EXISTS {c}")
            await conn.execute(
                "UPDATE memories SET protected_domains = $1::jsonb, updated_at = now() "
                "WHERE name = $2",
                _nest([], 2),
                name,
            )
        finally:
            await conn.close()

    async def repair():
        conn = await asyncpg.connect(PG_URL, ssl=False)
        try:
            async with conn.transaction():
                await _jsonb_arrays_postgres(conn)
        finally:
            await conn.close()

    t0 = asyncio.run(clock())
    asyncio.run(write_row())
    assert mod._probe_row_canary(name, t0) is True  # positive control: written now, []

    # Condition 2: a row NOT written in this run must fail, even though it is a clean '[]'.
    later = asyncio.run(clock())
    assert mod._probe_row_canary(name, later) is False
    assert mod._probe_row_canary(name, None) is False  # no clock read ⇒ never pass

    t1 = asyncio.run(clock())
    asyncio.run(nest_row())
    try:
        assert mod._probe_row_canary(name, t1) is False  # written now, but nested
    finally:
        asyncio.run(repair())


def test_contract_fails_outright_when_the_probe_write_fails(monkeypatch):
    """Condition 2: a non-2xx probe WRITE fails the contract; it must never skip to the canary."""
    mod = _contract_module()
    monkeypatch.delenv("MORI_DATABASE_URL", raising=False)
    calls = []

    def fake_request(method, url, key=None, body=None):
        calls.append((method, url))
        if method == "POST" and url.endswith("/api/memories"):
            return status["post"]
        if key is None:
            return 401
        return 200

    canary_called = []
    monkeypatch.setattr(mod, "_request", fake_request)
    monkeypatch.setattr(mod, "_probe_row_canary", lambda n, t: canary_called.append(n) or True)

    for bad in (500, 404, 202, 0):
        status = {"post": bad}
        calls.clear()
        canary_called.clear()
        assert mod._softdel_probe_and_canary("http://x", "k") == 1, bad
        assert calls == [("POST", "http://x/api/memories")]  # nothing after the failed write
        assert canary_called == []

    status = {"post": 200}  # positive control: the full round-trip and the canary run
    assert mod._softdel_probe_and_canary("http://x", "k") == 0
    assert canary_called == [mod.SOFTDEL_PROBE]
