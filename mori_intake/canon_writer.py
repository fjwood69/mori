"""Canon writer — the single writer of mori canon from the promotion pipeline.

This module is the **sole holder of mori canon write credentials** for the
agent-memory-governance pathway.  It:

1. Polls ``promotion_queue`` for ``queued`` (or stale-``processing``) rows
   using ``FOR UPDATE SKIP LOCKED`` inside a transaction so multiple concurrent
   processes never double-process a row.
2. Immediately sets ``status='processing'`` as a **lease** (same transaction,
   committed before any cross-store work begins).  Other drain workers skip
   ``processing`` rows unless the lease has expired (``updated_at`` older than
   ``_LEASE_SECONDS``).
3. For each leased row:
   a. Checks ``intake_promotion_map`` for the ``candidate_id`` — if present,
      the canon write already happened; skip straight to marking the queue row
      ``committed`` (idempotency guard).
   b. Collects corroborating ``agent_id``s from ``intake_corroborations``.
   c. Writes the canon memory via mori's public store ``write()`` API.
   d. Writes a ``memory_intake_lineage`` row via the mori store's public
      ``record_intake_lineage()`` method (Fix 4 — no ``_get_conn()``).
   e. In ONE intake transaction: inserts ``intake_promotion_map``, sets the
      candidate to ``promoted``, marks the queue row ``committed``.
4. On failure: increments ``attempt_count``, records ``error_message``, leaves
   the row in ``queued``/``failed`` for retry.  The lease is still picked up
   by the stale-lease reclaim after ``_LEASE_SECONDS``.

At-least-once + idempotent — NOT XA/2PC.  Canon availability is never coupled
to intake availability.

Idempotency under crash
-----------------------
The ``intake_promotion_map`` idempotency guard is checked INSIDE the leased
connection before any canon write.  A crash AFTER the canon write but BEFORE
the final intake commit leaves the row in ``processing`` (lease expires) and
the ``intake_promotion_map`` row absent.  On reclaim:

* Canon write: ``write()`` is an upsert — re-writing the same name + body is
  a no-op at the mori level.
* Lineage write: ``record_intake_lineage`` uses
  ``INSERT … ON CONFLICT DO NOTHING`` — a second call is a no-op.
* ``intake_promotion_map`` insert: ``ON CONFLICT DO NOTHING`` — a second
  call after the map row was committed on a prior attempt is a no-op.

Design notes
------------
* ``drain_once`` is async because it holds asyncpg connections.  The mori
  store ``write()`` / ``record_intake_lineage()`` calls may be synchronous
  (SQLiteStore) — we detect at call-time and wrap accordingly.
* The mori store is **injected** so tests can pass a dummy.
* The canon writer has NO reference to a read-capable search path; its only
  store interactions are ``write()`` and ``record_intake_lineage()`` (both
  write-side, but injected to allow test substitution).
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg

from mori_intake.eligibility import evaluate as eligibility_evaluate

logger = logging.getLogger(__name__)

# Maximum promotion attempts per queue row before it is marked ``failed`` and
# skipped.  Keeps a transient mori outage from looping forever.
_MAX_ATTEMPTS = 5

# Drain batch per call — limits how long a single drain_once() call can block.
_BATCH_SIZE = 20

# Seconds after which a ``processing`` row is considered a stale lease and
# eligible for re-claim by any drain worker.
#
# MVV runs a SINGLE drainer — the lease window must exceed worst-case
# assess + canon-write latency (the hard latency ceiling for one pass).
# Multi-worker worker-id + heartbeat locking is Slice-3; at that point
# the lease duration should be re-evaluated against observed p99 latencies.
_LEASE_SECONDS: int = int(os.environ.get("MORI_INTAKE_LEASE_SECONDS", "300"))


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
        A ``BaseStore`` instance with ``write()`` and
        ``record_intake_lineage()`` methods — either ``SQLiteStore``
        (synchronous) or ``PostgresStore`` (async).  Injected so tests can
        substitute a stub.

    Returns the number of rows successfully committed this pass.
    """
    committed = 0
    rows = await _fetch_and_lease_batch(intake_pool, batch_size)
    for row in rows:
        queue_id = row["id"]
        candidate_id = row["candidate_id"]
        try:
            did_commit = await _promote_one(intake_pool, mori_store, queue_id, candidate_id)
            if did_commit:
                committed += 1
        except Exception as exc:
            # Increment attempt_count; leave the row for retry.
            await _record_failure(intake_pool, queue_id, exc)
    return committed


