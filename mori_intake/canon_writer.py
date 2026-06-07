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


async def finalize_once(
    intake_pool: "asyncpg.Pool",
    mori_store: Any,
    *,
    batch_size: int = _BATCH_SIZE,
) -> int:
    """Bridge finalizer — phase 2 of the human-review gate.

    The assessor surfaces an ``UNRELATED`` candidate as a mori ``pending_write``
    (source=``agent-intake``) + a bridge-owned ``intake_promotion_tickets`` row.
    A Trusted Dreamer's *approve* records a **vote** (the pending_write moves to
    ``status='human_approved'`` — NO canon write).  This finalizer is the only
    component that holds BOTH the mori store and the intake pool, so it alone can
    turn that vote into canon:

    1. Read ``human_approved`` agent-intake pending_writes from mori.
    2. For each, read the trusted ticket (candidate_id + submission_ids + trust +
       body_hash) — the provenance JSON carries ONLY an opaque ``ticket_uuid``,
       so a forged provenance can never smuggle ids the finalizer trusts.
    3. Re-run the GOV-002 gate (approved-body integrity, live-candidate body
       integrity, eligibility re-check) against the **live** intake candidate.
    4. On pass → write canon + lineage + mark candidate ``promoted`` +
       pending_write ``approved``.  On fail → reject both.

    At-least-once + idempotent, never XA/2PC — mirrors ``drain_once``.  Returns
    the number of pending_writes finalized (promoted or idempotently skipped).
    """
    # ── Phase 2a: promote TD-approved candidates ──────────────────────────────
    approved = await _call_pending_list(
        mori_store, status="human_approved", proposed_by="intake-assessor"
    )
    finalized = 0
    for pw in approved[:batch_size]:
        # Defensive: proposed_by already scopes to the assessor, but never act on
        # a row whose source is not the agent-intake gate.
        if (pw.get("source") or "") != "agent-intake":
            continue
        try:
            if await _finalize_one(intake_pool, mori_store, pw):
                finalized += 1
        except Exception as exc:
            logger.error(
                "finalizer: pending_write %s raised, leaving human_approved for retry: %s",
                pw.get("id"),
                exc,
            )

    # ── Phase 2b: reconcile TD-rejected candidates ────────────────────────────
    # A TD *reject* sets the mori pending_write to 'rejected' directly (mori has
    # no intake reach).  The candidate is therefore still 'under_review' in
    # intake.  The bridge — the only component with both DSNs — closes the loop:
    # mark the candidate 'rejected' and terminalize the pending_write so it is
    # not re-scanned.
    rejected = await _call_pending_list(
        mori_store, status="rejected", proposed_by="intake-assessor"
    )
    for pw in rejected[:batch_size]:
        if (pw.get("source") or "") != "agent-intake":
            continue
        try:
            await _reconcile_rejected_one(intake_pool, mori_store, pw)
        except Exception as exc:
            logger.error(
                "finalizer: reconcile of rejected pending_write %s raised: %s",
                pw.get("id"),
                exc,
            )
    return finalized


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


