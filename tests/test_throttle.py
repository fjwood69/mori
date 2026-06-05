"""Throttle foundation — config parsing + in-memory adapters (#23 C/D groundwork).

Pure-unit, deterministic via an injectable clock. No backend/DB required; the
Postgres adapter (and its dual-backend tests) land with #23 C/D once migration 8
is merged. Async store calls run via ``asyncio.run`` to avoid a pytest-asyncio
dependency (matches the repo's existing async-test style).
"""

from __future__ import annotations

import asyncio

import pytest

from mori_advisor.throttle import (
    DEFAULT_CACHE_TTL,
    DEFAULT_CLAIM_TTL,
    InMemoryIdempotencyStore,
    InMemoryRateLimitStore,
    idempotency_ttls,
    make_idempotency_store,
    make_rate_limit_store,
    rate_limit_config,
    throttle_safety_warning,
)
from mori_advisor.throttle.base import (
    IdempotencyState,
    parse_rate_limit,
    parse_scope,
    should_limit,
)


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def run(coro):
    return asyncio.run(coro)


# ── parse_rate_limit ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("120/min", (120, 60)),
        ("60/sec", (60, 1)),
        ("1000/hour", (1000, 3600)),
        ("10/m", (10, 60)),
        ("  30 / minute ", (30, 60)),
    ],
)
def test_parse_rate_limit_valid(spec, expected):
    assert parse_rate_limit(spec) == expected


@pytest.mark.parametrize("spec", ["", "0", "off", "none", "disabled", "0/min", "-5/min"])
def test_parse_rate_limit_disabled(spec):
    assert parse_rate_limit(spec) is None


@pytest.mark.parametrize("spec", ["120", "120/decade", "abc/min", "12/"])
def test_parse_rate_limit_malformed_raises(spec):
    with pytest.raises(ValueError):
        parse_rate_limit(spec)


def test_parse_scope():
    assert parse_scope(None) == "writes"
    assert parse_scope("") == "writes"
    assert parse_scope("writes") == "writes"
    assert parse_scope("ALL") == "all"
    with pytest.raises(ValueError):
        parse_scope("sometimes")


def test_should_limit():
    for m in ("POST", "put", "Patch", "DELETE"):
        assert should_limit(m, "writes")
    assert not should_limit("GET", "writes")
    assert not should_limit("HEAD", "writes")
    assert should_limit("GET", "all")  # all scope limits everything


# ── env config readers ────────────────────────────────────────────────────────


def test_rate_limit_config_opt_in_disabled_by_default(monkeypatch):
    # Rate limiting is opt-in — unset MORI_RATE_LIMIT means disabled.
    monkeypatch.delenv("MORI_RATE_LIMIT", raising=False)
    monkeypatch.delenv("MORI_RATE_LIMIT_SCOPE", raising=False)
    cfg = rate_limit_config()
    assert not cfg.enabled and cfg.limit is None
    assert cfg.scope == "writes"  # scope still resolves (used once enabled)


def test_rate_limit_config_enabled_when_set(monkeypatch):
    monkeypatch.setenv("MORI_RATE_LIMIT", "120/min")
    cfg = rate_limit_config()
    assert cfg.enabled and cfg.limit == 120 and cfg.window_seconds == 60


def test_rate_limit_config_disabled(monkeypatch):
    monkeypatch.setenv("MORI_RATE_LIMIT", "off")
    cfg = rate_limit_config()
    assert not cfg.enabled and cfg.limit is None


def test_idempotency_ttls_defaults(monkeypatch):
    for v in ("MORI_IDEMPOTENCY_CLAIM_TTL", "MORI_IDEMPOTENCY_CACHE_TTL", "MORI_IDEMPOTENCY_TTL"):
        monkeypatch.delenv(v, raising=False)
    assert idempotency_ttls() == (DEFAULT_CLAIM_TTL, DEFAULT_CACHE_TTL)


