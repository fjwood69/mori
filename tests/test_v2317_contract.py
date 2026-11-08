"""v2.3.17 — the deploy contract proves the lock (design §1.2, §1.4 test 10; ruling P1-1, P2-1, Q2).

``scripts/verify-deployment.py`` runs against the REAL app (with the API-key middleware) on both
backends, with a WRITE key. It must pass with both canaries in place — twice in a row, the second
time on the proposal already open — and FAIL when: the live canary is gone (an approval made by
mistake), the tombstone is restored, another identity holds the delete, the route answers 403, the
lock is bypassed, or the canary has lost its protection. ``scripts/verify-store.py`` (the host-side
half) passes with intact canaries and fails closed otherwise, including with no Postgres URL.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
from pathlib import Path

import httpx
import pytest

from tests.test_mcp_tools import BACKENDS, PG_URL, _run_with_backend

ROOT = Path(__file__).resolve().parent.parent
CD_KEY, CD_SECRET = "cd-verify", "c" * 64
OTHER_KEY, OTHER_SECRET = "other-console", "o" * 64
DREAM_KEY, DREAM_SECRET = "fred-review", "d" * 64
LIVE, TOMB = "cd-canary-live", "cd-canary-tombstone"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), ROOT / "scripts" / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


async def _a(x):
    return await x if inspect.isawaitable(x) else x


def _ms(store):
    return store._mem if hasattr(store, "_mem") else store


@pytest.fixture
def keyed(monkeypatch):
    import mori_advisor.auth as auth
    import mori_advisor.middleware as mw
    import mori_advisor.policy as pol

    monkeypatch.setattr(pol, "_TD_MODE", "api")
    monkeypatch.setattr(pol, "_LOCAL_FULL_ACCESS", False)
    monkeypatch.setattr(pol, "_ROLES", {CD_KEY: "write", OTHER_KEY: "write", DREAM_KEY: "dreamer"})
    monkeypatch.setattr(
        auth, "_KEYS", {CD_KEY: CD_SECRET, OTHER_KEY: OTHER_SECRET, DREAM_KEY: DREAM_SECRET}
    )
    # /api/smoke's NATS check (degraded-only) must not reach a real bus, nor wait out its 2 s
    # timeout on retries, in a test
    import nats

    import mori_advisor.main as m

    async def _no_bus(*a, **k):
        raise ConnectionRefusedError("no NATS in tests")

    monkeypatch.setattr(m, "NATS_URL", "nats://127.0.0.1:9")
    monkeypatch.setattr(nats, "connect", _no_bus)
    if hasattr(mw, "_rate_cfg"):
        from mori_advisor.middleware import RateLimitConfig

        monkeypatch.setattr(mw, "_rate_cfg", RateLimitConfig(None, None, "writes"))


async def _seed_canaries(store, *, protect: bool = True, tombstone: bool = True):
    from mori_advisor.canon_authority import CanonAuthority
    from mori_advisor.provenance import SYSTEM

    auth = CanonAuthority("test: Fred's review key creates the canaries")
    names = [LIVE, TOMB] if tombstone else [LIVE]
    for name in names:
        r = await _a(
            _ms(store)._write(
                name=name,
                title=f"CD DEPLOY CANARY ({name}) — approving a delete DELETES it",
                description="deploy contract probe",
                type="decision",
                tier="canonical",
                body="canary",
                tags=["cd-canary"],
                provenance=SYSTEM,
                _canon_authority=auth,
            )
        )
        assert r.accepted, r
        if protect:
            await _a(_ms(store).protect(name, protected=True, actor="test"))
    if tombstone:
        await _a(_ms(store).soft_delete(TOMB, actor="test", _canon_authority=auth))


async def _run_contract(monkeypatch, key=CD_SECRET, key_name=CD_KEY, intercept=None) -> int:
    """Run verify-deployment.main() against the real app, on THIS event loop (PG pools are
    loop-bound): the contract runs in a thread; each request is marshalled back to this loop."""
    from starlette.middleware import Middleware

    import mori_advisor.main as m
    import mori_advisor.middleware as mw

    vd = _load("verify-deployment")
    app = m.mcp.http_app(transport="streamable-http", middleware=[Middleware(mw.ApiKeyMiddleware)])
    loop = asyncio.get_running_loop()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:

        def call(method, url, key=None, body=None):
            path = url.replace("http://testserver", "")
            if intercept is not None:
                hit = intercept(method, path, key)
                if hit is not None:
                    return hit
            headers = {"X-Api-Key": key} if key else {}
            fut = asyncio.run_coroutine_threadsafe(
                client.request(method, path, headers=headers, json=body), loop
            )
            r = fut.result(timeout=30)
            try:
                return r.status_code, r.json()
            except ValueError:
                return r.status_code, None

        monkeypatch.setattr(vd, "_call", call)
        return await asyncio.to_thread(
            vd.main, ["verify-deployment.py", "http://testserver", key, key_name]
        )


async def _approve(store, name: str):
    from mori_advisor.policy import Actor, current_actor
    from tests.test_canon_lock import _pending

    pid = [p for p in await _pending(store, name) if p["status"] == "pending"][0]["id"]
    token = current_actor.set(Actor(DREAM_KEY, "dreamer"))
    try:
        await _a(_ms(store).approve(pid, reviewer=DREAM_KEY))
    finally:
        current_actor.reset(token)


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_contract_passes_with_both_canaries_twice(
    backend, tmp_path, monkeypatch, keyed, capsys
):
    async def run(store):
        await _seed_canaries(store)
        assert await _run_contract(monkeypatch) == 0, capsys.readouterr().out
        assert await _run_contract(monkeypatch) == 0, capsys.readouterr().out  # same open id

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_contract_fails_when_the_live_canary_was_approved_away(
    backend, tmp_path, monkeypatch, keyed, capsys
):
    async def run(store):
        await _seed_canaries(store)
        assert await _run_contract(monkeypatch) == 0
        await _approve(store, LIVE)  # an approval made by mistake
        assert await _run_contract(monkeypatch) == 1
        assert "DELETE live canary -> 404" in capsys.readouterr().out

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_contract_fails_when_the_tombstone_is_restored(
    backend, tmp_path, monkeypatch, keyed, capsys
):
    from mori_advisor.canon_authority import CanonAuthority

    async def run(store):
        await _seed_canaries(store)
        await _a(
            _ms(store).restore_memory(TOMB, actor="test", _canon_authority=CanonAuthority("t"))
        )
        assert await _run_contract(monkeypatch) == 1
        assert "restore tombstone -> 404" in capsys.readouterr().out

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_contract_fails_when_another_identity_holds_the_delete(
    backend, tmp_path, monkeypatch, keyed, capsys
):
    async def run(store):
        await _seed_canaries(store)
        # another write key proposes deleting the canary first
        assert await _run_contract(monkeypatch, key=OTHER_SECRET, key_name=OTHER_KEY) == 0
        capsys.readouterr()
        assert await _run_contract(monkeypatch) == 1
        assert "DELETE live canary -> 409" in capsys.readouterr().out

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_contract_fails_when_the_lock_is_bypassed(
    backend, tmp_path, monkeypatch, keyed, capsys
):
    import mori_advisor.memory_store as ms
    import mori_advisor.store.postgres_store as ps

    async def run(store):
        await _seed_canaries(store)
        monkeypatch.setattr(ms, "has_canon_authority", lambda *a, **k: True)
        monkeypatch.setattr(ps, "has_canon_authority", lambda *a, **k: True)
        assert await _run_contract(monkeypatch) == 1
        assert "DELETE live canary -> 200" in capsys.readouterr().out

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("status", [403, 200, 404])
def test_the_contract_fails_on_a_route_403_a_200_or_a_404_for_either_probe(
    tmp_path, monkeypatch, keyed, capsys, status
):
    """K8/K9: a 403 from the route (the route needs dreamer again), a 200 or a 404 counts as
    neither queued nor refused."""

    async def run(store):
        await _seed_canaries(store)
        for target in ("DELETE", "POST"):

            def intercept(method, path, key, target=target):
                # only the KEYED probe: the no-key 401 checks stay real, or the contract would fail
                # for another reason and the mutant under test would go unseen
                if (
                    key
                    and method == target
                    and (path == f"/api/memories/{LIVE}" or path == f"/api/memories/{TOMB}/restore")
                ):
                    return status, {"error": "simulated"}
                return None

            assert await _run_contract(monkeypatch, intercept=intercept) == 1, target

    _run_with_backend("sqlite", tmp_path, monkeypatch, run)


@pytest.mark.parametrize(
    "route",
    ["/api/audit", f"/api/memories/{LIVE}/approve", "/api/memories/verify-deployment-probe/reject"],
)
def test_the_contract_fails_when_a_dreamer_route_answers_the_write_key(
    tmp_path, monkeypatch, keyed, capsys, route
):
    """K13: a dreamer-only route that answers 200 (or anything but 403) to the write key fails."""

    def intercept(method, path, key):
        if key and path.split("?")[0] == route:
            return 200, {"status": "ok"}
        return None

    async def run(store):
        await _seed_canaries(store)
        assert await _run_contract(monkeypatch, intercept=intercept) == 1

    _run_with_backend("sqlite", tmp_path, monkeypatch, run)


def test_the_contract_fails_when_the_canary_lost_its_protection(
    tmp_path, monkeypatch, keyed, capsys
):
    async def run(store):
        await _seed_canaries(store, protect=False)
        assert await _run_contract(monkeypatch) == 1
        assert "protected=False" in capsys.readouterr().out

    _run_with_backend("sqlite", tmp_path, monkeypatch, run)


def test_the_contract_checks_the_proposer_is_this_key(tmp_path, monkeypatch, keyed, capsys):
    """K10: a 202 whose proposer is another key fails (simulated transport)."""

    def intercept(method, path, key):
        if key and method == "DELETE" and path == f"/api/memories/{LIVE}":
            return 202, {
                "status": "pending",
                "name": LIVE,
                "pending_id": 7,
                "operation": "delete",
                "proposed_by": OTHER_KEY,
            }
        return None

    async def run(store):
        await _seed_canaries(store)
        assert await _run_contract(monkeypatch, intercept=intercept) == 1
        assert "proposer_is_this_key=False" in capsys.readouterr().out

    _run_with_backend("sqlite", tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_contract_passes_after_the_store_check_and_old_probe_rows(
    backend, tmp_path, monkeypatch, keyed, capsys
):
    """UAT finding (8 Nov): a REST write to a working row another key owns is queued (202). The
    store check writes its probe first, and production holds the pre-v2.3.17 probe rows written by
    the key the old contract borrowed — neither may make CD's own contract fail."""
    from mori_advisor.provenance import request_provenance

    vs = _load("verify-store")

    async def run(store):
        await _seed_canaries(store)
        await vs._probe_write(store)  # the host-side store check runs first, every deploy
        for old in ("verify-deployment-softdel-probe", "verify-deployment-write-probe"):
            r = await _a(
                _ms(store)._write(
                    name=old,
                    title="old probe",
                    body="probe",
                    tier="working",
                    origin_clients=[OTHER_KEY],
                    provenance=request_provenance("rest", OTHER_KEY, "test", op="propose_new"),
                )
            )
            assert r.accepted, r
        assert await _run_contract(monkeypatch) == 0, capsys.readouterr().out
        assert await _run_contract(monkeypatch) == 0, capsys.readouterr().out

    _run_with_backend(backend, tmp_path, monkeypatch, run)


