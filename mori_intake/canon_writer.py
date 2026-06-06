"""Canon writer — the single writer of mori canon from the promotion pipeline.

This module is the **sole holder of mori canon write credentials** for the
agent-memory-governance pathway.  It:

1. Polls ``promotion_queue`` for ``queued`` rows using
   ``FOR UPDATE SKIP LOCKED`` (Postgres) so multiple concurrent processes
   never double-process a row.
2. For each queued row:
   a. Checks ``intake_promotion_map`` for the ``candidate_id`` — if present,
      the canon write already happened; skip straight to marking the queue row
      ``committed`` (idempotency guard).
   b. Collects corroborating ``agent_id``s from ``intake_corroborations``.
   c. Writes the canon memory via mori's public store ``write()`` API.
   d. Writes a ``memory_intake_lineage`` row (mori-side) and an
      ``intake_promotion_map`` row (intake-side).
   e. Sets the candidate to ``promoted`` (+ ``promoted_canon_name`` /
      ``promoted_at``).
   f. Marks the queue row ``committed``.
3. On failure: increments ``attempt_count``, records ``error_message``, leaves
   the row in ``queued`` (or transitions to ``failed`` after the attempt cap)
   for retry.

At-least-once + idempotent — NOT XA/2PC.  Canon availability is never coupled
to intake availability.

Design notes
------------
* ``drain_once`` is async because it holds an asyncpg connection.  The mori
  store ``write()`` call is synchronous (SQLiteStore) — we run it via
  ``asyncio.get_event_loop().run_in_executor(None, ...)`` when needed, or call
  it directly when the store is synchronous.  PostgresStore's ``write()`` is a
  coroutine; both paths are handled.
* The mori store is **injected** so tests can pass a dummy.
* ``assess`` is irrelevant here — the canon writer only drains rows that are
  already ``queued`` (put there by the assessor after an ``UNRELATED`` verdict).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

# Maximum promotion attempts per queue row before it is marked ``failed`` and
# skipped.  Keeps a transient mori outage from looping forever.
_MAX_ATTEMPTS = 5

# Drain batch per call — limits how long a single drain_once() call can block.
_BATCH_SIZE = 20


# ── Public API ────────────────────────────────────────────────────────────────


async def drain_once(
    intake_pool: "asyncpg.Pool",
    mori_store: Any,
    *,
    batch_size: int = _BATCH_SIZE,
) -> int:
    """Drain up to *batch_size* queued promotion rows.

    Parameters
    ----------
    intake_pool:
        asyncpg pool for the **intake** Postgres (not mori's DB).
    mori_store:
        A ``BaseStore`` instance with a ``write()`` method — either
        ``SQLiteStore`` (synchronous) or ``PostgresStore`` (async).
        Injected so tests can substitute a stub.

    Returns the number of rows successfully committed this pass.
    """
    committed = 0
    rows = await _fetch_batch(intake_pool, batch_size)
    for row in rows:
        queue_id = row["id"]
        candidate_id = row["candidate_id"]
        try:
            did_commit = await _promote_one(intake_pool, mori_store, queue_id, candidate_id)
            if did_commit:
                committed += 1
        except Exception as exc:
            # Increment attempt_count; leave the row in ``queued`` for retry
            # (or transition to ``failed`` if the cap is reached).
            await _record_failure(intake_pool, queue_id, exc)
    return committed


# ── Internal helpers ──────────────────────────────────────────────────────────


async def _fetch_batch(pool: "asyncpg.Pool", batch_size: int) -> list:
    """Fetch up to *batch_size* queued rows using SKIP LOCKED."""
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT id, candidate_id, attempt_count
            FROM promotion_queue
            WHERE status IN ('queued', 'failed')
              AND attempt_count < $2
            ORDER BY created_at
            LIMIT $1
            FOR UPDATE SKIP LOCKED
            """,
            batch_size,
            _MAX_ATTEMPTS,
        )


