"""v2.3.12 — security and correctness hardening (board ruling 2026-10-03, design + consult 7fdb431d).

Measured before this release (api mode, a throwaway server): a session opened with a READ key,
then a request with NO key at all, wrote a server-side file through ``memory_export(output_path)``;
the session id was a never-expiring bearer credential that skipped the rate limiter; keys were
accepted in the URL; ``memory_protect`` toggled on SQLite and could only set on Postgres; names were
never validated (``../../x`` was accepted); ``memory_import`` read any server directory and let a
write key land canonical rows as actor ``import``.

Every role assertion runs with api mode patched in — host mode makes ``require_role`` a no-op, so a
role test that forgets the patch passes vacuously. ``test_api_mode_fixture_is_really_api_mode``
guards the fixture itself. Sessions and URL keys are driven through the REAL app (FastMCP issuing
real session ids) behind the REAL middleware, over HTTP.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from collections import OrderedDict
from pathlib import Path

import httpx
import pytest
from prometheus_client import generate_latest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from test_recovery_path import BACKENDS, GOOD_DESC, H, _a, _version_id
from test_recovery_path import run as _run

import mori_advisor.auth as auth
import mori_advisor.middleware as mw
import mori_advisor.policy as pol
from mori_advisor import bifrost_client as bc
from mori_advisor import metrics as mx
from mori_advisor.names import confined_export_path, invalid_name_reason, normalise_name
from mori_advisor.policy import Actor, PermissionDenied, current_actor, require_role
from mori_advisor.provenance import Provenance
from mori_advisor.throttle import InMemoryRateLimitStore, RateLimitConfig
from mori_advisor.write_result import Disposition

_CONFIG_KEYS = ("protected_tag_prefixes", "trusted_clients")


def run(backend, tmp_path, fn):
    """The v2.3.11 harness, plus: dreamer_config keys these tests set are cleared first (the
    Postgres harness truncates memories/versions/pending/queue/audit, not dreamer_config)."""

    async def clear(h):
        for key in _CONFIG_KEYS:
            await h.q("DELETE FROM dreamer_config WHERE key = $1", key)

    async def wrapped(h):
        await clear(h)
        try:
            return await fn(h)
        finally:
            await clear(h)  # never leak into a later test on the shared Postgres

    return _run(backend, tmp_path, wrapped)


# Distinctive key names, so "no key name appears in /metrics" is a meaningful substring check.
READ_KEY, WRITE_KEY, DREAM_KEY = (
    "keyname-reader-7f3a",
    "keyname-writer-91c2",
    "keyname-dreamer-c0de",
)
SECRETS = {READ_KEY: "r" * 64, WRITE_KEY: "w" * 64, DREAM_KEY: "d" * 64}
ROLES = {READ_KEY: "read", WRITE_KEY: "write", DREAM_KEY: "dreamer"}
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


# ── api-mode fixture (and its guard) ─────────────────────────────────────────


@pytest.fixture
def api_mode(monkeypatch):
    """Real keys, real roles, api mode, an empty session registry. Restored by monkeypatch."""
    monkeypatch.setattr(pol, "_TD_MODE", "api")
    monkeypatch.setattr(pol, "_LOCAL_FULL_ACCESS", False)
    monkeypatch.setattr(pol, "_ROLES", dict(ROLES))
    monkeypatch.setattr(auth, "_KEYS", dict(SECRETS))
    monkeypatch.setattr(mw, "_SESSIONS", OrderedDict())
    monkeypatch.setattr(mw, "_rate_cfg", RateLimitConfig(None, None, "writes"))
    yield


def test_api_mode_fixture_is_really_api_mode(api_mode):
    """Guard: if the fixture stops patching api mode, every role test below becomes vacuous."""
    assert pol._mode() == "api"
    token = current_actor.set(Actor(READ_KEY, "read"))
    try:
        with pytest.raises(PermissionDenied):
            require_role("dreamer")
    finally:
        current_actor.reset(token)


def test_host_mode_would_make_role_tests_vacuous(monkeypatch):
    """Why the guard exists: in host mode a READ actor passes a dreamer check."""
    monkeypatch.setattr(pol, "_TD_MODE", "host")
    token = current_actor.set(Actor(READ_KEY, "read"))
    try:
        require_role("dreamer")  # no raise
    finally:
        current_actor.reset(token)


def test_require_role_control_invalid_role_raises():
    with pytest.raises(ValueError):
        require_role("superuser")


class _As:
    """Run a block as a given key (sets the ContextVar the tools read)."""

    def __init__(self, key):
        self.actor = Actor(key, ROLES[key])

    def __enter__(self):
        self.token = current_actor.set(self.actor)

    def __exit__(self, *exc):
        current_actor.reset(self.token)


def _apply_store(monkeypatch, store):
    import mori_advisor.main as m

    monkeypatch.setattr(m, "store", store)
    monkeypatch.setattr(m, "memory_store", store._mem if hasattr(store, "_mem") else store)


async def _set_config(h: H, key: str, value) -> None:
    await h.q(
        "INSERT INTO dreamer_config (key, value) VALUES ($1, $2) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        key,
        json.dumps(value),
    )


def _files_under(root: Path) -> set[Path]:
    return {p for p in root.rglob("*") if p.is_file()}


# ── D7: names ────────────────────────────────────────────────────────────────

INVALID_NAMES = [
    "../../traversal_test",
    "a/b",
    "a\\b",
    ".hidden",
    "-lead",
    "_lead",
    "a..b",
    "abc\n",
    "with space",
    "naïve",
    "x" * 201,
    "",
]
VALID_NAMES = ["abc", "A1", "a.b", "a_b", "a-b", "mori-v2.3.12", "x" * 200, "a--b", "Team_Notes"]


@pytest.mark.parametrize("name", INVALID_NAMES)
def test_invalid_names_are_refused(name):
    assert invalid_name_reason(name) is not None


@pytest.mark.parametrize("name", VALID_NAMES)
def test_valid_names_pass_and_normalise_to_themselves(name):
    assert invalid_name_reason(name) is None
    assert normalise_name(name) == name  # existing memories keep matching their names


GENERATOR_INPUTS = [
    "project/infra/foo bar.md",
    "gotchas/under_score_name.md",
    "../../etc/passwd",
    "profile/Team's notes: (draft) #1.md",
    "naïve café/résumé.md",
    "日本語/メモ.md",
    "///",
    "...",
    "a" * 400,
    "tabs\tand\nnewlines",
    "emoji 🚀 launch",
    "-leading/_under/.dot",
]


@pytest.mark.parametrize("raw", GENERATOR_INPUTS)
def test_both_name_generators_always_produce_valid_names(raw):
    """D7 caution: the dream's _path_to_name and ingestion's _derive_name over paths, underscores,
    spaces and unicode — every output passes the chokepoint rule; the mapping is stable."""
    from mori_advisor.dream import DreamPipeline
    from mori_advisor.ingestion import IngestionPipeline

    dream = DreamPipeline._path_to_name(None, raw)
    ingest = IngestionPipeline._derive_name(None, {"title": raw})
    for out in (dream, ingest, normalise_name(raw)):
        assert invalid_name_reason(out) is None, (raw, out)
    assert DreamPipeline._path_to_name(None, raw) == dream


def test_derive_name_of_untitled_memory_is_valid():
    from mori_advisor.ingestion import IngestionPipeline

    assert invalid_name_reason(IngestionPipeline._derive_name(None, {})) is None


@pytest.mark.parametrize("backend", BACKENDS)
def test_chokepoint_rejects_a_traversal_name_and_counts_it(backend, tmp_path):
    async def t(h):
        before = (
            mx.prom_registry.get_sample_value(
                "mori_write_rejections_total", {"reason": "invalid_name", "actor": "mcp"}
            )
            or 0
        )
        r = await _a(
            h.mem._write(
                name="../../traversal_test",
                title="t",
                description=GOOD_DESC,
                body="body",
                provenance=Provenance(actor="mcp", actor_detail=WRITE_KEY, source="test"),
            )
        )
        assert r.disposition is Disposition.REJECTED and "must match" in r.reason
        assert await h.q("SELECT name FROM memories") == []
        assert await h.q("SELECT id FROM write_audit") == []
        after = mx.prom_registry.get_sample_value(
            "mori_write_rejections_total", {"reason": "invalid_name", "actor": "mcp"}
        )
        assert after == before + 1

    run(backend, tmp_path, t)


@pytest.mark.parametrize("backend", BACKENDS)
def test_export_all_never_writes_outside_its_directory(backend, tmp_path):
    """Defence in depth: a row stored BEFORE the name rule with a path-like name is skipped."""

    async def t(h):
        await h.write("fine-name", "body")
        legacy = "../../escaped"
        if h.pg:
            await h.q(
                "INSERT INTO memories (name, title, type, tier, body, tags) "
                "VALUES ($1, 't', 'project', 'working', 'b', '[]'::jsonb)",
                legacy,
            )
        else:
            await h.q(
                "INSERT INTO memories (name, title, type, tier, body, tags) "
                "VALUES ($1, 't', 'project', 'working', 'b', '[]')",
                legacy,
            )
        out = tmp_path / "data" / "exports"
        before = _files_under(tmp_path)
        await _a(h.mem.export_all(str(out)))
        new = _files_under(tmp_path) - before
        assert new and all(p.resolve().parent == out.resolve() for p in new), new
        assert not (tmp_path / "escaped.md").exists()
        assert confined_export_path(out, legacy) is None

    run(backend, tmp_path, t)


@pytest.mark.parametrize("backend", BACKENDS)
def test_export_all_does_not_follow_a_symlink_out_of_its_directory(backend, tmp_path):
    """The resolve() check is the layer the name rule cannot provide: a valid name whose file in
    the export directory is a symlink pointing elsewhere."""

    async def t(h):
        await h.write("fine-name", "body")
        out = tmp_path / "exports"
        out.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("UNTOUCHED")
        (out / "fine-name.md").symlink_to(outside)
        await _a(h.mem.export_all(str(out)))
        assert outside.read_text() == "UNTOUCHED"

    run(backend, tmp_path, t)


def test_dream_rejected_write_is_counted_and_persisted(tmp_path, monkeypatch):
    """D7 caution: a REJECTED dream write is counted and reaches /metrics from dream_state (a cron
    run is a separate process). The generator is bypassed here to force a rejection."""
    from mori_advisor.dream import DreamPipeline

    async def t(h):
        dp = DreamPipeline(db_path=tmp_path / "memories.db", bifrost_client=None, store=h.store)
        events = [{"id": 7, "session_id": "s", "client": "c", "event_name": "Stop"}]
        monkeypatch.setattr(dp.session_log, "read_events", lambda **kw: events, raising=False)
        monkeypatch.setattr(dp, "_run_intake_promotion", lambda *a, **k: _noop())
        monkeypatch.setattr(dp, "_format_events", lambda ev: "events")
        monkeypatch.setattr(dp, "_call_dream_model", lambda text: "model output")
        monkeypatch.setattr(dp, "_contradiction_scan", lambda mems: _scan_none())
        monkeypatch.setattr(dp, "_path_to_name", lambda p: p)  # force the invalid name through
        body = "A real body with a reason given for its existence and enough substance."
        monkeypatch.setattr(
            dp,
            "_parse_response",
            lambda r: [
                {"path": "good-one", "body": body, "reason": GOOD_DESC},
                {"path": "../../evil", "body": body, "reason": GOOD_DESC},
            ],
        )
        await dp.run()
        writes = json.loads(await _a(h.store.get_dream_state("last_run_writes")))
        assert writes["accepted"] == 1 and writes["rejected"] == 1, writes
        mx.set_dream_last_writes(writes)  # what the scrape does with the persisted value
        assert (
            mx.prom_registry.get_sample_value("mori_dream_last_run_writes", {"result": "rejected"})
            == 1
        )

    run("sqlite", tmp_path, t)


async def _noop():
    return None


async def _scan_none():
    from mori_advisor.utils import ScanOutcome

    return ScanOutcome()


def test_dream_normalises_a_hostile_path_into_an_accepted_write(tmp_path, monkeypatch):
    from mori_advisor.dream import DreamPipeline

    async def t(h):
        dp = DreamPipeline(db_path=tmp_path / "memories.db", bifrost_client=None, store=h.store)
        events = [{"id": 8, "session_id": "s", "client": "c", "event_name": "Stop"}]
        monkeypatch.setattr(dp.session_log, "read_events", lambda **kw: events, raising=False)
        monkeypatch.setattr(dp, "_run_intake_promotion", lambda *a, **k: _noop())
        monkeypatch.setattr(dp, "_format_events", lambda ev: "events")
        monkeypatch.setattr(dp, "_call_dream_model", lambda text: "model output")
        monkeypatch.setattr(dp, "_contradiction_scan", lambda mems: _scan_none())
        body = "A real body with a reason given for its existence and enough substance."
        monkeypatch.setattr(
            dp,
            "_parse_response",
            lambda r: [{"path": "../../ünïcode path/x.md", "body": body, "reason": GOOD_DESC}],
        )
        await dp.run()
        names = [r["name"] for r in await h.q("SELECT name FROM memories")]
        assert len(names) == 1 and invalid_name_reason(names[0]) is None, names

    run("sqlite", tmp_path, t)


# ── D1: export ───────────────────────────────────────────────────────────────


def test_export_tools_have_no_path_parameters():
    """Release blocker: the server-side path parameters are gone from the tool signatures."""
    from mori_advisor import main as m

    assert list(inspect.signature(m.memory_export).parameters) == ["name"]
    assert list(inspect.signature(m.memory_export_all).parameters) == []
    assert list(inspect.signature(m.memory_import).parameters) == []


@pytest.mark.parametrize("backend", BACKENDS)
def test_export_returns_content_and_excludes_tombstones(backend, tmp_path):
    async def t(h):
        await h.write("live-one", "LIVE")
        await h.write("gone-one", "GONE")
        await _a(h.store.soft_delete("gone-one"))
        before = _files_under(tmp_path)
        assert "LIVE" in await _a(h.mem.export("live-one"))
        assert "not found" in (await _a(h.mem.export("gone-one"))).lower()
        assert _files_under(tmp_path) == before  # no file for a single export
        out = tmp_path / "exp"
        await _a(h.mem.export_all(str(out)))
        assert {p.name for p in out.glob("*.md")} - {"MEMORY.md"} == {"live-one.md"}

    run(backend, tmp_path, t)


@pytest.mark.parametrize("backend", BACKENDS)
def test_export_all_tool_is_dreamer_only_and_uses_the_fixed_directory(
    backend, tmp_path, monkeypatch, api_mode
):
    import mori_advisor.main as m

    async def t(h):
        _apply_store(monkeypatch, h.store)
        monkeypatch.setattr(m, "DATA_DIR", tmp_path / "data")
        await h.write("exp-a", "A")
        for key in (READ_KEY, WRITE_KEY):
            with _As(key):
                denied = await m.memory_export_all()
            assert "'dreamer' is required" in denied
        assert not (tmp_path / "data").exists()
        with _As(DREAM_KEY):
            ok = await m.memory_export_all()
        assert "Exported 1" in ok, ok
        assert (tmp_path / "data" / "exports" / "exp-a.md").is_file()

    run(backend, tmp_path, t)


# ── D8: import ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_import_attributes_the_caller_and_cannot_smuggle_canon(
    backend, tmp_path, monkeypatch, api_mode
):
    """The audit names the CALLER (not 'import'); with tier enforcement on, a canonical
    frontmatter tier is refused for an mcp caller instead of landing as canon."""
    import mori_advisor.main as m

    async def t(h):
        _apply_store(monkeypatch, h.store)
        monkeypatch.setattr(m, "DATA_DIR", tmp_path / "data")
        imports = tmp_path / "data" / "imports"
        imports.mkdir(parents=True)
        (imports / "imp-working.md").write_text(
            "---\nname: imp-working\ntitle: W\ntype: project\ntier: working\n"
            f"description: '{GOOD_DESC}'\ntags: []\n---\nworking body with enough words in it.\n"
        )
        (imports / "imp-canon.md").write_text(
            "---\nname: imp-canon\ntitle: C\ntype: project\ntier: canonical\n"
            f"description: '{GOOD_DESC}'\ntags: []\n---\ncanonical body with enough words in it.\n"
        )
        monkeypatch.setenv("MORI_TIER_ENFORCE", "enforce")
        with _As(DREAM_KEY):
            await m.memory_import()
        assert (await h.row("imp-working")) is not None
        assert (await h.row("imp-canon")) is None, "a canonical import must not land as canon"
        audit = await h.q(
            "SELECT actor_key_name, op FROM write_audit WHERE memory_name = $1", "imp-working"
        )
        assert audit == [{"actor_key_name": DREAM_KEY, "op": "import"}], audit

    run(backend, tmp_path, t)


# ── D3 / R3 / R4: protect ────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_protect_sets_explicitly_is_idempotent_and_audited(backend, tmp_path):
    async def t(h):
        await h.write("pm", "BODY")
        for _ in range(2):  # idempotent: a repeat call does NOT flip it back
            msg = await _a(h.mem.protect("pm", ["dom-a"], actor=DREAM_KEY))
            assert "now protected" in msg
            assert bool((await h.row("pm"))["protected"]) is True
        assert _tags_of((await h.row("pm"))["protected_domains"]) == ["dom-a"]
        await _a(h.mem.protect("pm", None, actor=DREAM_KEY))  # None keeps the domains
        assert _tags_of((await h.row("pm"))["protected_domains"]) == ["dom-a"]
        await _a(h.mem.protect("pm", [], actor=DREAM_KEY))  # [] clears them
        assert _tags_of((await h.row("pm"))["protected_domains"]) == []
        await _a(h.mem.protect("pm", ["dom-b"], actor=DREAM_KEY))
        msg = await _a(h.mem.protect("pm", protected=False, actor=DREAM_KEY))
        assert "now unprotected" in msg
        row = await h.row("pm")
        assert not row["protected"] and _tags_of(row["protected_domains"]) == []  # unprotect clears
        audit = await h.q(
            "SELECT op, actor_key_name FROM write_audit WHERE memory_name = $1 "
            "AND op IN ('protect', 'unprotect') ORDER BY id",
            "pm",
        )
        assert [a["op"] for a in audit] == ["protect"] * 5 + ["unprotect"]
        assert {a["actor_key_name"] for a in audit} == {DREAM_KEY}

    run(backend, tmp_path, t)


@pytest.mark.parametrize("backend", BACKENDS)
def test_protect_of_a_missing_or_tombstoned_name_is_not_found(backend, tmp_path):
    async def t(h):
        await h.write("tomb", "B")
        await _a(h.store.soft_delete("tomb"))
        for name in ("never-existed", "tomb"):
            msg = await _a(h.mem.protect(name, actor=DREAM_KEY))
            assert "not found" in msg.lower(), msg
        rows = await h.q("SELECT protected FROM memories WHERE name = $1", "tomb")
        assert [bool(r["protected"]) for r in rows] == [False]
        assert await h.q("SELECT id FROM write_audit WHERE op = 'protect'") == []

    run(backend, tmp_path, t)


@pytest.mark.parametrize("backend", BACKENDS)
def test_memory_protect_tool_is_dreamer_only_and_records_the_caller(
    backend, tmp_path, monkeypatch, api_mode
):
    import mori_advisor.main as m

    async def t(h):
        _apply_store(monkeypatch, h.store)
        await h.write("tp", "B")
        for key in (READ_KEY, WRITE_KEY):
            with _As(key):
                assert "'dreamer' is required" in await m.memory_protect("tp")
        assert not (await h.row("tp"))["protected"]
        with _As(DREAM_KEY):
            await m.memory_protect("tp")
            await m.memory_protect("tp", protected=False)
        assert not (await h.row("tp"))["protected"]
        actors = await h.q(
            "SELECT actor_key_name FROM write_audit WHERE memory_name = $1 AND op LIKE '%protect'",
            "tp",
        )
        assert {a["actor_key_name"] for a in actors} == {DREAM_KEY}

    run(backend, tmp_path, t)


@pytest.mark.parametrize("backend", BACKENDS)
def test_unprotect_rollback_reprotect_round_trip(backend, tmp_path):
    async def t(h):
        await h.write("rt", "ONE")
        await h.write("rt", "TWO")
        vid = await _version_id(h, "rt", "ONE")
        await _a(h.mem.protect("rt", actor=DREAM_KEY))
        refused = await _a(h.store.rollback("rt", vid))
        assert "memory_protect('rt', protected=false)" in refused
        await _a(h.mem.protect("rt", protected=False, actor=DREAM_KEY))
        ok = await _a(h.store.rollback("rt", vid))
        assert ok.startswith("Memory 'rt' rolled back"), ok
        await _a(h.mem.protect("rt", actor=DREAM_KEY))
        row = await h.row("rt")
        assert (row["body"], bool(row["protected"])) == ("ONE", True)

    run(backend, tmp_path, t)


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("where", ["active", "version"])
def test_rollback_is_rejected_by_a_tag_prefix_too(backend, where, tmp_path):
    """R3 full predicate: a protected tag prefix on the active row OR on the version restored."""

    async def t(h):
        infra = ["infrastructure"]
        await h.write("tg", "ONE", tags=infra if where == "version" else [])
        await h.write("tg", "TWO", tags=infra if where == "active" else [])
        vid = await _version_id(h, "tg", "ONE")
        await _set_config(h, "protected_tag_prefixes", ["infra"])  # after the writes
        msg = await _a(h.store.rollback("tg", vid))
        assert "tag prefix 'infra'" in msg and "NOT rolled back" in msg, msg
        assert (await h.row("tg"))["body"] == "TWO"
        assert await h.q("SELECT id FROM pending_writes") == []

    run(backend, tmp_path, t)


def _tags_of(v):
    return json.loads(v) if isinstance(v, str) else list(v or [])


# ── R5: the tag-prefix lane at parity ────────────────────────────────────────


def _mcp(key):
    return Provenance(actor="mcp", actor_detail=key, source="test", op="write")


@pytest.mark.parametrize("backend", BACKENDS)
def test_tag_prefix_lane_queues_a_write_key_and_admits_a_dreamer_key(backend, tmp_path, api_mode):
    async def t(h):
        await _set_config(h, "protected_tag_prefixes", ["infra"])
        # A write key NAMED like a trusted client gains nothing in api mode (consult fold).
        await _set_config(h, "trusted_clients", [WRITE_KEY])
        kw = dict(title="t", description=GOOD_DESC, body="b", tags=["infrastructure"])
        queued = await _a(
            h.mem._write(name="pl", provenance=_mcp(WRITE_KEY), client=WRITE_KEY, **kw)
        )
        assert queued.disposition is Disposition.DOWNGRADED_TO_PENDING, queued.reason
        assert await h.row("pl") is None
        pend = await h.q("SELECT memory_name, status FROM pending_writes")
        assert pend == [{"memory_name": "pl", "status": "pending"}]
        admitted = await _a(h.mem._write(name="pl", provenance=_mcp(DREAM_KEY), **kw))
        assert admitted.disposition is Disposition.ACCEPTED, admitted.reason
        # The internal writers that pass _skip_protection (the dreamer) are untouched.
        dreamer = Provenance(actor="dreamer", source="test", op="write")
        r = await _a(h.mem._write(name="pl2", provenance=dreamer, _skip_protection=True, **kw))
        assert r.disposition is Disposition.ACCEPTED

    run(backend, tmp_path, t)


@pytest.mark.parametrize("backend", BACKENDS)
def test_tag_prefix_lane_host_mode_keeps_trusted_clients(backend, tmp_path, monkeypatch):
    monkeypatch.setattr(pol, "_TD_MODE", "host")

    async def t(h):
        await _set_config(h, "protected_tag_prefixes", ["infra"])
        await _set_config(h, "trusted_clients", ["trusted-host"])
        kw = dict(title="t", description=GOOD_DESC, body="b", tags=["infrastructure"])
        q = await _a(h.mem._write(name="hm", provenance=_mcp("x"), client="other-host", **kw))
        assert q.disposition is Disposition.DOWNGRADED_TO_PENDING
        ok = await _a(h.mem._write(name="hm", provenance=_mcp("x"), client="trusted-host", **kw))
        assert ok.disposition is Disposition.ACCEPTED

    run(backend, tmp_path, t)


# ── #71 / #76: approve and the pending sink ──────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_approve_marks_approved_only_when_canon_takes_the_write(backend, tmp_path, monkeypatch):
    """#71: under anatomy enforce an incomplete pending write is NOT approved and stays pending
    (one row, unchanged) — on both backends."""
    monkeypatch.setenv("MORI_ANATOMY_ENFORCE", "enforce")

    async def t(h):
        await h.q(
            "INSERT INTO pending_writes (memory_name, title, description, type, body, tags, "
            "origin_session_ids, origin_clients, proposed_by, status) "
            "VALUES ('ap', 't', '', 'project', 'short', '[]', '[]', '[]', 'x', 'pending')"
        )
        wid = (await h.q("SELECT id FROM pending_writes"))[0]["id"]
        msg = await _a(h.mem.approve(wid, reviewer=DREAM_KEY))
        assert "NOT approved" in msg, msg
        rows = await h.q("SELECT id, status, body FROM pending_writes")
        assert rows == [{"id": wid, "status": "pending", "body": "short"}], rows
        assert await h.row("ap") is None

    run(backend, tmp_path, t)


@pytest.mark.parametrize("backend", BACKENDS)
def test_approve_of_a_protected_memory_applies_instead_of_looping(backend, tmp_path):
    """#71 with _skip_protection: approving a change to a protected memory applies it."""

    async def t(h):
        await h.write("prot", "OLD")
        await _a(h.mem.protect("prot", actor=DREAM_KEY))
        body = "NEW body with a reason given for its existence and enough substance."
        await h.q(
            "INSERT INTO pending_writes (memory_name, title, description, type, body, tags, "
            "origin_session_ids, origin_clients, proposed_by, status) "
            "VALUES ('prot', 't', $1, 'project', $2, '[]', '[]', '[]', 'x', 'pending')",
            GOOD_DESC,
            body,
        )
        wid = (await h.q("SELECT id FROM pending_writes"))[0]["id"]
        msg = await _a(h.mem.approve(wid, reviewer=DREAM_KEY))
        assert "approved" in msg and "NOT" not in msg, msg
        assert (await h.row("prot"))["body"] == body
        assert [r["status"] for r in await h.q("SELECT status FROM pending_writes")] == ["approved"]

    run(backend, tmp_path, t)