# ── Internal helpers ──────────────────────────────────────────────────────────


async def _fetch_and_lease_batch(pool: "asyncpg.Pool", batch_size: int) -> list:
    """Fetch up to *batch_size* rows, lease each one atomically.

    Transaction structure
    ---------------------
    BEGIN (implicit via asyncpg transaction context):
      SELECT ... FOR UPDATE SKIP LOCKED   -- grab advisory row locks
      UPDATE ... SET status='processing'  -- write the lease
    COMMIT

    After commit, other drain workers see ``status='processing'`` and skip
    these rows until the lease expires.  The stale-lease reclaim clause::

        OR (status = 'processing' AND updated_at < NOW() - INTERVAL '5 min')

    re-surfaces rows whose drainer crashed before committing the final state.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                SELECT id, candidate_id, attempt_count
                FROM promotion_queue
                WHERE (
                    status IN ('queued', 'failed')
                    OR (
                        status = 'processing'
                        AND updated_at < NOW() - ($2 || ' seconds')::INTERVAL
                    )
                )
                AND attempt_count < $3
                ORDER BY created_at
                LIMIT $1
                FOR UPDATE SKIP LOCKED
                """,
                batch_size,
                str(_LEASE_SECONDS),
                _MAX_ATTEMPTS,
            )
            if rows:
                ids = [r["id"] for r in rows]
                await conn.execute(
                    """
                    UPDATE promotion_queue
                    SET status = 'processing', updated_at = NOW()
                    WHERE id = ANY($1)
                    """,
                    ids,
                )
    return list(rows)


