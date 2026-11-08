"""v2.3.16 canon lock: only a reviewed decision changes canon.

Board ruling 7 Nov 2026 (dotfiles docs/mori-planning/canon-lock-records/BOARD-RULING-2026-11-07.md).
A change that would create or alter a canonical row applies only with canon authority — the private
``CanonAuthority`` token (``approve()`` and the standards import) or an mcp/rest request whose
AUTHENTICATED key has the ``dreamer`` role in api mode. Anything else is stored as a proposal and the
row is unchanged. Every test runs on both backends.
"""

from __future__ import annotations

import contextlib
import inspect

import pytest

from tests.test_mcp_tools import BACKENDS, _run_with_backend

WRITE_KEY = "dev-console"
DREAM_KEY = "fred-review"
OTHER_KEY = "other-console"
READ_KEY = "viewer"


async def _a(x):
    return await x if inspect.isawaitable(x) else x


def _ms(store):
    return store._mem if hasattr(store, "_mem") else store


def _is_pg(store) -> bool:
    return hasattr(store, "pool") and store.pool is not None


async def _rows(store, sql_sqlite: str, sql_pg: str, *args) -> list[dict]:
    if _is_pg(store):
        async with store.pool.acquire() as conn:
            return [dict(r) for r in await conn.fetch(sql_pg, *args)]
    conn = store._mem._get_conn()
    try:
        cur = conn.execute(sql_sqlite, args)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


async def _active(store, name: str) -> dict | None:
    rows = await _rows(
        store,
        "SELECT name, tier, body, title, deleted_at, protected FROM memories "
        "WHERE name = ? AND deleted_at IS NULL",
        "SELECT name, tier, body, title, deleted_at, protected FROM memories "
        "WHERE name = $1 AND deleted_at IS NULL",
        name,
    )
    return rows[0] if rows else None


async def _pending(store, name: str | None = None) -> list[dict]:
    if name is None:
        return await _rows(
            store,
            "SELECT * FROM pending_writes ORDER BY id",
            "SELECT * FROM pending_writes ORDER BY id",
        )
    return await _rows(
        store,
        "SELECT * FROM pending_writes WHERE memory_name = ? ORDER BY id",
        "SELECT * FROM pending_writes WHERE memory_name = $1 ORDER BY id",
        name,
    )


async def _audit(store, op: str) -> list[dict]:
    return await _rows(
        store,
        "SELECT actor_key_name, op, memory_name, detail FROM write_audit WHERE op = ? ORDER BY id",
        "SELECT actor_key_name, op, memory_name, detail FROM write_audit WHERE op = $1 ORDER BY id",
        op,
    )


async def _seed_canon(store, name: str, body: str = "canon body", protected: bool = False):
    """Seed a canonical row WITH the token — the only way tests may create canon directly."""
    from mori_advisor.canon_authority import CanonAuthority
    from mori_advisor.provenance import SYSTEM

    r = await _a(
        _ms(store)._write(
            name=name,
            title=f"{name} title",
            description="seeded canon for a test",
            type="decision",
            tier="canonical",
            body=body,
            tags=["test"],
            provenance=SYSTEM,
            _canon_authority=CanonAuthority("test-seed"),
        )
    )
    assert r.accepted, r
    if protected:
        await _a(_ms(store).protect(name, protected=True, actor="test"))
    return r


async def _seed_working(store, name: str, body: str = "working body"):
    from mori_advisor.provenance import SYSTEM

    r = await _a(
        _ms(store)._write(
            name=name,
            title=f"{name} title",
            description="seeded working row for a test",
            tier="working",
            body=body,
            tags=["test"],
            provenance=SYSTEM,
        )
    )
    assert r.accepted, r
    return r


@pytest.fixture
def api_mode(monkeypatch):
    import mori_advisor.policy as pol

    monkeypatch.setattr(pol, "_TD_MODE", "api")
    monkeypatch.setattr(
        pol,
        "_ROLES",
        {WRITE_KEY: "write", OTHER_KEY: "write", DREAM_KEY: "dreamer", READ_KEY: "read"},
    )
    return pol


@contextlib.contextmanager
def as_key(key: str | None):
    """The authenticated actor, as the request middleware would set it."""
    from mori_advisor.policy import Actor, current_actor, role_for

    token = current_actor.set(Actor(key, role_for(key)) if key else None)
    try:
        yield
    finally:
        current_actor.reset(token)