@pytest.mark.parametrize("backend", BACKENDS)
def test_second_downgrade_for_a_name_upserts_the_open_pending_row(backend, tmp_path, monkeypatch):
    """#76: SQLite raised IntegrityError on the second open pending row; Postgres upserted."""
    monkeypatch.setenv("MORI_ANATOMY_ENFORCE", "enforce")

    async def t(h):
        prov = Provenance(actor="mcp", actor_detail=WRITE_KEY, source="test")
        for body in ("first", "second"):
            r = await _a(
                h.mem._write(name="dup", title="t", description="", body=body, provenance=prov)
            )
            assert r.disposition is Disposition.DOWNGRADED_TO_PENDING
        rows = await h.q("SELECT body, status FROM pending_writes WHERE memory_name = 'dup'")
        assert rows == [{"body": "second", "status": "pending"}], rows

    run(backend, tmp_path, t)


# ── Route audit + MCP tool roles (api mode, real HTTP where it is HTTP) ──────


@pytest.fixture(autouse=True)
def _isolate_real_app_lifespan(monkeypatch, tmp_path):
    """The app's lifespan shuts down main's LLM executor and closes main's store on exit. Give it a
    throwaway store, and let monkeypatch put the module globals back, so no later test in the
    process inherits a shut-down executor or a closed store."""
    import mori_advisor.main as m
    from mori_advisor.store import get_store

    monkeypatch.setattr(m, "_llm_executor", m._llm_executor)
    store = get_store(tmp_path / "app-memories.db")
    store.bootstrap()
    monkeypatch.setattr(m, "store", store)
    monkeypatch.setattr(m, "memory_store", store._mem if hasattr(store, "_mem") else store)
    yield


