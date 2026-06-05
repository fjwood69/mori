"""Throttling foundation — pluggable stores for idempotency and rate limiting.

This module defines the *contracts* (abstract bases + result types + config
parsing) that the governed write-API hardening (#23 parts C and D) builds on.
The deliberate design choice — per the advisor review on #23 — is that neither
idempotency nor rate-limit state is baked into the ASGI middleware as a process
``dict``. Both sit behind an async store interface with two implementations:

* :mod:`mori_advisor.throttle.memory` in-memory adapters — the default, correct
  for a single-instance (solo / small-team) deployment.
* a Postgres-backed adapter (lands with the #23 C/D Postgres path, migration
  9/10) — required once the Quadlet substrate scales to more than one worker,
  because per-instance counters would otherwise multiply the intended limit.

The interface is ``async`` so the in-memory adapter and the future Postgres
adapter present an identical surface to the (async) middleware and route
handlers — no call-site changes when the backing store is swapped via
``MORI_THROTTLE_STORE``.

Two design decisions hardened after the advisor review (so they don't become
contract debt once C/D depend on them):

* **Idempotency is a self-healing execution cache, not a distributed lock.** A
  claim has a *short* TTL; a completed response has a *long* TTL. A stale
  in-progress claim (worker crashed between ``begin`` and ``complete``) is
  *stolen* by the next caller rather than wedging the key until expiry. A claim
  token guards ``complete`` so a stolen claim's late finisher cannot clobber.
* **Rate limiting is a token bucket** — O(1) state per key (no per-hit deque),
  natural burst tolerance, and idle keys are evicted, so high-cardinality keys
  cannot leak memory.

Scope: solo tier / small teams. Enterprise-scale throttling is handled by a
different mechanism and is intentionally out of scope here.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from typing import AsyncIterator, Callable

# A monotonic clock, injectable for deterministic tests. Monotonic (not wall
# clock) so window/TTL arithmetic is immune to system clock adjustments.
Clock = Callable[[], float]
default_clock: Clock = time.monotonic


# ── Rate limiting ─────────────────────────────────────────────────────────────

# Mutating HTTP methods — the explicit definition of "writes" for the
# ``writes`` rate-limit scope, so the middleware (D) does not guess.
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def should_limit(method: str, scope: str) -> bool:
    """Whether a request of ``method`` is subject to rate limiting under ``scope``.

    ``all`` → every guarded request. ``writes`` → only mutating methods.
    """
    if scope == "all":
        return True
    return method.upper() in WRITE_METHODS


@dataclass(frozen=True)
class RateLimitVerdict:
    """Outcome of a single rate-limit check.

    ``retry_after`` is seconds until the caller's next request would be admitted
    (0 when ``allowed``). Surface it as the HTTP ``Retry-After`` header on 429.
    """

    allowed: bool
    limit: int
    remaining: int
    retry_after: float


class RateLimitStore(ABC):
    """Per-key token-bucket rate limiter."""

    @abstractmethod
    async def check(self, key: str, limit: int, window_seconds: int) -> RateLimitVerdict:
        """Spend one token for ``key`` and report whether it is admitted.

        The bucket holds up to ``limit`` tokens and refills at
        ``limit / window_seconds`` tokens per second — so a key may burst up to
        ``limit`` then sustains ``limit`` per ``window_seconds``. A denied call
        spends nothing.
        """

    @abstractmethod
    async def cleanup(self) -> int:
        """Evict idle (fully-refilled) keys. Returns count removed."""


# ── Idempotency ───────────────────────────────────────────────────────────────


class IdempotencyState(Enum):
    FRESH = "fresh"  # newly claimed — caller should process, then reservation.complete()
    IN_PROGRESS = "in_progress"  # an *active* claim is held by a concurrent request → 409
    REPLAY = "replay"  # completed earlier with the SAME payload → return stored response
    MISMATCH = "mismatch"  # key reused with a DIFFERENT payload → 422


@dataclass(frozen=True)
class IdempotencyRecord:
    payload_digest: str
    status_code: int | None
    response_body: str | None
    created_at: float  # wall-clock epoch seconds, for audit/debug only


@dataclass(frozen=True)
class IdempotencyOutcome:
    state: IdempotencyState
    record: IdempotencyRecord | None = None
    # Set only on FRESH — an opaque token the caller passes back to complete()
    # so a stolen claim's late finisher cannot overwrite the new owner's result.
    claim_token: str | None = None


class Reservation:
    """A live idempotency claim yielded by :meth:`IdempotencyStore.reserve`.

    On ``FRESH`` the caller runs the handler then calls :meth:`complete`. On
    ``REPLAY``/``MISMATCH``/``IN_PROGRESS`` it short-circuits (the body is not
    re-executed). A handler that fails should NOT call :meth:`complete`; the
    claim then self-heals once its short claim-TTL lapses.
    """

    def __init__(self, store: "IdempotencyStore", key: str, outcome: IdempotencyOutcome) -> None:
        self._store = store
        self._key = key
        self.state = outcome.state
        self.record = outcome.record
        self._token = outcome.claim_token

    @property
    def is_fresh(self) -> bool:
        return self.state is IdempotencyState.FRESH

    async def complete(self, status_code: int, response_body: str) -> bool:
        """Record the finished response. No-op (returns False) if the claim was stolen."""
        if self._token is None:
            return False
        return await self._store.complete(self._key, self._token, status_code, response_body)


class IdempotencyStore(ABC):
    """Self-healing execution cache keyed by a client ``Idempotency-Key``.

    Two-phase: :meth:`begin` atomically claims the key (short ``claim_ttl``);
    :meth:`complete` records the response (long ``cache_ttl``). Prefer the
    :meth:`reserve` context manager at call sites.
    """

    @abstractmethod
    async def begin(
        self, key: str, payload_digest: str, claim_ttl: int, cache_ttl: int
    ) -> IdempotencyOutcome:
        """Atomically claim ``key`` for this payload, or report a prior claim.

        A stale in-progress claim (older than ``claim_ttl``) is stolen and the
        outcome is ``FRESH``. A payload-digest mismatch is reported as
        ``MISMATCH`` even while a claim is in progress.
        """

    @abstractmethod
    async def complete(
        self, key: str, claim_token: str, status_code: int, response_body: str
    ) -> bool:
        """Record the response for a claim. Returns False if the token no longer owns the key."""

    @abstractmethod
    async def cleanup(self) -> int:
        """Drop expired claims and replays. Returns count removed."""

    @asynccontextmanager
    async def reserve(
        self, key: str, payload_digest: str, *, claim_ttl: int, cache_ttl: int
    ) -> AsyncIterator[Reservation]:
        outcome = await self.begin(key, payload_digest, claim_ttl, cache_ttl)
        yield Reservation(self, key, outcome)


# ── Config parsing ────────────────────────────────────────────────────────────

_UNIT_SECONDS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hour": 3600,
    "hours": 3600,
}

_DISABLED = {"", "0", "off", "none", "disabled", "false"}


def parse_rate_limit(spec: str | None) -> tuple[int, int] | None:
    """Parse a ``MORI_RATE_LIMIT`` spec like ``"120/min"`` → ``(120, 60)``.

    Returns ``None`` (limiting disabled) for an empty/``0``/``off`` value.
    Raises ``ValueError`` on a malformed non-empty spec so a typo fails loudly
    at startup rather than silently disabling protection.
    """
    raw = (spec or "").strip().lower()
    if raw in _DISABLED:
        return None
    if "/" not in raw:
        raise ValueError(
            f"MORI_RATE_LIMIT={spec!r} is malformed; expected '<count>/<unit>' "
            "e.g. '120/min' (units: sec|min|hour), or '0'/'off' to disable"
        )
    count_s, unit_s = (part.strip() for part in raw.split("/", 1))
    try:
        count = int(count_s)
    except ValueError as exc:
        raise ValueError(f"MORI_RATE_LIMIT count {count_s!r} is not an integer") from exc
    window = _UNIT_SECONDS.get(unit_s)
    if count <= 0:
        return None
    if window is None:
        raise ValueError(f"MORI_RATE_LIMIT unit {unit_s!r} unknown; use sec|min|hour")
    return count, window


def parse_scope(spec: str | None) -> str:
    """Parse ``MORI_RATE_LIMIT_SCOPE`` → ``'writes'`` (default) or ``'all'``.

    ``writes`` limits only mutating methods (see :data:`WRITE_METHODS`) —
    targeting the runaway-autonomous-writer risk #23 names, leaving reads
    unthrottled. ``all`` limits every guarded request.
    """
    raw = (spec or "").strip().lower()
    if raw in ("", "writes", "write"):
        return "writes"
    if raw == "all":
        return "all"
    raise ValueError(f"MORI_RATE_LIMIT_SCOPE={spec!r} unknown; use 'writes' or 'all'")