def test_the_contract_needs_three_arguments():
    vd = _load("verify-deployment")
    assert vd.main(["verify-deployment.py", "http://x", "k"]) == 2


# ── the host-side store check ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_store_check_passes_with_intact_canaries(backend, tmp_path, monkeypatch, capsys):
    vs = _load("verify-store")
    if backend == "postgres":
        monkeypatch.setenv("MORI_DATABASE_URL", PG_URL)  # the probe-row canary reads it

    async def run(store):
        await _seed_canaries(store)
        check = vs._check_pg if backend == "postgres" else vs._check_sqlite
        assert await check(store) == 0, capsys.readouterr().out

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(
    "damage", ["live-missing", "unprotected", "tomb-restored", "tomb-missing", "tomb-shadowed"]
)
def test_the_store_check_fails_when_a_canary_is_damaged(backend, tmp_path, monkeypatch, damage):
    from mori_advisor.canon_authority import CanonAuthority

    vs = _load("verify-store")
    if backend == "postgres":
        monkeypatch.setenv("MORI_DATABASE_URL", PG_URL)  # the probe-row canary reads it

    async def run(store):
        if damage == "unprotected":
            await _seed_canaries(store, protect=False)
        elif damage == "tomb-missing":
            await _seed_canaries(store, tombstone=False)
        else:
            await _seed_canaries(store)
        if damage == "live-missing":
            await _a(_ms(store).soft_delete(LIVE, actor="t", _canon_authority=CanonAuthority("t")))
        if damage == "tomb-restored":
            await _a(
                _ms(store).restore_memory(TOMB, actor="t", _canon_authority=CanonAuthority("t"))
            )
        if damage == "tomb-shadowed":  # the deleted row is intact, but an ACTIVE row holds the name
            from mori_advisor.provenance import SYSTEM

            r = await _a(
                _ms(store)._write(
                    name=TOMB,
                    title="shadow",
                    tier="canonical",
                    body="x",
                    tags=["cd-canary"],
                    provenance=SYSTEM,
                    _canon_authority=CanonAuthority("t"),
                )
            )
            assert r.accepted, r
        check = vs._check_pg if backend == "postgres" else vs._check_sqlite
        assert await check(store) == 1

    _run_with_backend(backend, tmp_path, monkeypatch, run)


def test_the_store_check_fails_closed_without_a_postgres_url(tmp_path, monkeypatch, capsys):
    vs = _load("verify-store")
    monkeypatch.delenv("MORI_DATABASE_URL", raising=False)
    monkeypatch.setenv("MORI_ADVISOR_DATA", str(tmp_path))
    assert vs.main(["verify-store.py"]) == 1
    assert "fails closed" in capsys.readouterr().out
