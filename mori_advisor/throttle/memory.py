"""In-memory throttle adapters — the default, single-instance backing store.

Correct for a single-process (solo / small-team) deployment. State lives in the
process and is lost on restart, which is acceptable: rate-limit buckets refill
quickly, and a dropped idempotency cache merely means a replay after a restart is
re-processed rather than de-duplicated.

⚠ Single-instance ONLY. With more than one worker each holds its own counters,
so the effective rate limit becomes ``N × limit`` and a duplicate POST routed to
a different worker can slip through. Switch ``MORI_THROTTLE_STORE=postgres``
(the shared adapter, lands with #23 C/D) before scaling out — and see
``throttle_safety_warning()``.
"""

from __future__ import annotations

import asyncio
import time

from .base import (
    Clock,
    IdempotencyOutcome,
    IdempotencyRecord,
    IdempotencyState,
    IdempotencyStore,
    RateLimitStore,
    RateLimitVerdict,
    default_clock,
)


class InMemoryRateLimitStore(RateLimitStore):
    """Token-bucket rate limiter — O(1) state per key, idle keys evicted."""

    class _Bucket:
        __slots__ = ("tokens", "last", "cap", "rate")

        def __init__(self, tokens: float, last: float, cap: float, rate: float) -> None:
            self.tokens = tokens
            self.last = last
            self.cap = cap  # capacity == limit (last seen)
            self.rate = rate  # tokens per second == limit / window

    def __init__(self, clock: Clock = default_clock) -> None:
        self._clock = clock
        self._buckets: dict[str, InMemoryRateLimitStore._Bucket] = {}
        self._lock = asyncio.Lock()

    async def check(self, key: str, limit: int, window_seconds: int) -> RateLimitVerdict:
        now = self._clock()
        rate = limit / window_seconds
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = self._Bucket(float(limit), now, float(limit), rate)
                self._buckets[key] = bucket
            else:
                # Refill for elapsed time; track the latest config (cap/rate).
                bucket.cap = float(limit)
                bucket.rate = rate
                elapsed = now - bucket.last
                if elapsed > 0:
                    bucket.tokens = min(bucket.cap, bucket.tokens + elapsed * rate)
                    bucket.last = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return RateLimitVerdict(
                    allowed=True, limit=limit, remaining=int(bucket.tokens), retry_after=0.0
                )
            retry_after = (1.0 - bucket.tokens) / rate
            return RateLimitVerdict(
                allowed=False, limit=limit, remaining=0, retry_after=retry_after
            )

    async def cleanup(self) -> int:
        """Evict buckets that would be fully refilled by now (idle). Returns count removed."""
        now = self._clock()
        async with self._lock:
            idle = []
            for key, b in self._buckets.items():
                projected = min(b.cap, b.tokens + max(0.0, now - b.last) * b.rate)
                if projected >= b.cap:
                    idle.append(key)
            for key in idle:
                del self._buckets[key]
            return len(idle)


class InMemoryIdempotencyStore(IdempotencyStore):
    """Self-healing two-phase idempotency cache (claim-steal + claim-token guard)."""

    class _Entry:
        __slots__ = (
            "payload_digest",
            "status_code",
            "response_body",
            "created_at",
            "claim_token",
            "claim_expires_at",
            "cache_ttl",
            "cache_expires_at",
            "complete",
        )

        def __init__(
            self,
            payload_digest: str,
            created_at: float,
            claim_token: str,
            claim_expires_at: float,
            cache_ttl: int,
        ) -> None:
            self.payload_digest = payload_digest
            self.status_code: int | None = None
            self.response_body: str | None = None
            self.created_at = created_at
            self.claim_token = claim_token
            self.claim_expires_at = claim_expires_at
            self.cache_ttl = cache_ttl
            self.cache_expires_at = 0.0  # set on complete()
            self.complete = False

    def __init__(self, clock: Clock = default_clock) -> None:
        self._clock = clock
        self._entries: dict[str, InMemoryIdempotencyStore._Entry] = {}
        self._lock = asyncio.Lock()
        self._counter = 0

    async def begin(
        self, key: str, payload_digest: str, claim_ttl: int, cache_ttl: int
    ) -> IdempotencyOutcome:
        now = self._clock()
        async with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                if entry.complete and entry.cache_expires_at <= now:
                    del self._entries[key]  # replay window elapsed
                    entry = None
                elif not entry.complete and entry.claim_expires_at <= now:
                    del self._entries[key]  # stale claim — steal it
                    entry = None
            if entry is not None:
                if entry.payload_digest != payload_digest:
                    return IdempotencyOutcome(IdempotencyState.MISMATCH, self._snapshot(entry))
                if entry.complete:
                    return IdempotencyOutcome(IdempotencyState.REPLAY, self._snapshot(entry))
                return IdempotencyOutcome(IdempotencyState.IN_PROGRESS, self._snapshot(entry))
            self._counter += 1
            token = str(self._counter)
            self._entries[key] = self._Entry(
                payload_digest,
                created_at=time.time(),
                claim_token=token,
                claim_expires_at=now + claim_ttl,
                cache_ttl=cache_ttl,
            )
            return IdempotencyOutcome(IdempotencyState.FRESH, claim_token=token)

    async def complete(
        self, key: str, claim_token: str, status_code: int, response_body: str
    ) -> bool:
        now = self._clock()
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.claim_token != claim_token:
                return False  # expired, swept, or claim was stolen by another caller
            entry.status_code = status_code
            entry.response_body = response_body
            entry.complete = True
            entry.cache_expires_at = now + entry.cache_ttl
            return True

    async def cleanup(self) -> int:
        now = self._clock()
        async with self._lock:
            drop = [
                k
                for k, e in self._entries.items()
                if (e.complete and e.cache_expires_at <= now)
                or (not e.complete and e.claim_expires_at <= now)
            ]
            for k in drop:
                del self._entries[k]
            return len(drop)

    @staticmethod
    def _snapshot(entry: "InMemoryIdempotencyStore._Entry") -> IdempotencyRecord:
        return IdempotencyRecord(
            payload_digest=entry.payload_digest,
            status_code=entry.status_code,
            response_body=entry.response_body,
            created_at=entry.created_at,
        )