def test_idempotency_ttls_split_and_legacy_fallback(monkeypatch):
    monkeypatch.setenv("MORI_IDEMPOTENCY_CLAIM_TTL", "15")
    monkeypatch.delenv("MORI_IDEMPOTENCY_CACHE_TTL", raising=False)
    monkeypatch.setenv("MORI_IDEMPOTENCY_TTL", "3600")  # legacy → cache fallback
    assert idempotency_ttls() == (15, 3600)


# ── rate limiter (token bucket) ───────────────────────────────────────────────


def test_rate_limit_admits_under_limit_then_denies():
    clock = FakeClock()
    store = InMemoryRateLimitStore(clock=clock)

    verdicts = [run(store.check("k", limit=3, window_seconds=60)) for _ in range(3)]
    assert all(v.allowed for v in verdicts)
    assert [v.remaining for v in verdicts] == [2, 1, 0]

    denied = run(store.check("k", limit=3, window_seconds=60))
    assert not denied.allowed
    assert denied.remaining == 0
    # Next single token at rate 3/60 = 0.05/s → 20s.
    assert denied.retry_after == pytest.approx(20.0)


def test_rate_limit_refills_over_time():
    clock = FakeClock()
    store = InMemoryRateLimitStore(clock=clock)
    for _ in range(2):
        assert run(store.check("k", 2, 60)).allowed
    assert not run(store.check("k", 2, 60)).allowed

    clock.advance(61)  # bucket refills to capacity
    assert run(store.check("k", 2, 60)).allowed


def test_rate_limit_bursts_then_sustains():
    clock = FakeClock()
    store = InMemoryRateLimitStore(clock=clock)
    # Fresh key may burst the full limit immediately.
    assert all(run(store.check("k", 5, 5)).allowed for _ in range(5))
    assert not run(store.check("k", 5, 5)).allowed
    clock.advance(1)  # rate 1/s → exactly one token back
    assert run(store.check("k", 5, 5)).allowed
    assert not run(store.check("k", 5, 5)).allowed


def test_rate_limit_keys_are_independent():
    store = InMemoryRateLimitStore(clock=FakeClock())
    assert run(store.check("a", 1, 60)).allowed
    assert not run(store.check("a", 1, 60)).allowed
    assert run(store.check("b", 1, 60)).allowed


def test_rate_limit_cleanup_evicts_idle():
    clock = FakeClock()
    store = InMemoryRateLimitStore(clock=clock)
    run(store.check("k", 1, 10))  # consume the only token
    clock.advance(11)  # bucket would be fully refilled → idle
    assert run(store.cleanup()) == 1
    # busy key (just spent a token) is NOT evicted
    run(store.check("busy", 5, 100))
    assert run(store.cleanup()) == 0


# ── idempotency ───────────────────────────────────────────────────────────────


def test_idempotency_fresh_then_replay():
    store = InMemoryIdempotencyStore(clock=FakeClock())
    out = run(store.begin("key1", "digestA", 30, 3600))
    assert out.state is IdempotencyState.FRESH and out.claim_token is not None

    assert run(store.complete("key1", out.claim_token, 201, '{"ok":true}')) is True

    replay = run(store.begin("key1", "digestA", 30, 3600))
    assert replay.state is IdempotencyState.REPLAY
    assert replay.record.status_code == 201
    assert replay.record.response_body == '{"ok":true}'


def test_idempotency_in_progress_before_complete():
    store = InMemoryIdempotencyStore(clock=FakeClock())
    assert run(store.begin("k", "d", 30, 3600)).state is IdempotencyState.FRESH
    assert run(store.begin("k", "d", 30, 3600)).state is IdempotencyState.IN_PROGRESS


def test_idempotency_mismatch_even_while_in_progress():
    store = InMemoryIdempotencyStore(clock=FakeClock())
    run(store.begin("k", "digestA", 30, 3600))
    out = run(store.begin("k", "digestB", 30, 3600))
    assert out.state is IdempotencyState.MISMATCH


