"""B1 v2 (board consolidated ruling 2026-11-01): decision messages go to review; the post-compact brief gets the brief's
scope. Written before the fix (RED), on both backends.

Binding build requirements (Cursor, via the board):
  1. the pending row is named from the MESSAGE ID, never a slug of the title (a slug could collide with, and on
     approval overwrite, an existing memory);
  2. approval keeps actor="msg" + the sending host (the approval audit keeps the reviewer); proposed_by "msg:<host>";
  3. memory_pending_list shows the msg id and the raw provenance.
"""

from __future__ import annotations

import ast
import inspect
import json
import uuid
from pathlib import Path

import pytest

from tests.test_mcp_tools import BACKENDS, _run_with_backend, _seed_memory, _stub_brief_env

ROOT = Path(__file__).resolve().parent.parent


async def _a(x):
    return await x if inspect.isawaitable(x) else x


async def _sql(store, sql_sqlite: str, sql_pg: str, *args):
    """Run one statement on whichever backend the test is using."""
    if hasattr(store, "pool") and store.pool is not None:
        async with store.pool.acquire() as conn:
            await conn.execute(sql_pg, *args)
    else:
        conn = store._mem._get_conn()
        conn.execute(sql_sqlite, args)
        conn.commit()
        conn.close()


async def _seed_mix(store):
    await _seed_memory(store, name="this-proj", tags=["project:mori"])
    await _seed_memory(store, name="other-proj", tags=["project:other"])
    await _seed_memory(store, name="untagged-row", tags=[])
    await _seed_memory(store, name="untagged-pattern", type="pattern", tags=[])
    await _seed_memory(store, name="explicit-global", tags=["scope:global"])


def _names_listed(out: str, names) -> set:
    return {n for n in names if f"**{n}**" in out}


MIX = ("this-proj", "other-proj", "untagged-row", "untagged-pattern", "explicit-global")