def _real_app():
    import mori_advisor.main as m

    return m.mcp.http_app(transport="streamable-http", middleware=[Middleware(mw.ApiKeyMiddleware)])


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/events"),
        ("POST", "/api/events/raw"),
        ("POST", "/api/precompact"),
        ("GET", "/api/dream/run"),
        ("POST", "/api/dream/run"),
        ("POST", "/api/git/ingest"),
    ],
)
def test_state_changing_rest_routes_refuse_a_read_key(api_mode, method, path):
    with TestClient(_real_app()) as c:
        r = c.request(method, path, headers={"x-api-key": SECRETS[READ_KEY]}, json={})
    assert r.status_code == 403, (path, r.status_code, r.text[:200])
    assert "'write' is required" in r.json()["detail"]


def test_a_write_key_still_reaches_the_event_route(api_mode):
    """Control for the route test: the role gate admits a write key (body errors are fine)."""
    with TestClient(_real_app()) as c:
        r = c.post("/api/events", headers={"x-api-key": SECRETS[WRITE_KEY]}, json={})
    assert r.status_code != 403, r.text[:200]


@pytest.mark.parametrize(
    "tool,kwargs,role",
    [
        ("key_generate", {"name": "x"}, "dreamer"),
        ("standards_reload", {}, "dreamer"),
        ("mori_ingest", {"source": ["/nonexistent"]}, "write"),
        ("dream_run", {}, "write"),
        ("nats_pub", {"message": "x"}, "write"),
        ("msg_send", {"to": "x", "type": "broadcast", "body": "x"}, "write"),
        ("consult_advisor", {"question": "x"}, "write"),
    ],
)
def test_state_changing_mcp_tools_refuse_a_lower_role(api_mode, tool, kwargs, role):
    import mori_advisor.main as m

    lower = READ_KEY if role == "write" else WRITE_KEY
    with _As(lower):
        out = asyncio.run(getattr(m, tool)(**kwargs))
    assert f"'{role}' is required" in str(out), out


