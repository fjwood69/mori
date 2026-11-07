"""v2.3.16 canon lock — the rest of the ruling's tests (see tests/test_canon_lock.py for the core).

R5 startup, the request-boundary lock, reserved key names, the ingestion server's constant-time
comparison, R7, the standards import (token, idempotent, dry run, awaited at startup), V1's brief
tiers (measured, pinned), the review surfaces (R3) and the REST status codes, and R4's actor on the
supersession and protect tools. Both backends wherever a store is involved.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging

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
    api_mode,  # noqa: F401  (fixture)
    as_key,
)
from tests.test_mcp_tools import BACKENDS, _apply_store, _make_sqlite_store, _run_with_backend


async def _a(x):
    return await x if inspect.isawaitable(x) else x


async def _exec(store, sql_sqlite: str, sql_pg: str, *args) -> None:
    """One committed statement on either backend (``_rows`` is for reads)."""
    if hasattr(store, "pool") and store.pool is not None:
        async with store.pool.acquire() as conn:
            await conn.execute(sql_pg, *args)
        return
    conn = store._mem._get_conn()
    try:
        conn.execute(sql_sqlite, args)
        conn.commit()
    finally:
        conn.close()


# ── R5: the mode fails closed ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", [None, "", "host", "HOST"])
def test_keys_without_api_mode_refuse_to_start(monkeypatch, caplog, raw):
    import mori_advisor.policy as pol

    if raw is None:
        monkeypatch.delenv("MORI_TD_MODE", raising=False)
    else:
        monkeypatch.setenv("MORI_TD_MODE", raw)
    monkeypatch.setattr(pol, "_TD_MODE", "host")
    with caplog.at_level(logging.CRITICAL), pytest.raises(SystemExit) as exc:
        pol.assert_mode_fail_closed(keys_configured=True)
    assert "MORI_TD_MODE" in str(exc.value)
    assert any("MORI_TD_MODE" in r.getMessage() for r in caplog.records)


def test_host_mode_without_keys_starts_and_warns(monkeypatch, caplog):
    import mori_advisor.policy as pol

    monkeypatch.setattr(pol, "_TD_MODE", "host")
    with caplog.at_level(logging.WARNING):
        pol.assert_mode_fail_closed(keys_configured=False)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("role checks are OFF" in m for m in msgs), msgs


def test_api_mode_with_keys_starts(monkeypatch, caplog):
    import mori_advisor.policy as pol

    monkeypatch.setattr(pol, "_TD_MODE", "api")
    with caplog.at_level(logging.WARNING):
        pol.assert_mode_fail_closed(keys_configured=True)  # no SystemExit
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_api_mode_without_keys_warns_read_only(monkeypatch, caplog):
    import mori_advisor.policy as pol

    monkeypatch.setattr(pol, "_TD_MODE", "api")
    with caplog.at_level(logging.WARNING):
        pol.assert_mode_fail_closed(keys_configured=False)
    assert any("read-only" in r.getMessage() for r in caplog.records)


def test_the_startup_check_runs_in_main_and_not_in_init_policy():
    """The check lives in __main__ (after init_auth) — the intake process calls init_policy too
    and must be left as it is (ruling, answer 3)."""
    from pathlib import Path

    import mori_advisor.policy as pol

    main_src = (Path(pol.__file__).parent / "main.py").read_text()
    assert "assert_mode_fail_closed(bool(_auth._KEYS))" in main_src
    assert "assert_mode_fail_closed" not in inspect.getsource(pol.init_policy)


# ── C1 P2-2 / P3-2: the request boundary and reserved names ───────────────────────────


@pytest.mark.parametrize("cls", ["init", "governed-promotion", "dreamer", "system", "import"])
def test_request_provenance_mints_only_request_classes(cls):
    from mori_advisor.provenance import request_provenance

    with pytest.raises(ValueError):
        request_provenance(cls, "someone", "tests")
    assert request_provenance("mcp", "k", "tests").actor == "mcp"
    assert request_provenance("rest", "k", "tests").actor == "rest"


@pytest.mark.parametrize(
    "name",
    [
        "anonymous",
        "local",
        "mcp",
        "rest",
        "unknown",
        "legacy",
        "dreamer",
        "dream-pipeline",
        "ingestion",
        "system",
        "init",
        "governed-promotion",
        "intake-assessor",
        "import",
    ],
)
def test_reserved_names_cannot_hold_a_role(monkeypatch, name):
    import mori_advisor.policy as pol

    monkeypatch.setenv("MORI_API_KEY_ROLES", f"{name}:dreamer,real-key:dreamer")
    roles = pol._load_roles()
    assert name not in roles and roles == {"real-key": "dreamer"}


def test_local_full_access_is_the_documented_exception(monkeypatch):
    """api mode, no authenticated actor, MORI_LOCAL_FULL_ACCESS=true: the stated exception."""
    import mori_advisor.policy as pol
    from mori_advisor.canon_authority import has_canon_authority

    monkeypatch.setattr(pol, "_TD_MODE", "api")
    monkeypatch.setattr(pol, "_LOCAL_FULL_ACCESS", True)
    with as_key(None):
        assert has_canon_authority(_req("anyone"))
    monkeypatch.setattr(pol, "_LOCAL_FULL_ACCESS", False)
    with as_key(None):
        assert not has_canon_authority(_req("anyone"))


# ── D5: the ingestion server's key check is constant-time ─────────────────────────────


def test_ingestion_server_key_check_uses_compare_digest(monkeypatch):
    """D5: the comparison is constant-time — since the board's ingestion fix it is the main server's
    ``auth.check_key`` (hmac.compare_digest over the key table)."""
    import hmac

    import mori_advisor.auth as auth
    import mori_advisor.ingestion_server as ing
    import mori_advisor.policy as pol

    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr(auth, "_KEYS", {"k": "right-key"})
    monkeypatch.setattr(pol, "_ROLES", {"k": "write"})
    monkeypatch.setattr(auth.hmac, "compare_digest", spy)

    class _R:
        def __init__(self, k):
            self.headers = {"X-Api-Key": k}

    assert ing.require_api_key(_R("right-key")) == "k"
    with pytest.raises(Exception) as e:
        ing.require_api_key(_R("wrong-key"))
    assert getattr(e.value, "status_code", None) == 401
    assert len(calls) == 2


# ── R7: proposing an eviction needs write ─────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_memory_review_proposing_needs_write(backend, tmp_path, monkeypatch, api_mode):
    from mori_advisor.main import memory_review

    async def run(store):
        await _seed_working(store, "orph")
        await _exec(
            store,
            "UPDATE memories SET last_retrieved_at = datetime('now','-90 days') WHERE name = ?",
            "UPDATE memories SET last_retrieved_at = NOW() - interval '90 days' WHERE name = $1",
            "orph",
        )
        with as_key(READ_KEY):
            out = await memory_review(orphan_days=30, dry_run=False)
        assert "write" in out.lower() and "required" in out.lower(), out
        with as_key(READ_KEY):
            await memory_review(orphan_days=30, dry_run=True)  # a read key may still look
        q = "SELECT COUNT(*) AS n FROM eviction_queue WHERE memory_name = {} AND reason = 'orphan'"
        n0 = await _rows(store, q.format("?"), q.format("$1"), "orph")
        assert n0[0]["n"] == 0
        for _ in range(2):
            with as_key(WRITE_KEY):
                await memory_review(orphan_days=30, dry_run=False)
        n = await _rows(store, q.format("?"), q.format("$1"), "orph")
        assert n[0]["n"] == 1, "one OPEN orphan proposal per memory, however often the scan runs"

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── Ruling item 9: the standards import ───────────────────────────────────────────────


def _standards_dir(tmp_path, monkeypatch, files: dict):
    import mori_advisor.main as m

    d = tmp_path / "standards"
    for rel, text in files.items():
        f = d / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    monkeypatch.setattr(m, "_is_within_standards_roots", lambda p: True)
    return str(d)


@pytest.mark.parametrize("backend", BACKENDS)
def test_standards_import_is_idempotent_and_lands_canon(backend, tmp_path, monkeypatch):
    import mori_advisor.main as m

    src = _standards_dir(
        tmp_path, monkeypatch, {"security/baseline.md": "# Baseline\nUse TLS.\n", "style.md": "S"}
    )

    async def run(store):
        out = await m.import_standards(standards_dir=src)
        assert out.startswith("Imported 2 standards"), out
        row = await _active(store, "security-baseline")
        assert row["tier"] == "canonical"
        versions_q = "SELECT COUNT(*) AS n FROM memory_versions WHERE memory_name = {}"
        before = await _rows(store, versions_q.format("?"), versions_q.format("$1"), "style")
        again = await m.import_standards(standards_dir=src)
        assert again.startswith("Imported 0 standards") and "2 unchanged" in again, again
        after = await _rows(store, versions_q.format("?"), versions_q.format("$1"), "style")
        assert before == after, "an unchanged standard must not be rewritten (no new version)"
        assert await _pending(store) == []

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_standards_dry_run_lists_and_writes_nothing(backend, tmp_path, monkeypatch):
    import mori_advisor.main as m

    src = _standards_dir(tmp_path, monkeypatch, {"a.md": "A", "b.md": "B", "c.md": "C"})

    async def run(store):
        await m.import_standards(standards_dir=src)
        (tmp_path / "standards" / "b.md").write_text("B changed")
        (tmp_path / "standards" / "d.md").write_text("D")
        plan = await m.import_standards(standards_dir=src, dry_run=True)
        assert "- unchanged: a" in plan and "- change: b" in plan and "- create: d" in plan, plan
        assert "1 to create, 1 to change" in plan, plan
        assert await _active(store, "d") is None and (await _active(store, "b"))["body"] == "B"

    _run_with_backend(backend, tmp_path, monkeypatch, run)


def test_the_startup_import_is_awaited_in_the_lifespan(tmp_path, monkeypatch):
    """Before v2.3.16 the startup call created a coroutine and never ran it."""
    import mori_advisor.main as m

    store = _make_sqlite_store(tmp_path)
    _apply_store(monkeypatch, store)
    # The lifespan shuts main's LLM executor down on exit: let monkeypatch put it back, so no later
    # test inherits a shut-down executor (as tests/test_v2312_hardening.py's isolation fixture does).
    monkeypatch.setattr(m, "_llm_executor", m._llm_executor)
    calls = []

    async def fake_import(*a, **k):
        calls.append(1)
        return "Imported 0 standards"

    monkeypatch.setattr(m, "STANDARDS_DIR", str(tmp_path))
    monkeypatch.setattr(m, "import_standards", fake_import)

    async def run():
        async with m._lifespan(None):
            pass

    asyncio.run(run())
    assert calls == [1]


# ── V1 (measured, not inferred): which tiers reach /brief ─────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_v1_brief_project_lane_lists_working_rows_as_index_lines(backend, tmp_path, monkeypatch):
    """MEASURED 7 Nov (pinned, not changed — the board decides V1): the project lane takes
    canonical AND working rows; a working row is listed as `name: title (working)`, its BODY is not
    injected. Canonical rows are listed the same way (the "full body" comment is wrong)."""
    from mori_advisor.main import brief
    from mori_advisor.provenance import SYSTEM

    async def run(store):
        await _a(
            _ms(store)._write(
                name="w-row",
                title="Working title",
                tier="working",
                body="SECRET-WORKING-BODY",
                description="d",
                tags=["project:demo"],
                provenance=SYSTEM,
            )
        )
        await _seed_canon(store, "c-row", body="SECRET-CANON-BODY")
        await _exec(
            store,
            "UPDATE memories SET tags = '[\"project:demo\"]' WHERE name = ?",
            "UPDATE memories SET tags = '[\"project:demo\"]'::jsonb WHERE name = $1",
            "c-row",
        )
        out = await brief(project="demo")
        assert "**w-row**: Working title (working)" in out, out
        assert "**c-row**" in out and "(canonical)" in out
        assert "SECRET-WORKING-BODY" not in out and "SECRET-CANON-BODY" not in out

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_v1_global_lane_has_no_tier_filter(backend, tmp_path, monkeypatch):
    """MEASURED 7 Nov (pinned): the global lane selects any tier — an ephemeral row tagged
    scope:global reaches the global-lane candidates. The next release's first item."""
    from mori_advisor.provenance import SYSTEM

    async def run(store):
        await _a(
            _ms(store)._write(
                name="eph-global",
                title="E",
                tier="ephemeral",
                body="b",
                description="d",
                tags=["scope:global"],
                provenance=SYSTEM,
            )
        )
        lanes = await _a(
            _ms(store).filter_by_scope("demo", include_global=True, strict_global=True)
        )
        names = {r["name"] for r in lanes["global_memories"]}
        assert "eph-global" in names, lanes

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── R3: the review surfaces ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_pending_json_shows_operation_diff_and_chain(backend, tmp_path, monkeypatch, api_mode):
    from mori_advisor.main import memory_delete

    async def run(store):
        await _seed_canon(store, "canon-p", body="line one\nline two")
        for body in ("line one\nline TWO", "line one\nline 2"):
            with as_key(WRITE_KEY):
                await _a(
                    _ms(store)._write(
                        name="canon-p",
                        title="canon-p title",
                        description="seeded canon for a test",
                        type="decision",
                        tier="canonical",
                        body=body,
                        tags=["test"],
                        provenance=_req(WRITE_KEY),
                    )
                )
        await _seed_canon(store, "canon-del", body="to be removed")
        with as_key(WRITE_KEY):
            await memory_delete("canon-del")
        items = {i["name"]: i for i in await _a(_ms(store).pending_list_json(status="pending"))}
        w = items["canon-p"]
        assert w["operation"] == "write" and w["diff"]["kind"] == "write"
        assert "-line two" in w["diff"]["body_diff"] and "+line 2" in w["diff"]["body_diff"]
        assert [c["status"] for c in w["chain"]] == ["superseded-by-proposal"]
        assert w["proposer_changed"] is False
        d = items["canon-del"]
        assert d["operation"] == "delete" and d["diff"]["current_body"] == "to be removed"
        text = await _a(_ms(store).pending_list())
        assert "operation: delete" in text and "operation: write" in text, text

    _run_with_backend(backend, tmp_path, monkeypatch, run)