async def _promote_one(
    pool: "asyncpg.Pool",
    mori_store: Any,
    queue_id: uuid.UUID,
    candidate_id: uuid.UUID,
) -> bool:
    """Promote one leased row to canon.

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

        # ── GOV-002: defence-in-depth eligibility re-check ────────────────
        # Re-run the eligibility gate against the candidate body before
        # writing to canon.  If the intake DB has been tampered with (or the
        # gate rules tightened since submission), this prevents arbitrary
        # content from flowing into canon.
        #
        # Re-check the candidate's REAL governance values (target / stable_key /
        # action), fetched from one of its originating submissions via the
        # corroboration ledger — NOT a synthesised key — so the namespace gate
        # and the GOV-001 substring deny are re-applied with full fidelity.
        content_hash_hex: str = candidate["content_hash"]

        # ── GOV-002: body-integrity check ─────────────────────────────────
        # The stored canonicalized_body must hash to the stored content_hash
        # (the hash contract is idempotent over the NFKC canonical form).  A
        # mismatch means the intake DB row was tampered with between dedup and
        # promotion — reject rather than write attacker-controlled content to
        # canon.
        from mori_intake.normalize import content_hash as _compute_hash

        if _compute_hash(body) != content_hash_hex:
            logger.error(
                "canon_writer: GOV-002 body-integrity FAILED for candidate %s "
                "(stored content_hash does not match the body) — marking rejected",
                candidate_id,
            )
            async with conn.transaction():
                await conn.execute(
                    "UPDATE intake_candidates SET status = 'rejected', "
                    "rejection_reason = $1, updated_at = NOW() WHERE id = $2",
                    "promotion-body-integrity-mismatch",
                    candidate_id,
                )
                await _mark_failed(conn, queue_id, "body-integrity mismatch")
            return False

        _orig = await conn.fetchrow(
            """
            SELECT s.target_name, s.stable_key, s.action
            FROM intake_submissions s
            JOIN intake_corroborations c ON c.submission_id = s.id
            WHERE c.candidate_id = $1
            ORDER BY s.received_at
            LIMIT 1
            """,
            candidate_id,
        )
        if _orig is not None:
            _recheck_target = _orig["target_name"]
            _recheck_key = _orig["stable_key"]
            _recheck_action = _orig["action"]
        else:
            # No originating submission found (should not happen) — fall back to
            # a synthetic memory/add key so body/proposition checks still fire.
            _recheck_target = "memory"
            _recheck_key = f"learned-{content_hash_hex[:32]}"
            _recheck_action = "add"
        _body_decision = eligibility_evaluate(
            target=_recheck_target,
            action=_recheck_action,
            stable_key=_recheck_key,
            body=body,
        )
        if not _body_decision.eligible:
            logger.error(
                "canon_writer: GOV-002 eligibility re-check FAILED for candidate %s "
                "(reason=%r) — marking rejected instead of promoting",
                candidate_id,
                _body_decision.reason,
            )
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE intake_candidates
                    SET status = 'rejected',
                        rejection_reason = $1,
                        updated_at = NOW()
                    WHERE id = $2
                    """,
                    f"promotion-eligibility-recheck:{_body_decision.reason}",
                    candidate_id,
                )
                await _mark_failed(
                    conn, queue_id, f"eligibility recheck failed: {_body_decision.reason}"
                )
            return False

        # ── Body validation ────────────────────────────────────────────────
        # Non-empty, length cap, valid UTF-8 (defence-in-depth, GOV-002).
        if not body or not body.strip():
            logger.error(
                "canon_writer: empty body for candidate %s — marking rejected",
                candidate_id,
            )
            async with conn.transaction():
                await conn.execute(
                    "UPDATE intake_candidates SET status='rejected', "
                    "rejection_reason='empty-body', updated_at=NOW() WHERE id=$1",
                    candidate_id,
                )
                await _mark_failed(conn, queue_id, "empty body at promotion time")
            return False

        _MAX_BODY_BYTES = 1_048_576  # 1 MiB
        try:
            body_bytes = body.encode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError) as exc:
            logger.error(
                "canon_writer: body for candidate %s is not valid UTF-8 — rejecting: %s",
                candidate_id,
                exc,
            )
            async with conn.transaction():
                await conn.execute(
                    "UPDATE intake_candidates SET status='rejected', "
                    "rejection_reason='invalid-encoding', updated_at=NOW() WHERE id=$1",
                    candidate_id,
                )
                await _mark_failed(conn, queue_id, f"invalid UTF-8: {exc}")
            return False

        if len(body_bytes) > _MAX_BODY_BYTES:
            logger.error(
                "canon_writer: body for candidate %s exceeds 1 MiB (%d bytes) — rejecting",
                candidate_id,
                len(body_bytes),
            )
            async with conn.transaction():
                await conn.execute(
                    "UPDATE intake_candidates SET status='rejected', "
                    "rejection_reason='body-too-large', updated_at=NOW() WHERE id=$1",
                    candidate_id,
                )
                await _mark_failed(conn, queue_id, f"body too large: {len(body_bytes)} bytes")
            return False

        # ── Derive canon name ──────────────────────────────────────────────
        # Use a deterministic name derived from the content hash so that a
        # re-drive produces the same name (name collisions are handled by
        # mori's upsert — it updates the existing row).
        # content_hash_hex is already bound above (GOV-002 eligibility re-check).
        canon_name = f"agent-intake-{content_hash_hex[:16]}"

        # ── Write to mori canon ────────────────────────────────────────────
        # Use the public store write() API — NOT raw SQL into memories.
        # origin_clients is populated with the distinct corroborating agent_ids.
        # type="agent-intake" distinguishes promoted agent memories from
        # human-authored memories for future trust-curve logic.
        try:
            result = await _call_store_write(
                mori_store,
                name=canon_name,
                title=f"Agent intake: {body[:60]}{'...' if len(body) > 60 else ''}",
                body=body,
                type="agent-intake",
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

        # ── Write memory_intake_lineage (mori-side) ────────────────────────
        # Uses the public mori store method — no _get_conn(), no closing of
        # shared connections.  Idempotent: ON CONFLICT DO NOTHING on both
        # SQLite and Postgres backends.
        try:
            await _call_record_lineage(
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

        # ── Final intake transaction: map + candidate + queue ──────────────
        # All three intake-side writes are in ONE transaction so they are
        # committed atomically.  A crash here leaves the lineage written
        # (idempotent) but the promotion_map absent — the idempotency guard
        # above fires on the next re-drive and skips the canon write.
        try:
            async with conn.transaction():
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
                await _mark_committed(conn, queue_id, canon_name)
        except Exception as exc:
            logger.error(
                "canon_writer: final intake commit failed for %r: %s",
                canon_name,
                exc,
            )
            await _mark_failed(conn, queue_id, f"final commit error: {exc}")
            return False

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


async def _call_record_lineage(mori_store: Any, **kwargs) -> None:
    """Call mori_store.record_intake_lineage() regardless of sync/async.

    Uses the public store method — no ``_get_conn()``, no closing of shared
    connections.  SQLiteStore opens its own short-lived connection internally;
    PostgresStore acquires from the pool.
    """
    result = mori_store.record_intake_lineage(**kwargs)
    if inspect.isawaitable(result):
        await result


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
            status = CASE WHEN attempt_count + 1 >= $3 THEN 'failed' ELSE 'queued' END,
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