# ── F2: the post-compact brief uses the brief's scope ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_post_compact_unscoped_safe_lists_only_the_global_lane_and_counts_the_rest(
    backend, tmp_path, monkeypatch
):
    import mori_advisor.main as m

    async def run(store):
        await _seed_mix(store)
        _stub_brief_env(m, monkeypatch, store, tmp_path)
        out = await m.brief(project=None, post_compact=True, since="1h")
        assert _names_listed(out, MIX) == {"explicit-global"}, out
        assert "withheld" in out.lower(), "out-of-scope changes must be counted, never named"

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_post_compact_project_safe_matches_the_normal_brief_membership(
    backend, tmp_path, monkeypatch
):
    import mori_advisor.main as m

    async def run(store):
        await _seed_mix(store)
        _stub_brief_env(m, monkeypatch, store, tmp_path)
        delta = await m.brief(project="mori", post_compact=True, since="1h")
        normal = await m.brief(project="mori", scope="safe")
        assert _names_listed(delta, MIX) == {"this-proj", "explicit-global"}, delta
        # Property: everything the safe delta lists, the safe brief for the same project also surfaces.
        assert _names_listed(delta, MIX) <= {n for n in MIX if n in normal}

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_post_compact_scope_all_keeps_the_legacy_opt_in(backend, tmp_path, monkeypatch):
    import mori_advisor.main as m

    async def run(store):
        await _seed_mix(store)
        _stub_brief_env(m, monkeypatch, store, tmp_path)
        out = await m.brief(project="mori", post_compact=True, since="1h", scope="all")
        assert {"this-proj", "explicit-global", "untagged-pattern"} <= _names_listed(out, MIX), out

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_post_compact_scope_fails_closed_on_an_unknown_value(backend, tmp_path, monkeypatch):
    import mori_advisor.main as m

    async def run(store):
        await _seed_mix(store)
        _stub_brief_env(m, monkeypatch, store, tmp_path)
        out = await m.brief(project="mori", post_compact=True, since="1h", scope="saef")
        assert "untagged-pattern" not in _names_listed(out, MIX), (
            "anything but 'all' must be treated as safe"
        )

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_post_compact_filters_before_the_cap(backend, tmp_path, monkeypatch):
    import mori_advisor.main as m

    async def run(store):
        for i in range(40):  # out-of-scope churn
            await _seed_memory(store, name=f"other-{i}", tags=["project:other"])
        await _seed_memory(store, name="in-scope-late", tags=["project:mori"])
        _stub_brief_env(m, monkeypatch, store, tmp_path)
        out = await m.brief(project="mori", post_compact=True, since="1h")
        assert "**in-scope-late**" in out, "out-of-scope rows must not consume the cap"
        assert "other-0" not in out

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_post_compact_superseded_feed_is_scoped(backend, tmp_path, monkeypatch):
    import mori_advisor.main as m

    async def run(store):
        await _seed_memory(store, name="mine-old", tags=["project:mori"])
        await _seed_memory(store, name="theirs-old", tags=["project:other"])
        for name in ("mine-old", "theirs-old"):
            await _sql(
                store,
                "UPDATE memories SET superseded_by = 'x-new', updated_at = datetime('now') WHERE name = ?",
                "UPDATE memories SET superseded_by = 'x-new', updated_at = now() WHERE name = $1",
                name,
            )
        _stub_brief_env(m, monkeypatch, store, tmp_path)
        out = await m.brief(project="mori", post_compact=True, since="1h")
        assert "mine-old" in out
        assert "theirs-old" not in out, (
            "a supersession outside the scope must be counted, not listed"
        )

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_post_compact_error_text_is_static(backend, tmp_path, monkeypatch):
    import mori_advisor.main as m

    async def run(store):
        _stub_brief_env(m, monkeypatch, store, tmp_path)

        def boom(*a, **k):
            raise RuntimeError("SECRET-INTERNAL-DETAIL /path/to/db")

        monkeypatch.setattr(m.store, "get_memories_changed_window", boom, raising=False)
        out = await m.brief(project="mori", post_compact=True, since="1h")
        assert "SECRET-INTERNAL-DETAIL" not in out

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── F1: decision messages go to review ────────────────────────────────────────────────────────────────────────────


def _decision(body: str, sender: str = "peer-device", msg_id: str | None = None) -> dict:
    return {
        "id": msg_id or str(uuid.uuid4()),
        "from": sender,
        "to": "this-host",
        "type": "decision",
        "ts": "2026-11-01T12:00:00+00:00",
        "body": body,
    }


async def _dispatch(store, msg: dict, tmp_path: Path, monkeypatch):
    from mori_advisor import msg_daemon
    from mori_advisor.msg_store import MsgStore

    monkeypatch.setattr(msg_daemon, "HOSTNAME", "this-host")
    ms = MsgStore(db_path=tmp_path / "msg.db")
    await msg_daemon._dispatch(msg, "mori.msg.this-host", ms, store)
    return ms


async def _pending_rows(store) -> list[dict]:
    if hasattr(store, "pool") and store.pool is not None:
        async with store.pool.acquire() as conn:
            return [dict(r) for r in await conn.fetch("SELECT * FROM pending_writes ORDER BY id")]
    conn = store._mem._get_conn()
    conn.row_factory = __import__("sqlite3").Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM pending_writes ORDER BY id")]
    conn.close()
    return rows