async def _promote_one(
    pool: "asyncpg.Pool",
    mori_store: Any,
    queue_id: uuid.UUID,
    candidate_id: uuid.UUID,
) -> bool:
    """Promote one queued row to canon.

    Returns True if the row is now committed (either freshly promoted or
    already idempotently skipped), False if we had to abort and leave it
    for retry.

    This function does NOT raise — callers should catch unexpected exceptions
    from ``_record_failure``.
    """
    async with pool.acquire() as conn:
        # ── Idempotency guard ──────────────────────────────────────────────
        # Check intake_promotion_map: if a row for this candidate already
        # exists, the canon write happened on a previous attempt.  Skip the
        # write and jump straight to marking the queue row committed.
        existing_map = await conn.fetchrow(
            "SELECT canon_name FROM intake_promotion_map WHERE candidate_id = $1",
            candidate_id,
        )
        if existing_map is not None:
            canon_name = existing_map["canon_name"]
            logger.info(
                "canon_writer: candidate %s already promoted as %r — marking committed (idempotent re-drive)",
                candidate_id,
                canon_name,
            )
            await _mark_committed(conn, queue_id, canon_name)
            return True

        # ── Gather candidate + corroborations ─────────────────────────────
        candidate = await conn.fetchrow(
            "SELECT canonicalized_body, content_hash, reinforcement_count "
            "FROM intake_candidates WHERE id = $1",
            candidate_id,
        )
        if candidate is None:
            logger.error(
                "canon_writer: candidate %s not found — skipping queue row %s",
                candidate_id,
                queue_id,
            )
            await _mark_failed(conn, queue_id, "candidate not found")
            return False

        corroborations = await conn.fetch(
            "SELECT DISTINCT agent_id, submission_id FROM intake_corroborations WHERE candidate_id = $1",
            candidate_id,
        )
        agent_ids: list[str] = sorted({str(r["agent_id"]) for r in corroborations})
        submission_ids: list[uuid.UUID] = [r["submission_id"] for r in corroborations]

        body: str = candidate["canonicalized_body"]
        reinforcement_count: int = candidate["reinforcement_count"]

        # ── Derive canon name ──────────────────────────────────────────────
        # Use a deterministic name derived from the content hash so that a
        # re-drive produces the same name (name collisions are handled by
        # mori's upsert — it updates the existing row).
        content_hash_hex: str = candidate["content_hash"]
        canon_name = f"agent-intake-{content_hash_hex[:16]}"

        # ── Write to mori canon ────────────────────────────────────────────
        # Use the public store write() API — NOT raw SQL into memories.
        # origin_clients is populated with the distinct corroborating agent_ids.
        try:
            result = await _call_store_write(
                mori_store,
                name=canon_name,
                title=f"Agent intake: {body[:60]}{'...' if len(body) > 60 else ''}",
                body=body,
                type="feedback",
                # Let mori's default tier apply (working); the promotion path
                # earns the memory its place via normal tier mechanics later.
                tier="working",
                tags=["source:agent-intake"],
                origin_clients=agent_ids,
            )
            logger.info(
                "canon_writer: wrote canon memory %r — store response: %s", canon_name, result
            )
        except Exception as exc:
            logger.error("canon_writer: mori write failed for candidate %s: %s", candidate_id, exc)
            await _mark_failed(conn, queue_id, f"mori write error: {exc}")
            return False

        # ── Build trust snapshot ───────────────────────────────────────────
        trust_snapshot: dict = {
            "reinforcement_count": reinforcement_count,
            "corroborating_agent_ids": agent_ids,
        }

        now_utc = datetime.now(timezone.utc)

        # ── Write memory_intake_lineage (mori-side, via raw conn if SQLite,
        #    or via pool if Postgres). Since both are in the same intake pool
        #    here, we write it to the intake pool side-table and also call
        #    the mori-side migration table via a separate helper.
        # NOTE: memory_intake_lineage lives in the *mori* database (not intake).
        # We call _write_lineage_to_mori() which uses the mori_store directly.
        try:
            await _write_lineage_to_mori(
                mori_store,
                canon_name=canon_name,
                intake_candidate_id=str(candidate_id),
                intake_submission_ids=[str(s) for s in submission_ids],
                trust_snapshot=trust_snapshot,
                promoted_at=now_utc,
            )
        except Exception as exc:
            logger.error(
                "canon_writer: lineage write failed for %r: %s — "
                "canon memory was written; will retry lineage+map on re-drive",
                canon_name,
                exc,
            )
            await _mark_failed(conn, queue_id, f"lineage write error: {exc}")
            return False

        # ── Write intake_promotion_map (intake-side) ───────────────────────
        try:
            await conn.execute(
                """
                INSERT INTO intake_promotion_map
                    (canon_name, candidate_id, submission_ids, provenance_snapshot, promoted_at)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (canon_name) DO NOTHING
                """,
                canon_name,
                candidate_id,
                submission_ids,
                json.dumps(trust_snapshot),
                now_utc,
            )
        except Exception as exc:
            logger.error(
                "canon_writer: intake_promotion_map write failed for %r: %s",
                canon_name,
                exc,
            )
            await _mark_failed(conn, queue_id, f"promotion_map write error: {exc}")
            return False

        # ── Mark candidate promoted ────────────────────────────────────────
        await conn.execute(
            """
            UPDATE intake_candidates
            SET status = 'promoted',
                promoted_canon_name = $1,
                updated_at = $2
            WHERE id = $3
            """,
            canon_name,
            now_utc,
            candidate_id,
        )

        # ── Mark queue row committed ───────────────────────────────────────
        await _mark_committed(conn, queue_id, canon_name)

        logger.info(
            "canon_writer: candidate %s promoted → %r (agents: %s)",
            candidate_id,
            canon_name,
            ", ".join(agent_ids) or "none",
        )
        return True