# ── D2: keys only in the header ──────────────────────────────────────────────


def _stub_app(monkeypatch, hits: list):
    async def handler(request):
        hits.append(request.url.path)
        return JSONResponse({"ok": True})

    async def health(request):
        hits.append("/health")
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route("/x", handler, methods=["GET", "POST"]),
            Route("/health", health, methods=["GET"]),
            Route("/mcp", handler, methods=["GET", "POST", "DELETE"]),
        ]
    )
    app.add_middleware(mw.ApiKeyMiddleware)
    return TestClient(app)


@pytest.mark.parametrize("param", ["api-key", "api_key"])
@pytest.mark.parametrize("path", ["/x", "/health"])
@pytest.mark.parametrize("with_header", [False, True])
def test_url_key_is_refused_before_any_handler(api_mode, monkeypatch, param, path, with_header):
    hits: list = []
    c = _stub_app(monkeypatch, hits)
    before = mx.prom_registry.get_sample_value("mori_url_key_rejections_total") or 0
    headers = {"x-api-key": SECRETS[WRITE_KEY]} if with_header else {}
    r = c.get(f"{path}?{param}={SECRETS[WRITE_KEY]}", headers=headers)
    assert r.status_code == 400 and "X-Api-Key header" in r.json()["detail"]
    assert hits == []
    assert mx.prom_registry.get_sample_value("mori_url_key_rejections_total") == before + 1