def _req(key: str, op: str = "write"):
    from mori_advisor.provenance import request_provenance

    return request_provenance("mcp", key, "tests:canon_lock", op=op)


# ── R1: the chokepoint ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_write_key_canonical_create_is_queued(backend, tmp_path, monkeypatch, api_mode):
    from mori_advisor.main import memory_write

    async def run(store):
        with as_key(WRITE_KEY):
            out = await memory_write(
                name="new-canon", title="t", body="proposed", tier="canonical", tags="x"
            )
        assert await _active(store, "new-canon") is None, "the store must be unchanged"
        pend = await _pending(store, "new-canon")
        assert len(pend) == 1 and pend[0]["status"] == "pending"
        assert pend[0]["operation"] == "write" and pend[0]["tier"] == "canonical"
        assert pend[0]["body"] == "proposed"
        assert pend[0]["proposed_by"] == WRITE_KEY
        assert f"pending id {pend[0]['id']}" in out, out

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_write_key_rewrite_of_canonical_body_is_queued(backend, tmp_path, monkeypatch, api_mode):
    """The wider gap: a tier='working' write to a canonical name used to replace its body."""

    async def run(store):
        await _seed_canon(store, "canon-a", body="original")
        with as_key(WRITE_KEY):
            r = await _a(
                _ms(store)._write(
                    name="canon-a",
                    title="t",
                    body="rewritten",
                    tier="working",
                    provenance=_req(WRITE_KEY),
                )
            )
        assert not r.accepted and r.pending_id
        row = await _active(store, "canon-a")
        assert (row["body"], row["tier"]) == ("original", "canonical")
        pend = await _pending(store, "canon-a")
        assert [p["body"] for p in pend] == ["rewritten"]

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_dreamer_key_canonical_write_applies(backend, tmp_path, monkeypatch, api_mode):
    """Control: a dreamer-role key's own write applies as today."""

    async def run(store):
        await _seed_canon(store, "canon-b", body="original")
        with as_key(DREAM_KEY):
            r = await _a(
                _ms(store)._write(
                    name="canon-b",
                    title="t",
                    body="by the dreamer",
                    tier="canonical",
                    provenance=_req(DREAM_KEY),
                )
            )
        assert r.accepted, r
        assert (await _active(store, "canon-b"))["body"] == "by the dreamer"
        assert await _pending(store, "canon-b") == []

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_dreamer_name_in_provenance_without_authenticated_actor_confers_nothing(
    backend, tmp_path, monkeypatch, api_mode
):
    """Provenance strings confer nothing: the identity comes from the authenticated actor."""

    async def run(store):
        await _seed_canon(store, "canon-c", body="original")
        with as_key(None):
            r = await _a(
                _ms(store)._write(
                    name="canon-c",
                    title="t",
                    body="forged",
                    tier="canonical",
                    provenance=_req(DREAM_KEY),
                )
            )
        assert not r.accepted
        assert (await _active(store, "canon-c"))["body"] == "original"

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("protected", [False, True])
def test_dream_write_to_canonical_name_is_queued(backend, protected, tmp_path, monkeypatch):
    """The hole this release closes: dream asks for working, skips protection, and used to
    replace a canonical body. Its _skip_protection still skips the flag, never this rule."""
    from mori_advisor.provenance import DREAMER

    async def run(store):
        await _seed_canon(store, "canon-d", body="original", protected=protected)
        r = await _a(
            _ms(store)._write(
                name="canon-d",
                title="t",
                body="dream rewrite",
                tier="working",
                tags=["dream-phase"],
                provenance=DREAMER,
                _skip_protection=True,
            )
        )
        assert not r.accepted
        row = await _active(store, "canon-d")
        assert (row["body"], row["tier"]) == ("original", "canonical")
        pend = await _pending(store, "canon-d")
        assert len(pend) == 1 and pend[0]["body"] == "dream rewrite"
        assert pend[0]["proposed_by"] == "dream-pipeline"  # not the review role's name

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_dream_never_creates_canon(backend, tmp_path, monkeypatch):
    from mori_advisor.provenance import DREAMER

    async def run(store):
        r = await _a(
            _ms(store)._write(
                name="dream-new",
                title="t",
                body="b",
                tier="canonical",
                provenance=DREAMER,
                _skip_protection=True,
            )
        )
        assert not r.accepted
        assert await _active(store, "dream-new") is None

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("actor", ["ingestion", "system", "legacy", "msg", "import"])
def test_internal_actors_without_token_are_queued(backend, actor, tmp_path, monkeypatch):
    from mori_advisor.provenance import Provenance

    async def run(store):
        await _seed_canon(store, "canon-e", body="original")
        r = await _a(
            _ms(store)._write(
                name="canon-e",
                title="t",
                body="other",
                tier="working",
                provenance=Provenance(actor=actor, source="tests"),
            )
        )
        assert not r.accepted
        assert (await _active(store, "canon-e"))["body"] == "original"

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_working_writes_are_unchanged(backend, tmp_path, monkeypatch, api_mode):
    async def run(store):
        await _seed_working(store, "work-a", body="v1")
        with as_key(WRITE_KEY):
            r = await _a(
                _ms(store)._write(
                    name="work-a", title="t", body="v2", tier="working", provenance=_req(WRITE_KEY)
                )
            )
        assert r.accepted
        assert (await _active(store, "work-a"))["body"] == "v2"
        assert await _pending(store, "work-a") == []

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_host_mode_queues_canonical_changes(backend, tmp_path, monkeypatch):
    """D1: host mode has no roles, so no request actor holds authority — the change is queued."""
    import mori_advisor.policy as pol

    monkeypatch.setattr(pol, "_TD_MODE", "host")

    async def run(store):
        await _seed_canon(store, "canon-f", body="original")
        with as_key(DREAM_KEY):
            r = await _a(
                _ms(store)._write(
                    name="canon-f",
                    title="t",
                    body="x",
                    tier="canonical",
                    provenance=_req(DREAM_KEY),
                )
            )
        assert not r.accepted
        assert (await _active(store, "canon-f"))["body"] == "original"

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_standard_tier_string_is_queued_as_canonical(backend, tmp_path, monkeypatch, api_mode):
    """There is no 'standard' tier: it is normalised to canonical, never to working."""

    async def run(store):
        with as_key(WRITE_KEY):
            r = await _a(
                _ms(store)._write(
                    name="std-a", title="t", body="b", tier="standard", provenance=_req(WRITE_KEY)
                )
            )
        assert not r.accepted
        assert await _active(store, "std-a") is None
        assert [p["tier"] for p in await _pending(store, "std-a")] == ["canonical"]

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_queue_pending_write_normalises_standard_tier(backend, tmp_path, monkeypatch):
    async def run(store):
        await _a(
            _ms(store).queue_pending_write(
                name="std-b", body="b", proposed_by="ingestion", source="ingestion", tier="standard"
            )
        )
        assert [p["tier"] for p in await _pending(store, "std-b")] == ["canonical"]

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── R2: delete, rollback, hard delete, restore ────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_write_key_delete_of_canon_is_queued(backend, tmp_path, monkeypatch, api_mode):
    from mori_advisor.main import memory_delete

    async def run(store):
        await _seed_canon(store, "canon-g")
        with as_key(WRITE_KEY):
            out = await memory_delete("canon-g")
        assert await _active(store, "canon-g") is not None, "the row must still be present"
        pend = await _pending(store, "canon-g")
        assert [(p["operation"], p["status"]) for p in pend] == [("delete", "pending")]
        assert "queued" in out.lower(), out

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_write_key_rollback_of_canon_is_queued(backend, tmp_path, monkeypatch, api_mode):
    from mori_advisor.main import memory_rollback

    async def run(store):
        await _seed_canon(store, "canon-h", body="v1")
        from mori_advisor.canon_authority import CanonAuthority
        from mori_advisor.provenance import SYSTEM

        await _a(
            _ms(store)._write(
                name="canon-h",
                title="canon-h title",
                description="seeded canon for a test",
                type="decision",
                tier="canonical",
                body="v2",
                tags=["test"],
                provenance=SYSTEM,
                _canon_authority=CanonAuthority("test-seed"),
            )
        )
        versions = await _rows(
            store,
            "SELECT version_id FROM memory_versions WHERE memory_name = ? ORDER BY version_id",
            "SELECT version_id FROM memory_versions WHERE memory_name = $1 ORDER BY version_id",
            "canon-h",
        )
        vid = versions[-1]["version_id"]
        with as_key(WRITE_KEY):
            out = await memory_rollback("canon-h", vid)
        assert (await _active(store, "canon-h"))["body"] == "v2", "the version must be unchanged"
        pend = await _pending(store, "canon-h")
        assert [(p["operation"], p["target_version_id"]) for p in pend] == [("rollback", vid)]
        assert "queued" in out.lower(), out

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_working_delete_and_rollback_by_write_key_apply(backend, tmp_path, monkeypatch, api_mode):
    from mori_advisor.main import memory_delete, memory_rollback

    async def run(store):
        await _seed_working(store, "work-b", body="v1")
        await _seed_working(store, "work-b", body="v2")
        versions = await _rows(
            store,
            "SELECT version_id FROM memory_versions WHERE memory_name = ? ORDER BY version_id",
            "SELECT version_id FROM memory_versions WHERE memory_name = $1 ORDER BY version_id",
            "work-b",
        )
        with as_key(WRITE_KEY):
            await memory_rollback("work-b", versions[-1]["version_id"])
        assert (await _active(store, "work-b"))["body"] == "v1"
        with as_key(WRITE_KEY):
            await memory_delete("work-b")
        assert await _active(store, "work-b") is None
        assert await _pending(store, "work-b") == []

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_hard_delete_and_restore_refuse_canon_without_authority(backend, tmp_path, monkeypatch):
    async def run(store):
        await _seed_canon(store, "canon-i")
        out = await _a(_ms(store).hard_delete("canon-i"))
        assert "canonical" in out.lower(), out
        assert await _active(store, "canon-i") is not None
        # restore: tombstone a canonical row WITH authority, then try to restore without it
        from mori_advisor.canon_authority import CanonAuthority

        await _a(
            _ms(store).soft_delete("canon-i", actor="test", _canon_authority=CanonAuthority("t"))
        )
        assert await _active(store, "canon-i") is None
        _final, out = await _a(_ms(store).restore_memory("canon-i"))  # (final_name, message)
        assert "canonical" in out.lower(), out
        assert await _active(store, "canon-i") is None

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── R3: proposals, review, approval ────────────────────────────────────────────────────