@pytest.mark.parametrize("backend", BACKENDS)
def test_decision_is_queued_for_review_named_from_the_message_id(backend, tmp_path, monkeypatch):
    async def run(store):
        mid = str(uuid.uuid4())
        await _dispatch(
            store, _decision("switch the gateway to provider X", msg_id=mid), tmp_path, monkeypatch
        )
        rows = [r for r in await _pending_rows(store) if r["status"] == "pending"]
        assert len(rows) == 1
        r = rows[0]
        assert mid in r["memory_name"], "the pending row must be named from the message id"
        assert r["proposed_by"] == "msg:peer-device"
        assert set(json.loads(r["tags"]) if isinstance(r["tags"], str) else r["tags"]) >= {
            "decision-msg",
            "from:peer-device",
        }
        assert (
            await _a(store.read(r["memory_name"])) in (None, "")
            or "not found" in str(await _a(store.read(r["memory_name"]))).lower()
        ), "nothing reaches memories before approval"

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_decision_whose_title_slugs_onto_an_existing_memory_never_touches_it(
    backend, tmp_path, monkeypatch
):
    async def run(store):
        await _seed_memory(
            store, name="switch-the-gateway-to-provider-x", body="ORIGINAL", tags=["project:mori"]
        )
        await _dispatch(store, _decision("switch the gateway to provider X"), tmp_path, monkeypatch)
        r = [r for r in await _pending_rows(store) if r["status"] == "pending"][0]
        assert r["memory_name"] != "switch-the-gateway-to-provider-x"
        await _a(store.approve(r["id"], reviewer="human"))
        assert "ORIGINAL" in str(await _a(store.read("switch-the-gateway-to-provider-x")))

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_approval_keeps_actor_msg_and_the_host_and_records_the_reviewer(
    backend, tmp_path, monkeypatch
):
    async def run(store):
        await _dispatch(store, _decision("adopt rule R"), tmp_path, monkeypatch)
        r = [r for r in await _pending_rows(store) if r["status"] == "pending"][0]
        res = await _a(store.approve(r["id"], reviewer="human-reviewer"))
        assert "approved" in res.lower(), res
        audit = await _a(store.get_audit_log(memory_name=r["memory_name"]))
        rows = audit if isinstance(audit, list) else []
        assert any(a.get("actor_key_name") == "peer-device" for a in rows), rows
        assert not any(a.get("actor_key_name") == "governed-promotion" for a in rows), (
            "origin must not be laundered"
        )
        assert any("human-reviewer" in (a.get("detail") or "") for a in rows), (
            "the approval audit keeps the reviewer"
        )

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_pending_list_shows_the_message_id_and_raw_provenance(backend, tmp_path, monkeypatch):
    async def run(store):
        mid = str(uuid.uuid4())
        await _dispatch(store, _decision("adopt rule S", msg_id=mid), tmp_path, monkeypatch)
        out = await _a(store.pending_list())
        assert mid in out and "msg_daemon" in out, out

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_at_most_twenty_pending_decisions_per_sender(backend, tmp_path, monkeypatch):
    async def run(store):
        ms = None
        ids = [str(uuid.uuid4()) for _ in range(21)]
        for i in ids:
            ms = await _dispatch(store, _decision(f"decision {i}", msg_id=i), tmp_path, monkeypatch)
        pending = [r for r in await _pending_rows(store) if r["status"] == "pending"]
        assert len(pending) == 20
        conn = ms._get_conn()
        status = conn.execute("SELECT status FROM msg_log WHERE id = ?", (ids[-1],)).fetchone()[0]
        conn.close()
        assert status == "rejected-queue-full"

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_unreviewed_decision_reaches_no_brief_mode(backend, tmp_path, monkeypatch):
    import mori_advisor.main as m

    async def run(store):
        await _seed_memory(store, name="anchor", tags=["project:mori"])
        await _dispatch(store, _decision("B1-marker-91c switch provider"), tmp_path, monkeypatch)
        _stub_brief_env(m, monkeypatch, store, tmp_path)
        for kw in (
            {"project": None},
            {"project": "mori"},
            {"project": None, "post_compact": True, "since": "1h"},
            {"project": "mori", "post_compact": True, "since": "1h"},
        ):
            assert "B1-marker-91c" not in await m.brief(**kw), kw
        assert "awaiting review" in (await m.brief(project="mori")).lower(), (
            "count-only pending line"
        )

    _run_with_backend(backend, tmp_path, monkeypatch, run)