async def _finalize_one(
    intake_pool: "asyncpg.Pool",
    mori_store: Any,
    pw: dict,
) -> bool:
    """Finalize one human-approved agent-intake pending_write.

    Returns True if the row reached canon (freshly or idempotently), False if it
    was rejected or left for retry.  Does NOT raise — ``finalize_once`` logs and
    moves on.

    Trust boundary: the only ids this function acts on come from the
    **bridge-owned** ``intake_promotion_tickets`` row, looked up by the opaque
    ``ticket_uuid`` carried in the pending_write provenance.  The provenance JSON
    is never trusted for candidate_id / submission_ids / body_hash — those are
    read back from the ticket, so a tampered provenance cannot redirect the
    promotion.
    """
    write_id = pw.get("id")
    canon_name = pw.get("name") or ""
    body = pw.get("body") or ""
    provenance = pw.get("provenance")

    # ── Forgery guard 1: provenance must carry a ticket_uuid ───────────────────
    ticket_uuid = provenance.get("ticket_uuid") if isinstance(provenance, dict) else None
    if not ticket_uuid:
        logger.error(
            "finalizer: pending_write %s has no ticket_uuid in provenance — rejecting",
            write_id,
        )
        await _call_set_pending_status(
            mori_store, write_id, "rejected", note="finalize: missing ticket_uuid"
        )
        return False

    # ── Forgery guard 2: the ticket must exist in the bridge-owned table ───────
    ticket = await _call_get_ticket(mori_store, ticket_uuid)
    if ticket is None:
        logger.error(
            "finalizer: ticket %s not found for pending_write %s — rejecting",
            ticket_uuid,
            write_id,
        )
        await _call_set_pending_status(
            mori_store, write_id, "rejected", note="finalize: ticket not found"
        )
        return False

    # ── Forgery guard 3: ticket's canon_name must match the pending_write ──────
    if ticket["canon_name"] != canon_name:
        logger.error(
            "finalizer: ticket %s canon_name %r != pending_write name %r — rejecting",
            ticket_uuid,
            ticket["canon_name"],
            canon_name,
        )
        await _call_set_pending_status(
            mori_store, write_id, "rejected", note="finalize: ticket/name mismatch"
        )
        return False

    candidate_id_str: str = str(ticket["candidate_id"])
    submission_ids = ticket["submission_ids"] or []
    trust_snapshot = ticket["trust_snapshot"] or {}
    body_hash: str = ticket["body_hash"]

    from mori_intake.normalize import content_hash as _compute_hash

    # ── GOV-002a: the body the TD approved must hash to the ticket body_hash ───
    # Catches tampering of the pending_write body between surface and approval.
    if _compute_hash(body) != body_hash:
        logger.error(
            "finalizer: GOV-002 approved-body hash != ticket body_hash for %r — rejecting",
            canon_name,
        )
        await _call_set_pending_status(
            mori_store, write_id, "rejected", note="finalize: approved-body integrity"
        )
        return False

    # intake is always Postgres for this feature — pass real UUIDs.
    candidate_uuid = uuid.UUID(candidate_id_str)
    submission_uuids = [uuid.UUID(str(s)) for s in submission_ids]

    # ── Cross to intake: idempotency guard + live GOV-002 re-checks ────────────
    async with intake_pool.acquire() as conn:
        existing_map = await conn.fetchrow(
            "SELECT canon_name FROM intake_promotion_map WHERE candidate_id = $1",
            candidate_uuid,
        )
        if existing_map is not None:
            logger.info(
                "finalizer: candidate %s already promoted as %r — marking pending_write "
                "approved (idempotent re-drive)",
                candidate_uuid,
                existing_map["canon_name"],
            )
            await _call_set_pending_status(
                mori_store, write_id, "approved", note="finalize: idempotent (already promoted)"
            )
            return True

        candidate = await conn.fetchrow(
            "SELECT canonicalized_body, content_hash, reinforcement_count "
            "FROM intake_candidates WHERE id = $1",
            candidate_uuid,
        )
        if candidate is None:
            logger.error(
                "finalizer: candidate %s missing in intake — rejecting pending_write %s",
                candidate_uuid,
                write_id,
            )
            await _call_set_pending_status(
                mori_store, write_id, "rejected", note="finalize: candidate missing"
            )
            return False

        live_body: str = candidate["canonicalized_body"]
        live_hash: str = candidate["content_hash"]
        reinforcement_count: int = candidate["reinforcement_count"]

        # ── GOV-002b: live intake body integrity (and ticket↔live agreement) ───
        if _compute_hash(live_body) != live_hash or live_hash != body_hash:
            logger.error(
                "finalizer: GOV-002 live-candidate body-integrity mismatch for %s — rejecting",
                candidate_uuid,
            )
            async with conn.transaction():
                await conn.execute(
                    "UPDATE intake_candidates SET status='rejected', "
                    "rejection_reason=$1, updated_at=NOW() WHERE id=$2",
                    "finalize-body-integrity-mismatch",
                    candidate_uuid,
                )
            await _call_set_pending_status(
                mori_store, write_id, "rejected", note="finalize: live body integrity"
            )
            return False

        # ── GOV-002c: eligibility re-check against the originating submission ───
        _orig = await conn.fetchrow(
            """
            SELECT s.target_name, s.stable_key, s.action
            FROM intake_submissions s
            JOIN intake_corroborations c ON c.submission_id = s.id
            WHERE c.candidate_id = $1
            ORDER BY s.received_at
            LIMIT 1
            """,
            candidate_uuid,
        )
        if _orig is not None:
            _t, _k, _a = _orig["target_name"], _orig["stable_key"], _orig["action"]
        else:
            _t, _k, _a = "memory", f"learned-{live_hash[:32]}", "add"
        _decision = eligibility_evaluate(target=_t, action=_a, stable_key=_k, body=live_body)
        if not _decision.eligible:
            logger.error(
                "finalizer: GOV-002 eligibility re-check FAILED for %s (reason=%r) — rejecting",
                candidate_uuid,
                _decision.reason,
            )
            async with conn.transaction():
                await conn.execute(
                    "UPDATE intake_candidates SET status='rejected', "
                    "rejection_reason=$1, updated_at=NOW() WHERE id=$2",
                    f"finalize-eligibility-recheck:{_decision.reason}",
                    candidate_uuid,
                )
            await _call_set_pending_status(
                mori_store,
                write_id,
                "rejected",
                note=f"finalize: eligibility {_decision.reason}",
            )
            return False

    # ── GOV-002 passed → write canon (idempotent upsert) ──────────────────────
    agent_ids = (
        trust_snapshot.get("corroborating_agent_ids", [])
        if isinstance(trust_snapshot, dict)
        else []
    )
    try:
        await _call_store_write(
            mori_store,
            name=canon_name,
            title=f"Agent intake: {live_body[:60]}{'...' if len(live_body) > 60 else ''}",
            body=live_body,
            type="agent-intake",
            tier="working",
            tags=["source:agent-intake"],
            origin_clients=agent_ids,
        )
    except Exception as exc:
        logger.error(
            "finalizer: canon write failed for %r: %s — leaving human_approved for retry",
            canon_name,
            exc,
        )
        return False

    now_utc = datetime.now(timezone.utc)

    try:
        await _call_record_lineage(
            mori_store,
            canon_name=canon_name,
            intake_candidate_id=candidate_id_str,
            intake_submission_ids=[str(s) for s in submission_ids],
            trust_snapshot=trust_snapshot,
            promoted_at=now_utc,
        )
    except Exception as exc:
        logger.error(
            "finalizer: lineage write failed for %r: %s — canon written; retry next pass",
            canon_name,
            exc,
        )
        return False

    # ── Intake side: promotion_map + candidate promoted (atomic) ──────────────
    try:
        async with intake_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO intake_promotion_map
                        (canon_name, candidate_id, submission_ids, provenance_snapshot, promoted_at)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (canon_name) DO NOTHING
                    """,
                    canon_name,
                    candidate_uuid,
                    submission_uuids,
                    json.dumps(trust_snapshot),
                    now_utc,
                )
                await conn.execute(
                    """
                    UPDATE intake_candidates
                    SET status = 'promoted', promoted_canon_name = $1, updated_at = $2
                    WHERE id = $3
                    """,
                    canon_name,
                    now_utc,
                    candidate_uuid,
                )
    except Exception as exc:
        logger.error(
            "finalizer: intake commit failed for %r: %s — canon written; map/candidate "
            "retried on next pass (idempotency guard skips the re-write)",
            canon_name,
            exc,
        )
        return False

    # ── Terminal: mark the pending_write approved ─────────────────────────────
    await _call_set_pending_status(
        mori_store, write_id, "approved", note="finalize: promoted to canon"
    )
    logger.info(
        "finalizer: pending_write %s → canon %r promoted (candidate %s, reinforcement=%d, agents: %s)",
        write_id,
        canon_name,
        candidate_uuid,
        reinforcement_count,
        ", ".join(agent_ids) or "none",
    )
    return True


async def _reconcile_rejected_one(
    intake_pool: "asyncpg.Pool",
    mori_store: Any,
    pw: dict,
) -> None:
    """Reconcile one TD-rejected agent-intake pending_write back into intake.

    Marks the originating candidate ``rejected`` (only if still ``under_review``
    — idempotent) and terminalizes the pending_write to ``rejected_reconciled``
    so future finalize passes do not re-scan it.  Trust boundary identical to
    :func:`_finalize_one`: the candidate id comes only from the bridge-owned
    ticket, never the provenance JSON.
    """
    write_id = pw.get("id")
    provenance = pw.get("provenance")
    ticket_uuid = provenance.get("ticket_uuid") if isinstance(provenance, dict) else None

    if not ticket_uuid:
        await _call_set_pending_status(
            mori_store, write_id, "rejected_reconciled", note="reconcile: no ticket_uuid"
        )
        return

    ticket = await _call_get_ticket(mori_store, ticket_uuid)
    if ticket is None:
        await _call_set_pending_status(
            mori_store, write_id, "rejected_reconciled", note="reconcile: ticket not found"
        )
        return

    candidate_uuid = uuid.UUID(str(ticket["candidate_id"]))
    async with intake_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE intake_candidates
                SET status = 'rejected',
                    rejection_reason = $1,
                    updated_at = NOW()
                WHERE id = $2 AND status = 'under_review'
                """,
                "human-review-rejected",
                candidate_uuid,
            )
    await _call_set_pending_status(
        mori_store, write_id, "rejected_reconciled", note="reconcile: candidate rejected"
    )
    logger.info(
        "finalizer: reconciled rejected pending_write %s → candidate %s rejected",
        write_id,
        candidate_uuid,
    )


async def _call_pending_list(mori_store: Any, **kwargs) -> list:
    """Call mori_store.pending_list_json() regardless of sync/async."""
    result = mori_store.pending_list_json(**kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def _call_get_ticket(mori_store: Any, ticket_uuid: str):
    """Call mori_store.get_intake_ticket() regardless of sync/async."""
    result = mori_store.get_intake_ticket(ticket_uuid)
    if inspect.isawaitable(result):
        return await result
    return result


async def _call_set_pending_status(mori_store: Any, write_id, status: str, note: str = "") -> None:
    """Call mori_store.set_pending_status() regardless of sync/async."""
    result = mori_store.set_pending_status(write_id, status, note=note)
    if inspect.isawaitable(result):
        await result


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
