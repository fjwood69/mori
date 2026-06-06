"""Async drain worker — Step 1 intra-pile dedup (Slice 1).

A single asyncio task started on app startup (FastAPI lifespan) and stopped on
shutdown.  It polls ``intake_submissions`` for rows that have not yet been
corroborated, hashes and canonicalises the body, then upserts into
``intake_candidates`` and writes a corroboration row.

The corroboration row is the drain marker: the LEFT JOIN drain query excludes
any submission that already has a corroboration, so each row is processed
exactly once under normal operation and retried on the next pass if the
transaction rolls back.

Embeddings are NOT computed in Slice 1.  The ``EMBEDDINGS_ENABLED`` flag is
reserved for the next slice.

Poison-row guard
----------------
A per-row error counter (in-memory dict) caps retries at ``_MAX_ATTEMPTS``.
After the cap, the row is logged and skipped indefinitely for the lifetime of
this process.  This prevents a single malformed row from hot-looping the drain.
The counter resets on process restart — intentional: a deploy may fix the bug.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from mori_intake.normalize import canonical_body, content_hash

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

# Maximum per-row processing attempts before we log-and-skip for this process
# lifetime.  Keeps a poison row from hot-looping the drain loop.
_MAX_ATTEMPTS = 5

# Drain batch size — number of unprocessed submissions fetched per tick.
_BATCH_SIZE = 50

# In-memory attempt counter.  key = submission UUID (str), value = attempt count.
_attempt_counts: dict[str, int] = {}

# ── Drain query ───────────────────────────────────────────────────────────────

# Fetch submissions that have NO corroboration row yet, ordered by received_at
# so the oldest items drain first (FIFO within the batch).
#
# ``s.purged_at IS NULL`` excludes tombstoned submissions: when the TTL purge
# (P3) reaps a stale pending candidate it cascade-deletes that candidate's
# corroborations, which would otherwise make the originating submissions look
# uncorroborated again and resurrect them here.  Tombstoning closes that loop.
_DRAIN_QUERY = """
SELECT s.id, s.raw_source_text, s.agent_id
FROM intake_submissions s
LEFT JOIN intake_corroborations c ON c.submission_id = s.id
WHERE c.id IS NULL AND s.purged_at IS NULL
ORDER BY s.received_at
LIMIT $1
"""


# ── Public interface ──────────────────────────────────────────────────────────


async def drain_once(pool: "asyncpg.Pool") -> int:
    """Run one drain pass.

    Processes up to ``_BATCH_SIZE`` uncorroborated submissions.  Each is
    handled in its own transaction so a failure on one row does not roll
    back the others.

    Returns the number of submissions successfully processed this pass.

    Exposed as a standalone coroutine so tests can drive it directly without
    waiting on the poll loop (no real sleeps in tests).
    """
    rows = await pool.fetch(_DRAIN_QUERY, _BATCH_SIZE)
    processed = 0
    for row in rows:
        sid = str(row["id"])
        if _attempt_counts.get(sid, 0) >= _MAX_ATTEMPTS:
            # Poison row — already logged at the cap; skip silently.
            continue
        try:
            await _process_one(pool, row)
            processed += 1
            # Success: clear error counter to prevent unbounded growth (QUAL-001).
            _attempt_counts.pop(sid, None)
        except Exception as exc:
            _attempt_counts[sid] = _attempt_counts.get(sid, 0) + 1
            count = _attempt_counts[sid]
            if count >= _MAX_ATTEMPTS:
                logger.error(
                    "intake worker: submission %s failed %d times — skipping for this process. "
                    "Last error: %s",
                    sid,
                    count,
                    exc,
                )
            else:
                logger.warning(
                    "intake worker: submission %s failed (attempt %d/%d): %s",
                    sid,
                    count,
                    _MAX_ATTEMPTS,
                    exc,
                )
    return processed


async def purge_stale_pending(pool: "asyncpg.Pool", ttl_hours: float) -> int:
    """Tombstone-and-reap pending candidates idle longer than ``ttl_hours`` (P3).

    Staleness is measured from ``updated_at`` — the last reinforcement — so a
    candidate still accumulating corroborations is never reaped while active.
    ``ttl_hours <= 0`` disables the purge (returns 0).

    Only ``pending`` candidates are touched; ``promoted`` / ``rejected`` are
    terminal audit rows and never expire here.

    Resurrection guard
    ------------------
    Deleting a pending candidate cascade-deletes its corroborations, which would
    make the originating submissions look uncorroborated and let the drain worker
    re-create the candidate.  So before deleting we **tombstone** those
    submissions (``purged_at = NOW()``); the drain query excludes tombstoned
    rows.  We tombstone rather than DELETE to preserve the agent-write audit
    trail and the ``UNIQUE(session_id, stable_key)`` idempotency guard.

    Cardinality guard
    -----------------
    A submission is tombstoned only if *every* candidate it corroborates is in
    the stale set.  A submission that also corroborates a non-stale candidate is
    left live — protects shared evidence should dedup ever become 1:N.

    The whole purge runs in one transaction; stale candidates are locked with
    ``FOR UPDATE SKIP LOCKED`` in id order so a concurrent drain pass cannot
    deadlock against it.
    """
    if ttl_hours is None or ttl_hours <= 0:
        return 0

    async with pool.acquire() as conn:
        async with conn.transaction():
            stale = await conn.fetch(
                "SELECT id FROM intake_candidates "
                "WHERE status = 'pending' "
                "  AND updated_at < NOW() - ($1::double precision * INTERVAL '1 hour') "
                "ORDER BY id "
                "FOR UPDATE SKIP LOCKED",
                float(ttl_hours),
            )
            if not stale:
                return 0
            stale_ids = [r["id"] for r in stale]

            # Tombstone submissions that corroborate ONLY stale candidates.
            await conn.execute(
                "UPDATE intake_submissions SET purged_at = NOW() "
                "WHERE purged_at IS NULL "
                "  AND id IN ("
                "      SELECT DISTINCT submission_id FROM intake_corroborations "
                "      WHERE candidate_id = ANY($1::uuid[])"
                "  ) "
                "  AND id NOT IN ("
                "      SELECT submission_id FROM intake_corroborations "
                "      WHERE candidate_id <> ALL($1::uuid[])"
                "  )",
                stale_ids,
            )

            # Delete the stale candidates; corroborations cascade-delete.
            await conn.execute(
                "DELETE FROM intake_candidates WHERE id = ANY($1::uuid[])",
                stale_ids,
            )

    logger.info(
        "intake worker: TTL purge reaped %d stale pending candidate(s) (idle > %.0fh)",
        len(stale_ids),
        ttl_hours,
    )
    return len(stale_ids)


async def run_loop(
    pool: "asyncpg.Pool",
    interval: float,
    *,
    ttl_hours: float = 168.0,
    purge_interval: float = 3600.0,
) -> None:
    """Continuously drain submissions at the configured poll interval.

    Errors within ``drain_once`` are caught and logged; the loop continues so
    that one bad batch never stalls the service.  The task exits cleanly on
    ``asyncio.CancelledError``.

    Every ``purge_interval`` seconds (not every drain tick) the loop also runs
    :func:`purge_stale_pending` to reap pending candidates idle beyond
    ``ttl_hours`` (P3).  ``ttl_hours <= 0`` disables the purge.
    """
    logger.info(
        "intake drain worker started (interval=%.1fs, ttl=%.0fh, purge_every=%.0fs)",
        interval,
        ttl_hours,
        purge_interval,
    )
    last_purge = time.monotonic()
    try:
        while True:
            try:
                n = await drain_once(pool)
                if n:
                    logger.debug("intake worker: drained %d submission(s)", n)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("intake worker: drain pass failed: %s", exc)

            if ttl_hours > 0 and (time.monotonic() - last_purge) >= purge_interval:
                last_purge = time.monotonic()
                try:
                    await purge_stale_pending(pool, ttl_hours)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error("intake worker: TTL purge failed: %s", exc)

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("intake drain worker stopped")


# ── Internal ──────────────────────────────────────────────────────────────────


async def _process_one(pool: "asyncpg.Pool", row) -> None:
    """Process a single submission inside one transaction.

    Steps (per spec):
    1. Hash and canonicalise the raw source text.
    2. Look up existing candidate by content_hash.
       - Hit  → increment reinforcement_count, use that candidate id.
       - Miss → insert a new pending candidate.
    3. Insert a corroboration row (ON CONFLICT DO NOTHING for safety).
    """
    sid = row["id"]
    raw_text: str = row["raw_source_text"]
    agent_id: str = row["agent_id"]

    h = content_hash(raw_text)
    body = canonical_body(raw_text)

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Step 2 — candidate lookup.
            existing = await conn.fetchrow(
                "SELECT id FROM intake_candidates WHERE content_hash = $1",
                h,
            )

            if existing is not None:
                # Hit — reinforce the existing candidate.
                candidate_id = existing["id"]
                await conn.execute(
                    "UPDATE intake_candidates "
                    "SET reinforcement_count = reinforcement_count + 1, updated_at = NOW() "
                    "WHERE id = $1",
                    candidate_id,
                )
            else:
                # Miss — insert a new pending candidate.
                candidate_id = await conn.fetchval(
                    "INSERT INTO intake_candidates "
                    "  (canonicalized_body, content_hash, status) "
                    "VALUES ($1, $2, 'pending') "
                    "RETURNING id",
                    body,
                    h,
                )

            # Step 3 — record corroboration + mark submission drained.
            await conn.execute(
                "INSERT INTO intake_corroborations "
                "  (candidate_id, submission_id, agent_id, source_weight) "
                "VALUES ($1, $2, $3, 1.0) "
                "ON CONFLICT (candidate_id, submission_id) DO NOTHING",
                candidate_id,
                sid,
                agent_id,
            )