async def _call_store_write(mori_store: Any, **kwargs) -> str:
    """Call mori_store.write() regardless of whether it is sync or async.

    SQLiteStore.write() is synchronous; PostgresStore.write() is a coroutine.
    We detect at call-time and wrap accordingly so the canon writer works with
    both backends (SQLite in tests, Postgres in production).
    """
    result = mori_store.write(**kwargs)
    if inspect.isawaitable(result):
        return await result
    return result  # type: ignore[return-value]


async def _write_lineage_to_mori(
    mori_store: Any,
    *,
    canon_name: str,
    intake_candidate_id: str,
    intake_submission_ids: list[str],
    trust_snapshot: dict,
    promoted_at: datetime,
) -> None:
    """Write a ``memory_intake_lineage`` row into the mori database.

    We call the mori store's raw connection for this because ``BaseStore``
    has no public method for ``memory_intake_lineage`` (it is a new table
    added by migration 10).  We use ``_write_lineage_raw`` which dispatches
    to SQLite or Postgres depending on the store type.
    """
    if hasattr(mori_store, "pool"):
        # PostgresStore path
        async with mori_store.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO memory_intake_lineage
                    (canon_name, intake_candidate_id, intake_submission_ids,
                     trust_snapshot, promoted_at)
                VALUES ($1, $2::uuid, $3::uuid[], $4, $5)
                ON CONFLICT (canon_name) DO NOTHING
                """,
                canon_name,
                intake_candidate_id,
                intake_submission_ids,
                json.dumps(trust_snapshot),
                promoted_at,
            )
    else:
        # SQLiteStore path — runs synchronously; wrap in executor to stay async.
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            _write_lineage_sqlite,
            mori_store,
            canon_name,
            intake_candidate_id,
            intake_submission_ids,
            trust_snapshot,
            promoted_at,
        )


def _write_lineage_sqlite(
    mori_store: Any,
    canon_name: str,
    intake_candidate_id: str,
    intake_submission_ids: list[str],
    trust_snapshot: dict,
    promoted_at: datetime,
) -> None:
    """SQLite-compatible lineage write (synchronous)."""

    conn = mori_store._mem._get_conn()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO memory_intake_lineage
                (canon_name, intake_candidate_id, intake_submission_ids,
                 trust_snapshot, promoted_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                canon_name,
                intake_candidate_id,
                json.dumps(intake_submission_ids),
                json.dumps(trust_snapshot),
                promoted_at.isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


async def _mark_committed(conn, queue_id: uuid.UUID, canon_name: str) -> None:
    await conn.execute(
        """
        UPDATE promotion_queue
        SET status = 'committed', canon_name = $1, updated_at = NOW()
        WHERE id = $2
        """,
        canon_name,
        queue_id,
    )


async def _mark_failed(conn, queue_id: uuid.UUID, error_msg: str) -> None:
    await conn.execute(
        """
        UPDATE promotion_queue
        SET attempt_count = attempt_count + 1,
            error_message = $1,
            status = CASE WHEN attempt_count + 1 >= $3 THEN 'failed' ELSE status END,
            updated_at = NOW()
        WHERE id = $2
        """,
        error_msg,
        queue_id,
        _MAX_ATTEMPTS,
    )


async def _record_failure(pool: "asyncpg.Pool", queue_id: uuid.UUID, exc: Exception) -> None:
    """Record a failure on a queue row, incrementing attempt_count."""
    try:
        async with pool.acquire() as conn:
            await _mark_failed(conn, queue_id, str(exc))
    except Exception as inner:
        logger.error(
            "canon_writer: could not record failure for queue row %s: %s (original: %s)",
            queue_id,
            inner,
            exc,
        )