def test_idempotency_stale_claim_is_stolen():
    clock = FakeClock()
    store = InMemoryIdempotencyStore(clock=clock)
    first = run(store.begin("k", "d", claim_ttl=30, cache_ttl=3600))
    assert first.state is IdempotencyState.FRESH

    clock.advance(31)  # claim now stale (crashed worker scenario)
    stolen = run(store.begin("k", "d2", claim_ttl=30, cache_ttl=3600))
    assert stolen.state is IdempotencyState.FRESH  # new owner

    # The original (crashed) worker's late completion must NOT clobber.
    assert run(store.complete("k", first.claim_token, 200, "stale")) is False
    # The new owner can complete.
    assert run(store.complete("k", stolen.claim_token, 201, "fresh")) is True


def test_idempotency_cache_expiry_reclaims_key():
    clock = FakeClock()
    store = InMemoryIdempotencyStore(clock=clock)
    out = run(store.begin("k", "d", claim_ttl=30, cache_ttl=100))
    run(store.complete("k", out.claim_token, 200, "body"))
    clock.advance(101)  # past cache TTL
    again = run(store.begin("k", "d2", 30, 100))
    assert again.state is IdempotencyState.FRESH


def test_idempotency_complete_wrong_token_noop():
    store = InMemoryIdempotencyStore(clock=FakeClock())
    run(store.begin("k", "d", 30, 3600))
    assert run(store.complete("k", "not-the-token", 200, "x")) is False


def test_idempotency_cleanup_removes_expired():
    clock = FakeClock()
    store = InMemoryIdempotencyStore(clock=clock)
    run(store.begin("claim", "d", claim_ttl=10, cache_ttl=3600))  # will go stale
    out = run(store.begin("replay", "d", claim_ttl=30, cache_ttl=50))
    run(store.complete("replay", out.claim_token, 200, "b"))
    clock.advance(51)  # claim stale AND replay cache expired
    assert run(store.cleanup()) == 2


def test_reserve_context_manager_fresh_and_replay():
    store = InMemoryIdempotencyStore(clock=FakeClock())

    async def scenario():
        async with store.reserve("k", "d", claim_ttl=30, cache_ttl=3600) as r:
            assert r.is_fresh
            assert await r.complete(201, "body") is True
        async with store.reserve("k", "d", claim_ttl=30, cache_ttl=3600) as r2:
            return r2.state, r2.record

    state, record = run(scenario())
    assert state is IdempotencyState.REPLAY
    assert record.status_code == 201 and record.response_body == "body"


# ── factories + safety guardrail ──────────────────────────────────────────────


def test_factories_default_memory(monkeypatch):
    monkeypatch.delenv("MORI_THROTTLE_STORE", raising=False)
    assert isinstance(make_rate_limit_store(), InMemoryRateLimitStore)
    assert isinstance(make_idempotency_store(), InMemoryIdempotencyStore)


def test_factories_postgres_not_yet(monkeypatch):
    monkeypatch.setenv("MORI_THROTTLE_STORE", "postgres")
    with pytest.raises(NotImplementedError):
        make_rate_limit_store()
    with pytest.raises(NotImplementedError):
        make_idempotency_store()


def test_factories_unknown_raises(monkeypatch):
    monkeypatch.setenv("MORI_THROTTLE_STORE", "redis")
    with pytest.raises(ValueError):
        make_rate_limit_store()


def test_safety_warning_fires_on_memory_multiworker(monkeypatch):
    monkeypatch.setenv("MORI_THROTTLE_STORE", "memory")
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    msg = throttle_safety_warning()
    assert msg is not None and "4" in msg


def test_safety_warning_silent_single_worker(monkeypatch):
    monkeypatch.setenv("MORI_THROTTLE_STORE", "memory")
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.delenv("UVICORN_WORKERS", raising=False)
    assert throttle_safety_warning() is None


def test_safety_warning_silent_for_postgres(monkeypatch):
    monkeypatch.setenv("MORI_THROTTLE_STORE", "postgres")
    monkeypatch.setenv("WEB_CONCURRENCY", "8")
    assert throttle_safety_warning() is None
