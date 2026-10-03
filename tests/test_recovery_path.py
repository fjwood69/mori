"""v2.3.11 — the recovery path, storage half (D1–D3; board ruling 2026-10-03).

Measured before this release: Postgres wrote no ``memory_versions`` rows (prod frozen at 50 since
the 4 June import); rollback was broken on BOTH backends (Postgres ``KeyError`` on ``row["tier"]``,
SQLite ``TypeError`` from ``_snapshot_to_versions`` without ``_conn``) and the existing test only
asserted "returns a str"; the Postgres write chokepoint was not transactional for REST/MCP; prod's
``eviction_queue_id_seq`` sat at 4 against ``max(id)`` 77, so every supersession insert collided.

Real engines only (SQLite file + asyncpg against a real Postgres — CI's Postgres job). State is
asserted in SQL, never only through return strings (a str-only oracle is exactly what hid M3).
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import re
from pathlib import Path

import pytest

from mori_advisor import metrics as mx
from mori_advisor.provenance import Provenance
from mori_advisor.write_result import Disposition

PG_URL = os.environ.get("MORI_TEST_DATABASE_URL", "")
requires_pg = pytest.mark.skipif(not PG_URL, reason="MORI_TEST_DATABASE_URL not set")
BACKENDS = ["sqlite", pytest.param("postgres", marks=requires_pg)]
REPO = Path(__file__).resolve().parent.parent
GOOD_DESC = "Why this matters: a test fixture with a real warrant."


async def _a(x):
    return await x if inspect.isawaitable(x) else x


class H:
    """One handle over either backend: the public store, the chokepoint owner, raw SQL."""

    def __init__(self, backend, store):
        self.backend = backend
        self.store = store
        self.mem = store._mem if hasattr(store, "_mem") else store  # owner of _write/rollback

    @property
    def pg(self) -> bool:
        return self.backend == "postgres"

    async def q(self, sql: str, *args):
        """Run SQL written with $n placeholders on either backend; return rows as dicts."""
        if self.pg:
            async with self.store.pool.acquire() as conn:
                return [dict(r) for r in await conn.fetch(sql, *args)]
        conn = self.mem._get_conn()
        try:
            cur = conn.execute(re.sub(r"\$\d+", "?", sql), args)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            conn.commit()
            return rows
        finally:
            conn.close()

    async def write(self, name, body, tags=None, desc=GOOD_DESC):
        r = await _a(
            self.mem._write(
                name=name,
                title=name,
                description=desc,
                body=body,
                tags=tags or [],
                provenance=Provenance(actor="system", source="test", op="write"),
            )
        )
        assert r.disposition is Disposition.ACCEPTED, r.reason
        return r

    async def row(self, name):
        rows = await self.q("SELECT * FROM memories WHERE name = $1 AND deleted_at IS NULL", name)
        return rows[0] if rows else None

    async def versions(self, name):
        return await self.q(
            "SELECT version_id, body, tags, version_note, memory_id FROM memory_versions "
            "WHERE memory_name = $1 ORDER BY version_id",
            name,
        )

    async def audit_ops(self, name):
        return [
            r["op"]
            for r in await self.q(
                "SELECT op FROM write_audit WHERE memory_name = $1 ORDER BY id", name
            )
        ]


def _tags(v):
    return json.loads(v) if isinstance(v, str) else v


def run(backend, tmp_path, fn):
    async def go():
        if backend == "sqlite":
            from mori_advisor.store import get_store

            store = get_store(tmp_path / "memories.db")
            store.bootstrap()
        else:
            from mori_advisor.store.postgres_store import PostgresStore

            store = PostgresStore(PG_URL)
            await store.bootstrap()
            async with store.pool.acquire() as conn:
                await conn.execute(
                    "TRUNCATE memories, memory_versions, pending_writes, eviction_queue, "
                    "write_audit CASCADE"
                )
        try:
            return await fn(H(backend, store))
        finally:
            if hasattr(store, "pool") and store.pool:
                await store.pool.close()

    return asyncio.run(go())


# ── D1: versioning on every update ───────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_update_snapshots_prior_state_keyed_to_the_row(backend, tmp_path):
    async def t(h):
        await h.write("v1", "ONE")
        await h.write("v1", "TWO")
        await h.write("v1", "THREE")
        row = await h.row("v1")
        vs = await h.versions("v1")
        assert [v["body"] for v in vs] == ["ONE", "TWO"]
        assert {v["memory_id"] for v in vs} == {row["id"]}
        assert {v["version_note"] for v in vs} == {"updated"}

    run(backend, tmp_path, t)


@pytest.mark.parametrize("backend", BACKENDS)
def test_versions_pruned_to_newest_twenty_by_version_id(backend, tmp_path):
    from mori_advisor.memory_store import MAX_VERSIONS_PER_MEMORY

    async def t(h):
        for i in range(MAX_VERSIONS_PER_MEMORY + 4):  # 24 writes -> 23 snapshots -> keep 20
            await h.write("p1", f"B{i:02d}")
        vs = await h.versions("p1")
        assert len(vs) == MAX_VERSIONS_PER_MEMORY
        assert [v["body"] for v in vs] == [f"B{i:02d}" for i in range(3, 23)]

    run(backend, tmp_path, t)


@requires_pg
def test_pg_audit_failure_rolls_back_the_whole_write(tmp_path):
    """Board test 4: with the write in one transaction, an audit failure must fail the WRITE —
    raise or non-ACCEPTED — never ACCEPTED with the upsert silently rolled back (A-F-A1)."""

    async def t(h):
        await h.write("a1", "ONE")  # positive control: the normal path is ACCEPTED
        async with h.store.pool.acquire() as conn:
            await conn.execute("ALTER TABLE write_audit RENAME TO write_audit_hidden")
        try:
            outcome = None
            try:
                r = await h.mem._write(
                    name="a1",
                    title="a1",
                    description=GOOD_DESC,
                    body="TWO",
                    provenance=Provenance(actor="system", source="test", op="write"),
                )
                outcome = r.disposition
            except Exception as e:  # raising is an acceptable outcome
                outcome = type(e).__name__
            assert outcome is not Disposition.ACCEPTED, "a rolled-back write reported ACCEPTED"
        finally:
            async with h.store.pool.acquire() as conn:
                await conn.execute("ALTER TABLE write_audit_hidden RENAME TO write_audit")
        assert (await h.row("a1"))["body"] == "ONE"
        assert await h.versions("a1") == []

    run("postgres", tmp_path, t)


# ── D2: rollback through the chokepoint ──────────────────────────────────────


async def _version_id(h, name, body):
    return [v for v in await h.versions(name) if v["body"] == body][0]["version_id"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_rollback_round_trip(backend, tmp_path):
    async def t(h):
        await h.write("rb", "ONE", tags=["a", "b"])
        await h.write("rb", "TWO", tags=["c"])
        vid = await _version_id(h, "rb", "ONE")
        msg = await _a(h.store.rollback("rb", vid))
        assert msg == f"Memory 'rb' rolled back to version {vid}.", msg
        row = await h.row("rb")
        assert row["body"] == "ONE"
        # Board test 2: tags come back as the original list — not a JSON string re-encoded.
        assert _tags(row["tags"]) == ["a", "b"]
        notes = {v["version_note"]: v["body"] for v in await h.versions("rb")}
        assert notes[f"before rollback to v{vid}"] == "TWO"
        assert "rollback" in await h.audit_ops("rb")

    run(backend, tmp_path, t)


@pytest.mark.parametrize("backend", BACKENDS)
def test_rollback_leaves_a_tombstoned_namesake_untouched(backend, tmp_path):
    async def t(h):
        await h.write("ns", "OLD")
        await _a(h.store.soft_delete("ns"))
        await h.write("ns", "NEW1")
        await h.write("ns", "NEW2")
        vid = await _version_id(h, "ns", "NEW1")
        await _a(h.store.rollback("ns", vid))
        assert (await h.row("ns"))["body"] == "NEW1"
        dead = await h.q(
            "SELECT body FROM memories WHERE name = $1 AND deleted_at IS NOT NULL", "ns"
        )
        assert [d["body"] for d in dead] == ["OLD"]

    run(backend, tmp_path, t)


@pytest.mark.parametrize("backend", BACKENDS)
def test_rollback_of_a_protected_memory_follows_the_ratified_fork(backend, tmp_path):
    """R2: Postgres REJECTED with the actionable reason; SQLite queues pending. Never silent."""

    async def t(h):
        await h.write("pr", "ONE")
        await h.write("pr", "TWO")
        vid = await _version_id(h, "pr", "ONE")
        await h.q("UPDATE memories SET protected = $1 WHERE name = $2", True, "pr")
        msg = await _a(h.store.rollback("pr", vid))
        assert msg.startswith("Memory 'pr' NOT rolled back"), msg
        assert (await h.row("pr"))["body"] == "TWO"
        if h.pg:
            assert "rejected" in msg and "unprotect, roll back, re-protect" in msg
        else:
            assert "downgraded_to_pending" in msg
            pend = await h.q("SELECT body FROM pending_writes WHERE memory_name = $1", "pr")
            assert [p["body"] for p in pend] == ["ONE"]

    run(backend, tmp_path, t)


@pytest.mark.parametrize("backend", BACKENDS)
def test_canonical_rollback_requires_dreamer(backend, tmp_path, monkeypatch):
    """R6: keyed off the active row's tier, independent of MORI_TIER_ENFORCE — so the dreamer's
    rollback must succeed with tier enforcement ON (rollback is content-only; no tier transition)."""

    async def t(h):
        await h.write("cn", "ONE")
        await h.write("cn", "TWO")
        vid = await _version_id(h, "cn", "ONE")
        await h.q("UPDATE memories SET tier = 'canonical' WHERE name = $1", "cn")
        monkeypatch.setenv("MORI_TIER_ENFORCE", "enforce")
        refused = await _a(h.store.rollback("cn", vid, caller_is_dreamer=False))
        assert "requires the dreamer role" in refused
        assert (await h.row("cn"))["body"] == "TWO"
        mcp = Provenance(actor="mcp", actor_detail="td-key", source="test", op="rollback")
        ok = await _a(h.store.rollback("cn", vid, provenance=mcp, caller_is_dreamer=True))
        assert ok.startswith("Memory 'cn' rolled back"), ok
        row = await h.row("cn")
        assert (row["body"], row["tier"]) == ("ONE", "canonical")

    run(backend, tmp_path, t)


@pytest.mark.parametrize("backend", BACKENDS)
def test_rollback_refuses_a_version_of_an_earlier_incarnation(backend, tmp_path):
    async def t(h):
        await h.write("inc", "A1")
        await h.write("inc", "A2")
        old_vid = await _version_id(h, "inc", "A1")
        await _a(h.store.soft_delete("inc"))
        await h.write("inc", "B1")
        msg = await _a(h.store.rollback("inc", old_vid))
        assert "earlier incarnation" in msg
        assert (await h.row("inc"))["body"] == "B1"

    run(backend, tmp_path, t)


# ── Board amendment: anatomy on rollback ─────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_anatomy_bypass_only_for_dreamer_restoring_exact_stored_content(
    backend, tmp_path, monkeypatch
):
    async def t(h):
        # v1 predates the gate: empty description = empty warrant (fails anatomy under enforce).
        await h.write("an", "LEGACY BODY", desc="")
        await h.write("an", "CURRENT BODY")
        vid = await _version_id(h, "an", "LEGACY BODY")
        monkeypatch.setenv("MORI_ANATOMY_ENFORCE", "enforce")

        # A write-role caller is NOT bypassed: downgraded to pending, not rolled back.
        msg = await _a(h.store.rollback("an", vid, caller_is_dreamer=False))
        assert "NOT rolled back" in msg and "downgraded_to_pending" in msg, msg
        assert (await h.row("an"))["body"] == "CURRENT BODY"
        # Clear it: SQLite raises on a SECOND pending row for one name (Postgres updates it) —
        # a pre-existing parity gap filed separately, not in this release's scope.
        await h.q("DELETE FROM pending_writes WHERE memory_name = $1", "an")

        # Content that is not an exact stored version is refused even with op=rollback.
        r = await _a(
            h.mem._write(
                name="an",
                title="an",
                description="",
                body="FORGED BODY",
                provenance=Provenance(actor="system", source="test", op="rollback"),
                _anatomy_bypass="forged",
            )
        )
        assert r.disposition is Disposition.REJECTED and "bypass refused" in r.reason

        # The bypass is ignored unless op == "rollback".
        r = await _a(
            h.mem._write(
                name="an",
                title="an",
                description="",
                body="LEGACY BODY",
                provenance=Provenance(actor="system", source="test", op="write"),
                _anatomy_bypass="not a rollback",
            )
        )
        assert r.disposition is Disposition.DOWNGRADED_TO_PENDING

        # The dreamer restoring the exact stored version IS bypassed, and it is audited.
        msg = await _a(h.store.rollback("an", vid, caller_is_dreamer=True))
        assert msg.startswith("Memory 'an' rolled back"), msg
        assert (await h.row("an"))["body"] == "LEGACY BODY"
        codes = await h.q(
            "SELECT reason_code FROM write_audit WHERE memory_name = $1 AND op = 'rollback'",
            "an",
        )
        assert [c["reason_code"] for c in codes] == ["anatomy_bypass_rollback"]

    run(backend, tmp_path, t)


@requires_pg
def test_pg_approve_under_anatomy_enforce_does_not_self_hang(tmp_path, monkeypatch):
    """A-F-A2: approve holds FOR UPDATE on the pending row; the anatomy downgrade used to re-queue
    it on a SECOND pooled connection, which blocked on that lock forever."""

    async def t(h):
        await _a(h.store.queue_pending_write(name="hang", title="hang", body="no warrant"))
        pid = (await h.q("SELECT id FROM pending_writes WHERE memory_name = $1", "hang"))[0]["id"]
        monkeypatch.setenv("MORI_ANATOMY_ENFORCE", "enforce")
        msg = await asyncio.wait_for(h.store.approve(pid, note="t", reviewer="t"), timeout=5)
        assert "NOT approved" in msg, msg
        assert await h.row("hang") is None

    run("postgres", tmp_path, t)


# ── Restore, delete: one transaction with their audit rows ───────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_delete_and_restore_are_audited_and_restore_repoints_history(backend, tmp_path):
    async def t(h):
        await h.write("rs", "A")
        await h.write("rs", "B")  # version A belongs to the first incarnation
        await _a(h.store.soft_delete("rs", actor="k-del"))
        await h.write("rs", "C")
        await h.write("rs", "D")  # version C belongs to the second
        final, msg = await _a(h.store.restore_memory("rs", actor="k-res"))
        assert final.startswith("rs_restored_") and "name taken" in msg
        assert [v["body"] for v in await h.versions("rs")] == ["C"]
        assert [v["body"] for v in await h.versions(final)] == ["A"]
        audit = await h.q(
            "SELECT op, actor_key_name FROM write_audit WHERE op IN "
            "('soft_delete', 'restore', 'restore_renamed') ORDER BY id"
        )
        assert [(a["op"], a["actor_key_name"]) for a in audit] == [
            ("soft_delete", "k-del"),
            ("restore_renamed", "k-res"),
        ]

    run(backend, tmp_path, t)


@pytest.mark.parametrize("backend", BACKENDS)
def test_restore_reports_a_lingering_supersession(backend, tmp_path):
    async def t(h):
        await h.write("old-fact", "X")
        await h.write("new-fact", "Y")
        await h.q("UPDATE memories SET superseded_by = $1 WHERE name = $2", "new-fact", "old-fact")
        await _a(h.store.soft_delete("old-fact"))
        _, msg = await _a(h.store.restore_memory("old-fact"))
        assert "still superseded by 'new-fact' (active)" in msg
        assert (await h.row("old-fact"))["superseded_by"] == "new-fact"  # not silently cleared

    run(backend, tmp_path, t)


# ── D3: owned-sequence invariant ─────────────────────────────────────────────


def _repairs(table: str) -> float:
    return mx.prom_registry.get_sample_value("mori_sequence_repairs_total", {"table": table}) or 0.0


@requires_pg
def test_pg_boot_raises_a_lagging_sequence_and_never_lowers_one(tmp_path):
    """Board test 5b: one sequence below max(id) (raised), one above (untouched)."""

    async def t(h):
        async with h.store.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO eviction_queue (id, memory_name, reason) "
                "SELECT g, 'seed-' || g, 'orphan' FROM generate_series(1, 77) g"
            )
            await conn.execute("SELECT setval('eviction_queue_id_seq', 4)")
            # The "above" leg needs ROWS: with max(id) NULL the repair skips the table and
            # "never lowered" is never exercised (that is how the first mutation run let
            # "always setval to max" survive).
            await conn.execute(
                "INSERT INTO pending_writes (id, memory_name, title, body) "
                "VALUES (3, 'seed', 'seed', 'seed')"
            )
            await conn.execute("SELECT setval('pending_writes_id_seq', 5000)")
        before_eq, before_pw = _repairs("eviction_queue"), _repairs("pending_writes")

        from mori_advisor.store.postgres_store import PostgresStore

        booted = PostgresStore(PG_URL)
        await booted.bootstrap()  # apply_postgres -> repair under the advisory lock
        await booted.pool.close()

        assert _repairs("eviction_queue") == before_eq + 1
        assert _repairs("pending_writes") == before_pw
        async with h.store.pool.acquire() as conn:
            new_id = await conn.fetchval(
                "INSERT INTO eviction_queue (memory_name, reason) VALUES ('after', 'orphan') "
                "RETURNING id"
            )
            pw_last = await conn.fetchval("SELECT last_value FROM pending_writes_id_seq")
        assert new_id == 78
        assert pw_last == 5000

    run("postgres", tmp_path, t)


def test_sqlite_needs_no_sequence_repair(tmp_path):
    """SQLite control: rowid allocation is max(rowid)+1 (floored by sqlite_sequence), so explicit
    imported ids cannot collide — proved, not asserted."""

    async def t(h):
        conn = h.mem._get_conn()
        try:
            conn.executemany(
                "INSERT INTO eviction_queue (id, memory_name, reason) VALUES (?, ?, 'orphan')",
                [(i, f"seed-{i}") for i in range(1, 78)],
            )
            cur = conn.execute(
                "INSERT INTO eviction_queue (memory_name, reason) VALUES ('after', 'orphan')"
            )
            conn.commit()
            assert cur.lastrowid == 78
        finally:
            conn.close()

    run("sqlite", tmp_path, t)


# ── Addition 2: no swallowed exception inside a transaction block ────────────


def _txn_ctx(item: ast.withitem) -> bool:
    call = item.context_expr
    return (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr in ("transaction", "begin_transaction")
    )


def _reraises(handler: ast.ExceptHandler) -> bool:
    return any(isinstance(n, ast.Raise) for n in ast.walk(handler))


def _is_savepoint_try(node: ast.Try) -> bool:
    """A try whose body is exactly one nested transaction block: the error rolled back that
    savepoint, not the enclosing transaction — the sanctioned pattern."""
    return (
        len(node.body) == 1
        and isinstance(node.body[0], ast.AsyncWith)
        and any(_txn_ctx(i) for i in node.body[0].items)
    )


def _swallows_in_txn(tree: ast.AST) -> list[int]:
    found: list[int] = []

    def visit(node: ast.AST, in_txn: bool) -> None:
        if isinstance(node, ast.AsyncWith) and any(_txn_ctx(i) for i in node.items):
            in_txn = True
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            and node is not tree
        ):
            in_txn = False  # a nested function body runs later, not inside this block
        if in_txn and isinstance(node, ast.Try) and not _is_savepoint_try(node):
            found.extend(h.lineno for h in node.handlers if not _reraises(h))
        for child in ast.iter_child_nodes(node):
            visit(child, in_txn)

    visit(tree, False)
    return found


def test_no_exception_is_swallowed_inside_a_transaction_block():
    """Addition 2 (generalises A-F-A1): on Postgres a swallowed error inside a transaction turns
    its COMMIT into a silent ROLLBACK. Allowed: re-raise, or a try around exactly one nested
    savepoint (the error rolled back the savepoint only)."""
    offenders = {}
    for path in sorted((REPO / "mori_advisor").rglob("*.py")):
        lines = _swallows_in_txn(ast.parse(path.read_text(), filename=str(path)))
        if lines:
            offenders[str(path.relative_to(REPO))] = lines
    assert offenders == {}, offenders


def test_swallow_lint_catches_the_a_f_a1_shape():
    """Positive control for the lint: the pre-v2.3.11 _write shape is flagged; savepoints are not."""
    bad = ast.parse(
        "async def f(conn):\n"
        "    async with conn.transaction():\n"
        "        try:\n"
        "            await conn.execute('x')\n"
        "        except Exception as ae:\n"
        "            if 'does not exist' not in str(ae):\n"
        "                raise\n"
        "        try:\n"
        "            await conn.execute('y')\n"
        "        except Exception:\n"
        "            pass\n"
    )
    assert _swallows_in_txn(bad) == [10]  # the conditional re-raise counts as re-raising
    ok = ast.parse(
        "async def f(conn):\n"
        "    async with conn.transaction():\n"
        "        try:\n"
        "            async with conn.transaction():\n"
        "                await conn.execute('x')\n"
        "        except Exception:\n"
        "            pass\n"
    )
    assert _swallows_in_txn(ok) == []


@requires_pg
def test_pg_a_second_repairing_boot_is_visible(tmp_path):
    """Board condition (v2.3.11 build ruling): the per-process counter cannot show a SECOND repair —
    each new process starts at 0 and repairs before its first scrape. The repair time is persisted
    in dream_state and exported at scrape; a later repairing boot moves it forward, a clean boot
    leaves it alone."""
    import time

    from mori_advisor.store.postgres_store import PostgresStore

    def gauge():
        return mx.prom_registry.get_sample_value("mori_sequence_last_repair_timestamp_seconds")

    async def boot_with_lag(h, lag: bool):
        if lag:
            async with h.store.pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO eviction_queue (id, memory_name, reason) "
                    "SELECT g, 'seed-' || g, 'orphan' FROM generate_series("
                    "(SELECT COALESCE(max(id), 0) + 1 FROM eviction_queue), "
                    "(SELECT COALESCE(max(id), 0) + 10 FROM eviction_queue)) g"
                )
                await conn.execute("SELECT setval('eviction_queue_id_seq', 1)")
        booted = PostgresStore(PG_URL)
        await booted.bootstrap()
        await booted.pool.close()
        await mx.collect_metrics(h.store)
        return await _a(h.store.get_dream_state("last_sequence_repair_at"))

    async def t(h):
        await h.q("DELETE FROM dream_state WHERE key = 'last_sequence_repair_at'")
        first = await boot_with_lag(h, lag=True)
        assert first and abs(float(first) - time.time()) < 60
        assert gauge() == float(first)
        assert await boot_with_lag(h, lag=False) == first  # a clean boot changes nothing
        time.sleep(1.1)
        second = await boot_with_lag(h, lag=True)
        assert float(second) > float(first)
        assert gauge() == float(second)

    run("postgres", tmp_path, t)


def test_a_failed_repair_timestamp_read_keeps_the_last_known_value(tmp_path, monkeypatch, caplog):
    """Board (tag-condition ruling): a failed dream_state read must not reset the gauge to 0
    ("never repaired") — that would silence MoriSequenceRepaired. Positive control: a successful
    read of an absent key does set 0."""
    import logging

    async def t(h):
        real = h.store.get_dream_state

        def failing(key, *a, **k):
            if key == "last_sequence_repair_at":
                raise RuntimeError("injected read failure")
            return real(key, *a, **k)

        mx._sequence_last_repair.set(1_791_000_000)
        monkeypatch.setattr(h.store, "get_dream_state", failing)
        with caplog.at_level(logging.WARNING):
            await mx.collect_metrics(h.store)
        sample = mx.prom_registry.get_sample_value("mori_sequence_last_repair_timestamp_seconds")
        assert sample == 1_791_000_000
        assert any("last_sequence_repair_at" in r.getMessage() for r in caplog.records)
        monkeypatch.setattr(h.store, "get_dream_state", real)
        await mx.collect_metrics(h.store)
        sample = mx.prom_registry.get_sample_value("mori_sequence_last_repair_timestamp_seconds")
        assert sample == 0  # control: never repaired on this store

    run("sqlite", tmp_path, t)
