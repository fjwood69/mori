"""Rate-limit middleware (#23 D).

Drives a real Starlette app wrapped in ApiKeyMiddleware through TestClient so the
limiter is exercised exactly as in production: after auth, keyed on the API-key
name, scope-aware, returning 429 + Retry-After when the bucket is empty.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import mori_advisor.middleware as mw
from mori_advisor.throttle import InMemoryRateLimitStore, RateLimitConfig


def _build_client(monkeypatch, cfg: RateLimitConfig):
    """A Starlette app behind ApiKeyMiddleware with two dummy endpoints.

    Auth is stubbed: x-api-key 'k1' -> client 'c1', 'k2' -> 'c2', else 401.
    """

    def fake_check_key(provided):
        return {"k1": "c1", "k2": "c2"}.get(provided)

    monkeypatch.setattr(mw, "check_key", fake_check_key)
    monkeypatch.setattr(mw, "role_for", lambda name: "write")
    monkeypatch.setattr(mw, "_rate_cfg", cfg)
    monkeypatch.setattr(mw, "rate_limit_store", InMemoryRateLimitStore())

    async def w(request):
        return JSONResponse({"ok": "write"})

    async def r(request):
        return JSONResponse({"ok": "read"})

    app = Starlette(routes=[Route("/w", w, methods=["POST"]), Route("/r", r, methods=["GET"])])
    app.add_middleware(mw.ApiKeyMiddleware)
    return TestClient(app)


def test_writes_limited_then_429(monkeypatch):
    client = _build_client(monkeypatch, RateLimitConfig(2, 60, "writes"))
    h = {"x-api-key": "k1"}
    assert client.post("/w", headers=h).status_code == 200
    assert client.post("/w", headers=h).status_code == 200
    resp = client.post("/w", headers=h)
    assert resp.status_code == 429
    assert int(resp.headers["retry-after"]) >= 1
    assert "rate limit" in resp.json()["detail"].lower()


def test_reads_not_limited_under_writes_scope(monkeypatch):
    client = _build_client(monkeypatch, RateLimitConfig(2, 60, "writes"))
    h = {"x-api-key": "k1"}
    # Far more than the limit, but GET is exempt under 'writes' scope.
    for _ in range(6):
        assert client.get("/r", headers=h).status_code == 200


def test_all_scope_limits_reads_too(monkeypatch):
    client = _build_client(monkeypatch, RateLimitConfig(2, 60, "all"))
    h = {"x-api-key": "k1"}
    assert client.get("/r", headers=h).status_code == 200
    assert client.get("/r", headers=h).status_code == 200
    assert client.get("/r", headers=h).status_code == 429


def test_keys_are_independent(monkeypatch):
    client = _build_client(monkeypatch, RateLimitConfig(1, 60, "writes"))
    assert client.post("/w", headers={"x-api-key": "k1"}).status_code == 200
    assert client.post("/w", headers={"x-api-key": "k1"}).status_code == 429
    # Separate key has its own bucket.
    assert client.post("/w", headers={"x-api-key": "k2"}).status_code == 200


def test_disabled_config_no_limiting(monkeypatch):
    client = _build_client(monkeypatch, RateLimitConfig(None, None, "writes"))
    h = {"x-api-key": "k1"}
    for _ in range(10):
        assert client.post("/w", headers=h).status_code == 200


def test_unauthenticated_is_401_not_429(monkeypatch):
    """Rate limiting runs AFTER auth — an unknown key is 401, never counted."""
    client = _build_client(monkeypatch, RateLimitConfig(1, 60, "writes"))
    for _ in range(5):
        assert client.post("/w", headers={"x-api-key": "nope"}).status_code == 401


def test_open_paths_never_limited(monkeypatch):
    """/health is in OPEN_PATHS — bypasses auth AND the limiter."""
    _build_client(monkeypatch, RateLimitConfig(1, 60, "all"))  # for monkeypatch side effects

    async def health(request):
        return JSONResponse({"status": "ok"})

    app = Starlette(routes=[Route("/health", health, methods=["GET"])])
    app.add_middleware(mw.ApiKeyMiddleware)
    c = TestClient(app)
    for _ in range(5):
        assert c.get("/health").status_code == 200