def test_header_key_still_works(api_mode, monkeypatch):
    hits: list = []
    c = _stub_app(monkeypatch, hits)
    assert c.get("/x", headers={"x-api-key": SECRETS[WRITE_KEY]}).status_code == 200
    assert c.get("/x").status_code == 401
    assert hits == ["/x"]


def test_url_key_value_is_never_logged_by_mori(api_mode, monkeypatch, caplog):
    c = _stub_app(monkeypatch, [])
    with caplog.at_level(logging.DEBUG, logger="mori_advisor"):
        c.get(f"/x?api-key={SECRETS[WRITE_KEY]}")
    mori = [r.getMessage() for r in caplog.records if r.name.startswith("mori_advisor")]
    assert mori and not any(SECRETS[WRITE_KEY] in m for m in mori), mori


@pytest.mark.parametrize("param", ["api-key", "api_key", "API_KEY"])
def test_access_log_redacts_a_url_key(param):
    """uvicorn writes its access line whatever we answer; the filter keeps the key out of it."""
    secret = SECRETS[WRITE_KEY]
    rec = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "x",
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", f"/api/memories?x=1&{param}={secret}&y=2", "1.1", 400),
        None,
    )
    mw.RedactUrlKeys().filter(rec)
    line = rec.getMessage()
    assert secret not in line and "[REDACTED]" in line and "y=2" in line, line