def test_proposer_changed_flags_a_shadow():
    from mori_advisor.proposal_view import review_fields

    f = review_fields(
        {"operation": "write", "body": "x", "proposed_by": "b"},
        {"body": "y"},
        [{"id": 1, "proposed_by": "a", "status": "superseded-by-proposal"}],
    )
    assert f["proposer_changed"] is True


@pytest.mark.parametrize("backend", BACKENDS)
def test_rest_approve_status_codes(backend, tmp_path, monkeypatch, api_mode):
    import uuid

    from mori_advisor.main import approve_memory
    from tests.test_write_api import _fake_request

    name = f"canon-rest-{uuid.uuid4().hex[:8]}"  # write_audit persists across PG tests

    async def run(store):
        await _seed_canon(store, name, body="original")
        with as_key(WRITE_KEY):
            r = await _a(
                _ms(store)._write(
                    name=name,
                    title="t",
                    body="new",
                    tier="canonical",
                    provenance=_req(WRITE_KEY),
                )
            )
        with as_key(DREAM_KEY):
            wrong = await approve_memory(
                _fake_request({"write_id": r.pending_id}, path_params={"name": "other-name"})
            )
        assert wrong.status_code == 404
        await _seed_canon(store, name, body="moved")
        with as_key(DREAM_KEY):
            stale = await approve_memory(
                _fake_request(
                    {"write_id": r.pending_id, "reviewer": "says-fred"},
                    path_params={"name": name},
                )
            )
        assert stale.status_code == 409 and json.loads(stale.body)["reason"] == "stale-proposal"
        assert [a for a in await _audit(store, "approve") if a["memory_name"] == name] == []

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_rest_propose_refused_is_409_with_the_open_id(backend, tmp_path, monkeypatch, api_mode):
    from mori_advisor.main import post_memory
    from tests.test_write_api import _fake_request

    async def run(store):
        await _seed_canon(store, "canon-r", body="original")
        with as_key(WRITE_KEY):
            first = await post_memory(_fake_request({"name": "canon-r", "body": "mine"}))
        assert first.status_code == 202
        pid = json.loads(first.body)["pending_id"]
        with as_key(OTHER_KEY):
            other = await post_memory(_fake_request({"name": "canon-r", "body": "theirs"}))
        assert other.status_code == 409 and json.loads(other.body)["pending_id"] == pid
        assert [p["proposed_by"] for p in await _pending(store, "canon-r")] == [WRITE_KEY]

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── R4: the supersession and protect tools name the authenticated key ─────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_protect_and_supersession_tools_audit_the_key(backend, tmp_path, monkeypatch, api_mode):
    import uuid

    from mori_advisor.main import memory_protect, memory_unsupersede

    name = f"canon-s-{uuid.uuid4().hex[:8]}"  # write_audit is not truncated between PG tests

    async def run(store):
        await _seed_canon(store, name, body="b")
        with as_key(DREAM_KEY):
            await memory_protect(name)
        prot = [a for a in await _audit(store, "protect") if a["memory_name"] == name]
        assert [a["actor_key_name"] for a in prot] == [DREAM_KEY]
        await _exec(
            store,
            "UPDATE memories SET superseded_by = 'x' WHERE name = ?",
            "UPDATE memories SET superseded_by = 'x' WHERE name = $1",
            name,
        )
        with as_key(DREAM_KEY):
            await memory_unsupersede(name, note="n")
        un = [a for a in await _audit(store, "unsupersede") if a["memory_name"] == name]
        assert [a["actor_key_name"] for a in un] == [DREAM_KEY]

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── R8: the dream pipeline's own write path ───────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_dream_pipeline_write_memory_cannot_change_canon(backend, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from mori_advisor.dream import DreamPipeline

    async def run(store):
        await _seed_canon(store, "canon-dream", body="original")
        owner = SimpleNamespace(store=store, _infer_type=lambda p: "project")
        r = await DreamPipeline._write_memory(
            owner,
            {"path": "canon-dream.md", "body": "dreamed", "reason": "r"},
            "canon-dream",
            "UPDATE",
            [],
            [],
        )
        assert not r.accepted
        row = await _active(store, "canon-dream")
        assert (row["body"], row["tier"]) == ("original", "canonical")
        assert [p["proposed_by"] for p in await _pending(store, "canon-dream")] == [
            "dream-pipeline"
        ]

    _run_with_backend(backend, tmp_path, monkeypatch, run)


