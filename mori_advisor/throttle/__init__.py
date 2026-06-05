"""Throttling foundation for governed-write hardening (#23 C/D).

Public surface:

* contracts + result types — :mod:`mori_advisor.throttle.base`
* in-memory adapters (default) — :mod:`mori_advisor.throttle.memory`
* factories + config readers — here

The factories read configuration from the environment and return the adapter
selected by ``MORI_THROTTLE_STORE`` (``memory`` default; ``postgres`` lands with
the #23 C/D Postgres path). Call sites depend only on the abstract base, so the
backing store swaps without touching middleware or route handlers.

Config (solo tier / small teams — Enterprise scales via a different mechanism):

* ``MORI_RATE_LIMIT``               e.g. ``"120/min"`` — default below; ``0``/``off`` disables
* ``MORI_RATE_LIMIT_SCOPE``         ``writes`` (default) | ``all``
* ``MORI_IDEMPOTENCY_CLAIM_TTL``    seconds an in-progress claim is held before it can be
                                    stolen (default 30) — short, so a crashed write self-heals
* ``MORI_IDEMPOTENCY_CACHE_TTL``    seconds a completed response stays replayable
                                    (default 86400) — falls back to ``MORI_IDEMPOTENCY_TTL``
* ``MORI_THROTTLE_STORE``           ``memory`` (default) | ``postgres``
"""

from __future__ import annotations

import os

from .base import (
    WRITE_METHODS,
    IdempotencyOutcome,
    IdempotencyRecord,
    IdempotencyState,
    IdempotencyStore,
    RateLimitStore,
    RateLimitVerdict,
    Reservation,
    parse_rate_limit,
    parse_scope,
    should_limit,
)
from .memory import InMemoryIdempotencyStore, InMemoryRateLimitStore

__all__ = [
    "WRITE_METHODS",
    "IdempotencyOutcome",
    "IdempotencyRecord",
    "IdempotencyState",
    "IdempotencyStore",
    "RateLimitStore",
    "RateLimitVerdict",
    "Reservation",
    "InMemoryIdempotencyStore",
    "InMemoryRateLimitStore",
    "parse_rate_limit",
    "parse_scope",
    "should_limit",
    "RateLimitConfig",
    "rate_limit_config",
    "idempotency_ttls",
    "make_rate_limit_store",
    "make_idempotency_store",
    "throttle_safety_warning",
    "DEFAULT_RATE_LIMIT",
    "DEFAULT_CLAIM_TTL",
    "DEFAULT_CACHE_TTL",
]

# Recommended default: 120 writes/min/key. Generous enough that no interactive
# human or well-behaved agent notices it, low enough to cap a runaway autonomous
# writer (a loop hammering POST /api/memories) before it floods the store.
DEFAULT_RATE_LIMIT = "120/min"
DEFAULT_CLAIM_TTL = 30  # seconds — short, so a crashed in-progress write self-heals
DEFAULT_CACHE_TTL = 86_400  # 24h — a completed response stays replayable this long


class RateLimitConfig:
    """Resolved rate-limit configuration. ``limit`` is ``None`` when disabled."""

    __slots__ = ("limit", "window_seconds", "scope")

    def __init__(self, limit: int | None, window_seconds: int | None, scope: str) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.scope = scope

    @property
    def enabled(self) -> bool:
        return self.limit is not None


def rate_limit_config() -> RateLimitConfig:
    """Read + validate rate-limit config from the environment (fail loud on typos)."""
    parsed = parse_rate_limit(os.environ.get("MORI_RATE_LIMIT", DEFAULT_RATE_LIMIT))
    scope = parse_scope(os.environ.get("MORI_RATE_LIMIT_SCOPE"))
    if parsed is None:
        return RateLimitConfig(None, None, scope)
    limit, window = parsed
    return RateLimitConfig(limit, window, scope)


def _positive_int_env(name: str, default: int, *fallback: str) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        for alt in fallback:
            raw = os.environ.get(alt, "").strip()
            if raw:
                break
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name}={raw!r} is not an integer") from exc
    return value if value > 0 else default


def idempotency_ttls() -> tuple[int, int]:
    """Return ``(claim_ttl, cache_ttl)`` in seconds from the environment.

    ``cache_ttl`` falls back to the legacy ``MORI_IDEMPOTENCY_TTL`` if the split
    ``MORI_IDEMPOTENCY_CACHE_TTL`` is unset.
    """
    claim_ttl = _positive_int_env("MORI_IDEMPOTENCY_CLAIM_TTL", DEFAULT_CLAIM_TTL)
    cache_ttl = _positive_int_env(
        "MORI_IDEMPOTENCY_CACHE_TTL", DEFAULT_CACHE_TTL, "MORI_IDEMPOTENCY_TTL"
    )
    return claim_ttl, cache_ttl


def _backend() -> str:
    return os.environ.get("MORI_THROTTLE_STORE", "memory").strip().lower()


_NOT_YET = (
    "MORI_THROTTLE_STORE=postgres is not available yet — the shared Postgres "
    "adapter lands with #23 C/D (migration 9/10). Use 'memory' (single-instance) "
    "until then."
)


def make_rate_limit_store() -> RateLimitStore:
    backend = _backend()
    if backend == "memory":
        return InMemoryRateLimitStore()
    if backend == "postgres":
        raise NotImplementedError(_NOT_YET)
    raise ValueError(f"MORI_THROTTLE_STORE={backend!r} unknown; use 'memory' or 'postgres'")


def make_idempotency_store() -> IdempotencyStore:
    backend = _backend()
    if backend == "memory":
        return InMemoryIdempotencyStore()
    if backend == "postgres":
        raise NotImplementedError(_NOT_YET)
    raise ValueError(f"MORI_THROTTLE_STORE={backend!r} unknown; use 'memory' or 'postgres'")


def throttle_safety_warning() -> str | None:
    """Return a warning string if the in-memory store is used with >1 worker, else None.

    The in-memory adapter fails *open* under horizontal scaling (per-worker
    counters → effective limit is ``N × limit``). Startup should log this so the
    silent breach is visible. Returns the message (testable) rather than logging
    directly, so the caller owns the logger.
    """
    if _backend() != "memory":
        return None
    raw = (os.environ.get("WEB_CONCURRENCY") or os.environ.get("UVICORN_WORKERS") or "").strip()
    try:
        workers = int(raw) if raw else 1
    except ValueError:
        workers = 1
    if workers > 1:
        return (
            f"MORI_THROTTLE_STORE=memory with {workers} workers: each worker keeps "
            f"its own counters, so the effective rate limit is ~{workers}× the "
            "configured value and idempotency does not de-duplicate across workers. "
            "Set MORI_THROTTLE_STORE=postgres before scaling out."
        )
    return None