def test_access_log_redaction_is_installed_once():
    mw.install_access_log_redaction()
    mw.install_access_log_redaction()
    access = logging.getLogger("uvicorn.access")
    assert sum(isinstance(f, mw.RedactUrlKeys) for f in access.filters) == 1


# ── D4: sessions (the real app) ──────────────────────────────────────────────

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    },
}


def _open(c, key) -> str:
    r = c.post("/mcp", headers={**MCP_HEADERS, "x-api-key": SECRETS[key]}, json=_INIT)
    assert r.status_code == 200, r.text[:200]
    sid = r.headers["mcp-session-id"]
    r = c.post(
        "/mcp",
        headers={**MCP_HEADERS, "x-api-key": SECRETS[key], "mcp-session-id": sid},
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert r.status_code == 202
    return sid


def _call(c, sid, tool, args, key=None, rid=9):
    headers = {**MCP_HEADERS, "mcp-session-id": sid}
    if key:
        headers["x-api-key"] = SECRETS[key]
    return c.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": rid,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        },
    )


def _rej(reason):
    return (
        mx.prom_registry.get_sample_value("mori_session_rejections_total", {"reason": reason}) or 0
    )


def test_probe_attack_replayed_is_impossible(api_mode, tmp_path):
    """The measured attack: open with a READ key, then send NO key — and try to write a file."""
    target = tmp_path / "WRITTEN-BY-NO-KEY.md"
    with TestClient(_real_app()) as c:
        sid = _open(c, READ_KEY)
        no_key = _call(c, sid, "memory_export", {"name": "x", "output_path": str(target)})
        assert no_key.status_code == 401
        # Even WITH the read key, the path argument is refused, not silently dropped.
        with_key = _call(
            c, sid, "memory_export", {"name": "x", "output_path": str(target)}, READ_KEY
        )
        assert with_key.status_code == 200 and '"isError":true' in with_key.text
        assert "Unexpected keyword argument" in with_key.text
        denied = _call(c, sid, "memory_export_all", {}, READ_KEY)
        assert "'dreamer' is required" in denied.text
    assert not target.exists()