async def _propose_rewrite(store, key: str, body: str):
    with as_key(key):
        return await _a(
            _ms(store)._write(
                name="canon-p", title="t", body=body, tier="canonical", provenance=_req(key)
            )
        )


@pytest.mark.parametrize("backend", BACKENDS)
def test_approval_applies_queued_write_exactly(backend, tmp_path, monkeypatch, api_mode):
    async def run(store):
        await _seed_canon(store, "canon-p", body="original")
        r = await _propose_rewrite(store, WRITE_KEY, "proposed text")
        out = await _a(_ms(store).approve(r.pending_id, reviewer=DREAM_KEY))
        assert "approved" in out.lower() and "not approved" not in out.lower(), out
        row = await _active(store, "canon-p")
        assert (row["body"], row["tier"]) == ("proposed text", "canonical")
        assert [p["status"] for p in await _pending(store, "canon-p")] == ["approved"]

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_approval_applies_queued_delete_and_rollback_without_requeue(
    backend, tmp_path, monkeypatch, api_mode
):
    from mori_advisor.main import memory_delete, memory_rollback

    async def run(store):
        await _seed_canon(store, "canon-q", body="v1")
        await _seed_canon(store, "canon-q", body="v2")
        vid = (
            await _rows(
                store,
                "SELECT version_id FROM memory_versions WHERE memory_name = ? ORDER BY version_id",
                "SELECT version_id FROM memory_versions WHERE memory_name = $1 ORDER BY version_id",
                "canon-q",
            )
        )[-1]["version_id"]
        with as_key(WRITE_KEY):
            await memory_rollback("canon-q", vid)
        rb = (await _pending(store, "canon-q"))[-1]
        await _a(_ms(store).approve(rb["id"], reviewer=DREAM_KEY))
        assert (await _active(store, "canon-q"))["body"] == "v1"
        with as_key(WRITE_KEY):
            await memory_delete("canon-q")
        dl = [p for p in await _pending(store, "canon-q") if p["status"] == "pending"][-1]
        await _a(_ms(store).approve(dl["id"], reviewer=DREAM_KEY))
        assert await _active(store, "canon-q") is None
        assert [p for p in await _pending(store, "canon-q") if p["status"] == "pending"] == []

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_stale_proposal_is_refused_and_changes_nothing(backend, tmp_path, monkeypatch, api_mode):
    async def run(store):
        await _seed_canon(store, "canon-p", body="original")
        r = await _propose_rewrite(store, WRITE_KEY, "proposed text")
        await _seed_canon(store, "canon-p", body="changed meanwhile")  # the target moves
        out = await _a(_ms(store).approve(r.pending_id, reviewer=DREAM_KEY))
        assert "stale-proposal" in out, out
        assert (await _active(store, "canon-p"))["body"] == "changed meanwhile"
        assert [p["status"] for p in await _pending(store, "canon-p")] == ["pending"]

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_stale_create_when_name_appears(backend, tmp_path, monkeypatch, api_mode):
    async def run(store):
        with as_key(WRITE_KEY):
            r = await _a(
                _ms(store)._write(
                    name="canon-new",
                    title="t",
                    body="p",
                    tier="canonical",
                    provenance=_req(WRITE_KEY),
                )
            )
        await _seed_canon(store, "canon-new", body="someone else")
        out = await _a(_ms(store).approve(r.pending_id, reviewer=DREAM_KEY))
        assert "stale-proposal" in out, out
        assert (await _active(store, "canon-new"))["body"] == "someone else"

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_same_proposer_supersedes_and_old_id_cannot_be_approved(
    backend, tmp_path, monkeypatch, api_mode
):
    async def run(store):
        await _seed_canon(store, "canon-p", body="original")
        first = await _propose_rewrite(store, WRITE_KEY, "first")
        second = await _propose_rewrite(store, WRITE_KEY, "second")
        assert second.pending_id != first.pending_id
        pend = {p["id"]: p for p in await _pending(store, "canon-p")}
        assert pend[first.pending_id]["status"] == "superseded-by-proposal"
        assert pend[first.pending_id]["superseded_by_id"] == second.pending_id
        assert pend[second.pending_id]["replaces_id"] == first.pending_id
        assert pend[first.pending_id]["body"] == "first", "a proposal is immutable"
        out = await _a(_ms(store).approve(first.pending_id, reviewer=DREAM_KEY))
        assert f"superseded by #{second.pending_id}" in out, out
        assert (await _active(store, "canon-p"))["body"] == "original"

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_other_proposer_is_refused_with_open_id_and_audited(
    backend, tmp_path, monkeypatch, api_mode
):
    async def run(store):
        await _seed_canon(store, "canon-p", body="original")
        first = await _propose_rewrite(store, WRITE_KEY, "first")
        other = await _propose_rewrite(store, OTHER_KEY, "takeover")
        assert not other.accepted and other.pending_id == first.pending_id
        assert "refused" in other.reason.lower()
        pend = await _pending(store, "canon-p")
        assert [(p["body"], p["status"]) for p in pend] == [("first", "pending")]
        refused = [  # keyed to this run's open id: write_audit is not truncated between PG tests
            a
            for a in await _audit(store, "propose_refused")
            if a["memory_name"] == "canon-p" and f"#{first.pending_id} " in (a["detail"] or "")
        ]
        assert [a["actor_key_name"] for a in refused] == [OTHER_KEY]

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_content_equal_reproposal_is_a_noop(backend, tmp_path, monkeypatch, api_mode):
    async def run(store):
        await _seed_canon(store, "canon-p", body="original")
        first = await _propose_rewrite(store, WRITE_KEY, "same")
        again = await _propose_rewrite(store, WRITE_KEY, "same")
        assert again.pending_id == first.pending_id
        assert len(await _pending(store, "canon-p")) == 1

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_request_key_cap_of_twenty_open_proposals(backend, tmp_path, monkeypatch, api_mode):
    async def run(store):
        for i in range(20):
            with as_key(WRITE_KEY):
                r = await _a(
                    _ms(store)._write(
                        name=f"cap-{i}",
                        title="t",
                        body="b",
                        tier="canonical",
                        provenance=_req(WRITE_KEY),
                    )
                )
            assert r.pending_id, r
        with as_key(WRITE_KEY):
            r = await _a(
                _ms(store)._write(
                    name="cap-20", title="t", body="b", tier="canonical", provenance=_req(WRITE_KEY)
                )
            )
        assert not r.accepted and r.pending_id is None and "cap" in r.reason.lower(), r
        # a content-equal re-proposal does not consume a slot
        with as_key(WRITE_KEY):
            r = await _a(
                _ms(store)._write(
                    name="cap-0", title="t", body="b", tier="canonical", provenance=_req(WRITE_KEY)
                )
            )
        assert r.pending_id, r

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_supersede_chain_is_capped_at_ten(backend, tmp_path, monkeypatch, api_mode):
    async def run(store):
        await _seed_canon(store, "canon-p", body="original")
        for i in range(10):
            r = await _propose_rewrite(store, WRITE_KEY, f"v{i}")
            assert r.pending_id, (i, r)
        r = await _propose_rewrite(store, WRITE_KEY, "v10")
        assert not r.accepted and "chain" in r.reason.lower(), r

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_ingestion_cap_of_fifty(backend, tmp_path, monkeypatch):
    async def run(store):
        for i in range(50):
            await _a(
                _ms(store).queue_pending_write(
                    name=f"ing-{i}",
                    body="b",
                    proposed_by="ingestion",
                    source="ingestion",
                    tier="canonical",
                )
            )
        out = await _a(
            _ms(store).queue_pending_write(
                name="ing-50",
                body="b",
                proposed_by="ingestion",
                source="ingestion",
                tier="canonical",
            )
        )
        assert "cap" in str(out).lower(), out
        assert await _pending(store, "ing-50") == []

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_request_proposer_comes_from_authenticated_actor(backend, tmp_path, monkeypatch, api_mode):
    """A request write's proposer is the authenticated key, never the provenance string."""

    async def run(store):
        await _seed_canon(store, "canon-p", body="original")
        with as_key(WRITE_KEY):
            r = await _a(
                _ms(store)._write(
                    name="canon-p",
                    title="t",
                    body="x",
                    tier="canonical",
                    provenance=_req("someone-else"),
                )
            )
        assert [p["proposed_by"] for p in await _pending(store, "canon-p")] == [WRITE_KEY], r

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── R4: the audit names the reviewing key ─────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_mcp_approve_audits_the_authenticated_key(backend, tmp_path, monkeypatch, api_mode):
    from mori_advisor.main import memory_approve

    async def run(store):
        await _seed_canon(store, "canon-p", body="original")
        r = await _propose_rewrite(store, WRITE_KEY, "proposed")
        with as_key(DREAM_KEY):
            out = await memory_approve(r.pending_id, note="ok", reviewer="claims-to-be-fred")
        assert "approved" in out.lower(), out
        rows = [
            a for a in await _audit(store, "approve") if a["memory_name"] == f"write:{r.pending_id}"
        ]
        assert [a["actor_key_name"] for a in rows] == [DREAM_KEY]
        assert "claims-to-be-fred" in (rows[0]["detail"] or "")
        pend = (await _pending(store, "canon-p"))[0]
        assert pend["reviewed_by"] == DREAM_KEY

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_mcp_reject_audits_the_key_and_failed_actions_audit_nothing(
    backend, tmp_path, monkeypatch, api_mode
):
    from mori_advisor.main import memory_approve, memory_reject

    async def run(store):
        await _seed_canon(store, "canon-p", body="original")
        r = await _propose_rewrite(store, WRITE_KEY, "proposed")
        with as_key(DREAM_KEY):
            await memory_reject(r.pending_id, note="no", reviewer="someone")
            out = await memory_approve(r.pending_id)  # already processed
        assert "already processed" in out.lower() or "not found" in out.lower(), out
        rej = [
            a for a in await _audit(store, "reject") if a["memory_name"] == f"write:{r.pending_id}"
        ]
        assert [a["actor_key_name"] for a in rej] == [DREAM_KEY]
        apr = [
            a for a in await _audit(store, "approve") if a["memory_name"] == f"write:{r.pending_id}"
        ]
        assert apr == [], "an approval that changed nothing must not write a success audit row"

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_reject_only_touches_pending_rows(backend, tmp_path, monkeypatch, api_mode):
    async def run(store):
        await _seed_canon(store, "canon-p", body="original")
        r = await _propose_rewrite(store, WRITE_KEY, "proposed")
        await _a(_ms(store).approve(r.pending_id, reviewer=DREAM_KEY))
        # v2.3.17: reject needs a reason and returns a structured outcome
        out = await _a(_ms(store).reject(r.pending_id, note="too late", reviewer=DREAM_KEY))
        assert out.kind == "not_pending", out
        assert [p["status"] for p in await _pending(store, "canon-p")] == ["approved"]
        out = await _a(_ms(store).reject(999999, note="no such id", reviewer=DREAM_KEY))
        assert out.kind == "not_found", out

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_pending_list_requires_dreamer(backend, tmp_path, monkeypatch, api_mode):
    from mori_advisor.main import memory_pending_list

    async def run(store):
        with as_key(WRITE_KEY):
            out = await memory_pending_list()
        assert "dreamer" in out.lower() and "required" in out.lower(), out
        with as_key(DREAM_KEY):
            out = await memory_pending_list()
        assert "required" not in out.lower(), out

    _run_with_backend(backend, tmp_path, monkeypatch, run)