with contextlib.suppress(Exception):  # keep the imported fixture referenced for linters
    api_mode  # noqa: B018


# ── Mutation-run follow-ups (survivors A3, T3, T4 of the first run) ───────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_host_mode_queues_even_a_key_configured_as_dreamer(backend, tmp_path, monkeypatch):
    """A3: host mode confers nothing, even when the key's configured role is dreamer — the mode
    check, not the role lookup, is what refuses (roles are not consulted in host mode)."""
    import mori_advisor.policy as pol

    monkeypatch.setattr(pol, "_TD_MODE", "host")
    monkeypatch.setattr(pol, "_ROLES", {DREAM_KEY: "dreamer"})

    async def run(store):
        await _seed_canon(store, "canon-h1", body="original")
        with as_key(DREAM_KEY):
            r = await _a(
                _ms(store)._write(
                    name="canon-h1",
                    title="t",
                    body="x",
                    tier="canonical",
                    provenance=_req(DREAM_KEY),
                )
            )
        assert not r.accepted
        assert (await _active(store, "canon-h1"))["body"] == "original"

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_canon_rule_runs_before_the_anatomy_downgrade(backend, tmp_path, monkeypatch, api_mode):
    """T3 / consult C1 P3-3 condition 1: under MORI_ANATOMY_ENFORCE=enforce a thin write to a
    canonical name is a CANON proposal (tier canonical), not an anatomy downgrade at working."""
    monkeypatch.setenv("MORI_ANATOMY_ENFORCE", "enforce")

    async def run(store):
        await _seed_canon(store, "canon-an", body="original")
        with as_key(WRITE_KEY):
            r = await _a(
                _ms(store)._write(
                    name="canon-an",
                    title="t",
                    description="",
                    body="x",
                    tier="working",
                    provenance=_req(WRITE_KEY),
                )
            )
        assert not r.accepted
        pend = await _pending(store, "canon-an")
        assert [p["tier"] for p in pend] == ["canonical"], pend
        assert "anatomy" not in (r.reason or "").lower(), r.reason

    _run_with_backend(backend, tmp_path, monkeypatch, run)