def test_session_requires_its_own_key_and_acts_as_it(api_mode):
    with TestClient(_real_app()) as c:
        sid = _open(c, READ_KEY)
        n0, w0 = _rej("no_key"), _rej("wrong_key")
        assert _call(c, sid, "dream_status", {}).status_code == 401
        assert _rej("no_key") == n0 + 1
        other = _call(c, sid, "dream_status", {}, DREAM_KEY)  # another VALID key
        assert other.status_code == 401 and "different key" in other.text
        assert _rej("wrong_key") == w0 + 1
        ok = _call(c, sid, "key_generate", {"name": "z"}, READ_KEY)
        assert ok.status_code == 200 and "'dreamer' is required" in ok.text  # acts as READ
        dsid = _open(c, DREAM_KEY)
        dok = _call(c, dsid, "key_generate", {"name": "z"}, DREAM_KEY)
        assert "Add to server MORI_API_KEYS" in dok.text  # acts as DREAMER


def test_tools_see_the_actor_of_the_current_request_not_the_opener(api_mode, monkeypatch):
    """R2: the actor is per request. Tools run in the session's task (context copied at
    initialize), so without the per-message hook a role change would never reach a live session."""
    with TestClient(_real_app()) as c:
        sid = _open(c, DREAM_KEY)
        assert (
            "Add to server MORI_API_KEYS"
            in _call(c, sid, "key_generate", {"name": "z"}, DREAM_KEY).text
        )
        monkeypatch.setitem(pol._ROLES, DREAM_KEY, "read")  # the same key, now read-only
        later = _call(c, sid, "key_generate", {"name": "z"}, DREAM_KEY, rid=10)
        assert "'dreamer' is required" in later.text, later.text[:300]


@pytest.mark.parametrize("which", ["idle", "absolute"])
def test_expired_session_is_refused_and_forgotten(api_mode, monkeypatch, which):
    with TestClient(_real_app()) as c:
        sid = _open(c, WRITE_KEY)
        sess = mw._SESSIONS[sid]
        if which == "idle":
            sess.last_seen -= mw.SESSION_IDLE_TTL_S + 1
        else:  # recently used, but older than the absolute lifetime
            sess.created_at -= mw.SESSION_MAX_AGE_S + 1
        e0 = _rej("expired")
        r = _call(c, sid, "dream_status", {}, WRITE_KEY)
        assert r.status_code == 404 and "re-initialise" in r.text
        assert sid not in mw._SESSIONS and _rej("expired") == e0 + 1
        # Recovery: a fresh initialize works.
        assert _open(c, WRITE_KEY) != sid


@pytest.mark.parametrize("state", ["live", "expired"])
def test_a_key_that_does_not_own_the_session_always_gets_401(api_mode, state):
    """Board B1 ordering: 401 for a non-owner whether the session is live or expired, so a holder
    of a stolen id cannot tell the two apart; the owner's expired session is left for the owner."""
    with TestClient(_real_app()) as c:
        sid = _open(c, WRITE_KEY)
        if state == "expired":
            mw._SESSIONS[sid].last_seen -= mw.SESSION_IDLE_TTL_S + 1
        r = _call(c, sid, "dream_status", {}, DREAM_KEY)
        assert r.status_code == 401 and "different key" in r.text
        assert sid in mw._SESSIONS  # not consumed by the non-owner
        if state == "expired":
            assert _call(c, sid, "dream_status", {}, WRITE_KEY).status_code == 404


@pytest.mark.parametrize("sid_kind", ["live", "unknown"])
@pytest.mark.parametrize("key", [None, "bad"])
def test_no_or_bad_key_is_401_before_any_session_lookup(api_mode, sid_kind, key):
    with TestClient(_real_app()) as c:
        sid = _open(c, WRITE_KEY) if sid_kind == "live" else "never-issued"
        h = {**MCP_HEADERS, "mcp-session-id": sid}
        if key:
            h["x-api-key"] = "z" * 64
        r = c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    assert r.status_code == 401


def test_unknown_session_id_is_404(api_mode):
    with TestClient(_real_app()) as c:
        r = _call(c, "not-a-session", "dream_status", {}, WRITE_KEY)
    assert r.status_code == 404


def test_any_request_on_a_session_moves_last_seen_including_get(api_mode, monkeypatch):
    c = _stub_app(monkeypatch, [])
    mw._register_session("sid-get", WRITE_KEY, 1000.0)
    monkeypatch.setattr(mw.time, "time", lambda: 5000.0)
    r = c.get("/mcp", headers={"x-api-key": SECRETS[WRITE_KEY], "mcp-session-id": "sid-get"})
    assert r.status_code == 200
    assert mw._SESSIONS["sid-get"].last_seen == 5000.0


