"""Simple in-memory per-API-key sliding-window rate limiter for mori-intake.

Applied ONLY to ``POST /intake/submissions`` (the write path).  Keyed on the
authenticated key name (never the raw secret — that is resolved by auth before
the limiter is consulted).

Algorithm: fixed-window token bucket.  Each key gets a bucket of ``limit``
tokens that refreshes every ``window_seconds`` seconds (a sliding window would
need a deque per key; a fixed window is O(1) and good enough for the guardrail
use-case here).

Configuration
-------------
``MORI_INTAKE_RATE_LIMIT_PER_MIN``
    Maximum submissions per minute per API-key name (default 120).  Set to 0
    to disable the limiter entirely.

When the bucket is exhausted the endpoint returns::

    HTTP 429
    Retry-After: <seconds until the next window>
    {"status": "rate_limited", "retry_after": <seconds>}

Thread-safety
-------------
The store is a plain dict protected by a threading.Lock so it is safe to call
from a synchronous context (FastAPI ``async def`` handlers run in an async
event loop, but the lock is acquired and released without a yield, so there is
no risk of async-context switching while the lock is held).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

_DEFAULT_RATE_LIMIT_PER_MIN = 120
_WINDOW_SECONDS = 60


def _read_rate_limit() -> int:
    """Read ``MORI_INTAKE_RATE_LIMIT_PER_MIN`` from the environment."""
    raw = os.environ.get("MORI_INTAKE_RATE_LIMIT_PER_MIN", "").strip()
    if not raw:
        return _DEFAULT_RATE_LIMIT_PER_MIN
    try:
        val = int(raw)
    except ValueError:
        logger.warning(
            "MORI_INTAKE_RATE_LIMIT_PER_MIN=%r is not an integer — using default %d",
            raw,
            _DEFAULT_RATE_LIMIT_PER_MIN,
        )
        return _DEFAULT_RATE_LIMIT_PER_MIN
    if val <= 0:
        logger.info("MORI_INTAKE_RATE_LIMIT_PER_MIN=0 — rate limiter disabled")
        return 0
    return val


# ── Data types ────────────────────────────────────────────────────────────────


@dataclass
class _Bucket:
    """A single key's token-bucket state."""

    tokens: int  # remaining tokens in the current window
    window_start: float  # monotonic time when the current window opened


@dataclass(frozen=True)
class RateLimitVerdict:
    """Result of a single rate-limit check."""

    allowed: bool
    retry_after: int  # seconds until the next window (0 when allowed)


# ── Limiter ───────────────────────────────────────────────────────────────────


class IntakeRateLimiter:
    """In-memory, thread-safe, per-key sliding fixed-window rate limiter.

    Parameters
    ----------
    limit_per_min:
        Maximum allowed requests per ``window_seconds`` per key.
        ``0`` disables the limiter (all calls allowed).
    window_seconds:
        Length of a rate-limit window in seconds.
    _clock:
        Monotonic clock injectable for deterministic tests.
    """

    def __init__(
        self,
        limit_per_min: int | None = None,
        window_seconds: int = _WINDOW_SECONDS,
        _clock=time.monotonic,
    ) -> None:
        self._limit = limit_per_min if limit_per_min is not None else _read_rate_limit()
        self._window = window_seconds
        self._clock = _clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    # ── Public API ─────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._limit > 0

    def check(self, key: str) -> RateLimitVerdict:
        """Spend one token for *key* and return whether the request is allowed.

        Denied requests are NOT counted (the token is not spent).  This matches
        the spec behaviour: a single 429 does not further erode the bucket.
        """
        if not self.enabled:
            return RateLimitVerdict(allowed=True, retry_after=0)

        now = self._clock()

        with self._lock:
            bucket = self._buckets.get(key)

            if bucket is None or now - bucket.window_start >= self._window:
                # Fresh window — full bucket.
                self._buckets[key] = _Bucket(
                    tokens=self._limit - 1,
                    window_start=now,
                )
                return RateLimitVerdict(allowed=True, retry_after=0)

            if bucket.tokens > 0:
                bucket.tokens -= 1
                return RateLimitVerdict(allowed=True, retry_after=0)

            # Bucket exhausted — compute seconds until the window resets.
            elapsed = now - bucket.window_start
            retry_after = max(1, int(self._window - elapsed) + 1)
            return RateLimitVerdict(allowed=False, retry_after=retry_after)


# ── Module-level singleton ────────────────────────────────────────────────────

# Instantiated once at import time so the same bucket dict is shared across
# all requests.  Tests can replace this with a fresh IntakeRateLimiter instance
# via dependency injection or monkeypatching.
_limiter: IntakeRateLimiter | None = None


def get_limiter() -> IntakeRateLimiter:
    """Return the module-level limiter, constructing it on first call."""
    global _limiter
    if _limiter is None:
        _limiter = IntakeRateLimiter()
    return _limiter


def reset_limiter(new_limiter: IntakeRateLimiter | None = None) -> None:
    """Replace the module-level limiter (test seam — not for production use)."""
    global _limiter
    _limiter = new_limiter