def test_pg_recheck_catches_a_row_turned_canonical_after_the_gate(tmp_path, monkeypatch, api_mode):
    """T4: the Postgres gate reads the tier in its own transaction; if the row becomes canonical
    before the upsert's transaction (a concurrent approval), the in-transaction recheck queues."""
    from tests.test_mcp_tools import PG_URL

    if not PG_URL:
        pytest.skip("MORI_TEST_DATABASE_URL not set")
    import mori_advisor.store.postgres_store as pgs

    real_retry = pgs._retry
    state = {"calls": 0}

    async def racing_retry(fn, conn, *a, **k):
        out = await real_retry(fn, conn, *a, **k)
        state["calls"] += 1
        if state["calls"] == 1:  # right after the gate: a concurrent approval makes it canon
            await conn.execute("UPDATE memories SET tier = 'canonical' WHERE name = 'race-row'")
        return out

    async def run(store):
        await _seed_working(store, "race-row", body="original")
        monkeypatch.setattr(pgs, "_retry", racing_retry)
        with as_key(WRITE_KEY):
            r = await store._write(
                name="race-row",
                title="t",
                body="raced",
                tier="working",
                provenance=_req(WRITE_KEY),
            )
        assert not r.accepted, r
        row = await _active(store, "race-row")
        assert (row["body"], row["tier"]) == ("original", "canonical")

    _run_with_backend("postgres", tmp_path, monkeypatch, run)