def test_registry_is_capped_by_oldest_last_seen(api_mode, monkeypatch):
    monkeypatch.setattr(mw, "SESSION_CAP", 2)
    mw._register_session("s1", WRITE_KEY, 1.0)
    mw._register_session("s2", WRITE_KEY, 2.0)
    mw._SESSIONS.move_to_end("s1")  # s1 used more recently than s2
    mw._register_session("s3", WRITE_KEY, 3.0)
    assert list(mw._SESSIONS) == ["s1", "s3"]
    assert mx.prom_registry.get_sample_value("mori_mcp_sessions") == 2


def test_delete_removes_the_session(api_mode):
    with TestClient(_real_app()) as c:
        sid = _open(c, WRITE_KEY)
        c.delete(
            "/mcp", headers={**MCP_HEADERS, "x-api-key": SECRETS[WRITE_KEY], "mcp-session-id": sid}
        )
        assert sid not in mw._SESSIONS


def test_rate_limiter_counts_session_requests(api_mode, monkeypatch):
    monkeypatch.setattr(mw, "_rate_cfg", RateLimitConfig(3, 60, "writes"))
    monkeypatch.setattr(mw, "rate_limit_store", InMemoryRateLimitStore())
    with TestClient(_real_app()) as c:
        sid = _open(c, WRITE_KEY)  # 2 POSTs
        assert _call(c, sid, "dream_status", {}, WRITE_KEY).status_code == 200  # 3rd
        assert _call(c, sid, "dream_status", {}, WRITE_KEY).status_code == 429


def test_open_mode_binds_sessions_to_anonymous(monkeypatch):
    monkeypatch.setattr(pol, "_TD_MODE", "host")
    monkeypatch.setattr(auth, "_KEYS", {})
    monkeypatch.setattr(mw, "_SESSIONS", OrderedDict())
    with TestClient(_real_app()) as c:
        r = c.post("/mcp", headers=MCP_HEADERS, json=_INIT)
        sid = r.headers["mcp-session-id"]
        assert mw._SESSIONS[sid].key_name == "anonymous"
        assert _call(c, sid, "dream_status", {}).status_code == 200


def test_session_ids_never_reach_the_logs(api_mode, caplog):
    with caplog.at_level(logging.DEBUG, logger="mori_advisor.middleware"):
        with TestClient(_real_app()) as c:
            sid = _open(c, WRITE_KEY)
            _call(c, sid, "dream_status", {})  # a rejection, which logs
    assert sid not in caplog.text and mw._sid_tag(sid) in caplog.text


def test_metrics_carry_no_key_names(api_mode):
    """Release gate: /metrics is an open path — exercise sessions, rejections and writes first."""
    with TestClient(_real_app()) as c:
        sid = _open(c, WRITE_KEY)
        _call(c, sid, "dream_status", {})
        _call(c, sid, "dream_status", {}, DREAM_KEY)
        _call(c, sid, "memory_write", {"name": "m-x", "title": "t", "body": "b"}, WRITE_KEY)
        body = c.get("/metrics").text
    text = generate_latest(mx.prom_registry).decode() + body
    for name in SECRETS:
        assert name not in text, name


# ── D5: consult retries (counted by attempts, not by reading an attribute) ───


def _attempts(monkeypatch, vk, mode="bifrost", env_retries=None):
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return httpx.Response(500, json={"error": {"message": "upstream hiccup"}})

    monkeypatch.setenv("MORI_PROVIDER_MODE", mode)
    if mode == "direct":
        monkeypatch.setenv("MORI_API_KEY", "test-direct-key")
    if env_retries is None:
        monkeypatch.delenv("MORI_ADVISOR_MAX_RETRIES", raising=False)
    else:
        monkeypatch.setenv("MORI_ADVISOR_MAX_RETRIES", env_retries)
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        bc, "DefaultHttpxClient", lambda **kw: httpx.Client(transport=transport, **kw)
    )
    monkeypatch.setattr(
        "openai._base_client.BaseClient._calculate_retry_timeout", lambda *a, **k: 0.0
    )
    client = bc.BifrostClient(base_url="http://bifrost.test", timeout=5)
    with pytest.raises(Exception):
        client.consult(system="s", user="u", vk=vk)
    return len(seen)


@pytest.mark.parametrize("mode", ["bifrost", "direct"])
def test_advisor_makes_exactly_one_attempt_in_both_modes(monkeypatch, mode):
    assert _attempts(monkeypatch, "advisor", mode) == 1


@pytest.mark.parametrize("vk", ["fast", "dream"])
def test_fast_and_dream_keep_the_sdk_retries(monkeypatch, vk):
    assert _attempts(monkeypatch, vk) == 3  # SDK default max_retries=2


def test_advisor_retries_can_be_restored_by_env(monkeypatch):
    assert _attempts(monkeypatch, "advisor", env_retries="2") == 3


@pytest.mark.parametrize("bad", ["-1", "two", "1.5"])
def test_advisor_retry_env_must_be_an_exact_integer(monkeypatch, bad):
    monkeypatch.setenv("MORI_ADVISOR_MAX_RETRIES", bad)
    with pytest.raises(ValueError):
        bc.BifrostClient(base_url="http://bifrost.test", timeout=5)


def test_session_ttl_env_is_fail_loud(monkeypatch):
    monkeypatch.setenv("MORI_SESSION_IDLE_TTL_S", "12h")
    with pytest.raises(ValueError):
        mw._positive_int_env("MORI_SESSION_IDLE_TTL_S", 1)
    monkeypatch.setenv("MORI_SESSION_IDLE_TTL_S", "0")
    with pytest.raises(ValueError):
        mw._positive_int_env("MORI_SESSION_IDLE_TTL_S", 1)
    assert os.environ.get("MORI_SESSION_IDLE_TTL_S") == "0"