def test_msg_daemon_never_writes_memories_directly():
    """Standing tripwire: no call to memory_store.write / store.write in the msg daemon."""
    tree = ast.parse((ROOT / "mori_advisor" / "msg_daemon.py").read_text())
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "write"
    ]
    assert not calls, f"msg_daemon calls .write() at lines {[c.lineno for c in calls]}"


def test_msg_daemon_uses_the_configured_backend():
    """The daemon must write where the server reads (Postgres in production), not a hard-coded SQLite file."""
    src = (ROOT / "mori_advisor" / "msg_daemon.py").read_text()
    assert "get_store(" in src
    assert "MemoryStore(db_path" not in src


# ── tests added by the mutation pass (survivors of run 1) ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_post_compact_cap_cannot_drop_an_older_in_scope_change(backend, tmp_path, monkeypatch):
    """The in-scope change is the OLDEST in the window; 40 newer out-of-scope changes must not push it out."""
    import mori_advisor.main as m

    async def run(store):
        await _seed_memory(store, name="in-scope-early", tags=["project:mori"])
        await _sql(
            store,
            "UPDATE memories SET updated_at = datetime('now', '-30 minutes') WHERE name = ?",
            "UPDATE memories SET updated_at = now() - interval '30 minutes' WHERE name = $1",
            "in-scope-early",
        )
        for i in range(40):
            await _seed_memory(store, name=f"newer-other-{i}", tags=["project:other"])
        _stub_brief_env(m, monkeypatch, store, tmp_path)
        out = await m.brief(project="mori", post_compact=True, since="1h")
        assert "**in-scope-early**" in out, "filtering must happen before the cap"

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_post_compact_unresolvable_eviction_is_counted_not_listed(backend, tmp_path, monkeypatch):
    import mori_advisor.main as m

    async def run(store):
        await _sql(
            store,
            "INSERT INTO eviction_queue (memory_name, reason, detected_at) VALUES (?, 'stale', datetime('now'))",
            "INSERT INTO eviction_queue (memory_name, reason, detected_at) VALUES ($1, 'stale', now())",
            "ghost-memory-b1",
        )
        _stub_brief_env(m, monkeypatch, store, tmp_path)
        out = await m.brief(project="mori", post_compact=True, since="1h")
        assert "ghost-memory-b1" not in out, "a row that cannot be resolved must fail closed"
        assert "withheld" in out.lower()

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_post_compact_project_lane_is_tier_gated_like_the_brief(backend, tmp_path, monkeypatch):
    import mori_advisor.main as m

    async def run(store):
        await _seed_memory(store, name="proj-ephemeral", tier="ephemeral", tags=["project:mori"])
        _stub_brief_env(m, monkeypatch, store, tmp_path)
        out = await m.brief(project="mori", post_compact=True, since="1h")
        assert "**proj-ephemeral**" not in out, "the project lane takes canonical/working only"

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_memory_already_holding_the_message_id_name_refuses_the_decision(
    backend, tmp_path, monkeypatch
):
    async def run(store):
        mid = str(uuid.uuid4())
        await _seed_memory(
            store, name=f"decision-msg-{mid}", body="PRE-EXISTING", tags=["project:mori"]
        )
        ms = await _dispatch(store, _decision("hijack attempt", msg_id=mid), tmp_path, monkeypatch)
        assert not [r for r in await _pending_rows(store) if r["status"] == "pending"]
        conn = ms._get_conn()
        status = conn.execute("SELECT status FROM msg_log WHERE id = ?", (mid,)).fetchone()[0]
        conn.close()
        assert status == "rejected-name-exists"
        assert "PRE-EXISTING" in str(await _a(store.read(f"decision-msg-{mid}")))

    _run_with_backend(backend, tmp_path, monkeypatch, run)