# ── Board, 7 Nov (build ruling): two more pins ────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_host_mode_lets_a_caller_without_dreamer_approve(backend, tmp_path, monkeypatch):
    """The public sentence says host mode is NOT the same lock: role checks are off, so any local
    caller can approve. Pinned, so nobody later reads host mode as locked."""
    import mori_advisor.policy as pol
    from mori_advisor.main import memory_approve

    monkeypatch.setattr(pol, "_TD_MODE", "host")
    monkeypatch.setattr(pol, "_ROLES", {WRITE_KEY: "write"})

    async def run(store):
        await _seed_canon(store, "canon-host", body="original")
        with as_key(WRITE_KEY):
            r = await _a(
                _ms(store)._write(
                    name="canon-host",
                    title="t",
                    body="host change",
                    tier="canonical",
                    provenance=_req(WRITE_KEY),
                )
            )
            assert r.pending_id, "host mode still QUEUES the change"
            out = await memory_approve(r.pending_id)
        assert "approved" in out.lower() and "not approved" not in out.lower(), out
        assert (await _active(store, "canon-host"))["body"] == "host change"

    _run_with_backend(backend, tmp_path, monkeypatch, run)


def test_the_authenticated_actor_reaches_the_sqlite_store_thread(tmp_path, monkeypatch, api_mode):
    """Build choice 3: AsyncStore off-loads SQLite calls to a worker thread IN A COPY OF THE CALLER'S
    CONTEXT, so the canon lock's authority and proposer lookups see the authenticated key there."""
    import threading

    from mori_advisor.policy import current_actor
    from mori_advisor.store.async_store import AsyncStore

    backend = _make_sqlite_store(tmp_path)
    seen: dict = {}

    def probe():
        actor = current_actor.get()
        seen["key"] = actor.key_name if actor else None
        seen["thread"] = threading.get_ident()
        return "ok"

    backend.probe_actor = probe  # an off-loaded (sync) backend method
    store = AsyncStore(backend)

    async def run():
        with as_key(DREAM_KEY):
            assert await store.probe_actor() == "ok"
            # end to end: a dreamer key's delete of canon, through AsyncStore, APPLIES
            await _seed_canon(backend, "canon-thread")
            from tests.test_canon_lock import _req as req

            out = await store.soft_delete(
                "canon-thread", actor=DREAM_KEY, provenance=req(DREAM_KEY, op="delete")
            )
        return out

    out = asyncio.run(run())
    assert seen["key"] == DREAM_KEY, "the actor must arrive on the store thread"
    assert seen["thread"] != threading.get_ident(), "the probe must have run off the loop thread"
    assert "soft-deleted" in out, out
    store.aclose()
