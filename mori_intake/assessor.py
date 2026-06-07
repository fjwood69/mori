"""Assessor worker — Step 2 of the agent-memory-governance pipeline.

Reads ``intake_candidates WHERE status='pending'``, calls an **injected
``assess`` function** to compare each candidate against mori canon, and
applies the verdict→action mapping:

    SUPERSEDES / RELATED  → candidate ``rejected``
                            (``rejection_reason='duplicate-of-canon:<name>'``)
    NEEDS_REVIEW          → candidate left ``pending`` (nothing enqueued)
                            used on any error/exception/uncertain path — fail closed
    UNRELATED             → candidate ``under_review``, then either:
                            * human-review gate (default): surface a mori
                              ``pending_write`` + ``intake_promotion_tickets``
                              row for Trusted-Dreamer approval; or
                            * legacy auto-promotion (``MORI_INTAKE_PROMOTION_ENABLED``):
                              enqueue ``promotion_queue`` for unattended canon write.

The ``assess`` function is injected (not hard-wired) so:

* Tests stay deterministic — the stub returns a fixed verdict without any
  network call.
* B2 wires in the real cheap-model vs-canon check without touching this
  module.

Idempotency
-----------
The assessor only reads ``pending`` candidates.  A candidate that has already
been transitioned to ``under_review``, ``rejected``, or ``promoted`` will
never be picked up again.  Within a single run, a per-row attempt cap prevents
a poison row from hot-looping.

Default stub (FAIL CLOSED)
--------------------------
When no ``assess`` function is supplied, the built-in stub returns
``NEEDS_REVIEW`` — i.e. leave every candidate pending, awaiting a real
assessor.  This is intentionally conservative: the ungoverned fast-path
(default stub → auto-promote) is a security risk that must be gated behind
an explicitly injected UNRELATED verdict.

To exercise the full B1 promotion path in tests, inject an explicit stub::

    def _unrelated_stub(body, h):
        return AssessmentResult(verdict="UNRELATED")

    await assess_once(pool, assess=_unrelated_stub)
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)


def _promotion_enabled() -> bool:
    """Read the ``MORI_INTAKE_PROMOTION_ENABLED`` feature flag from the env.

    When TRUE, an ``UNRELATED`` verdict takes the legacy auto-promotion path
    (enqueue ``promotion_queue`` → ``canon_writer.drain_once`` writes canon
    unattended).  When FALSE (the default), ``UNRELATED`` is **surfaced for
    human review**: a mori ``pending_write`` (source=``agent-intake``) plus a
    bridge-owned ``intake_promotion_tickets`` row — a Trusted Dreamer must
    approve before the bridge finalizer writes canon.  Fail-closed: anything
    other than an explicit truthy value leaves the human gate in place.
    """
    return os.environ.get("MORI_INTAKE_PROMOTION_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


# Per-row attempt cap within one process lifetime.  A row that keeps failing
# is logged and skipped so one bad candidate cannot stall the loop.
_MAX_ATTEMPTS = 5

# Fetch batch size per drain pass.
_BATCH_SIZE = 50


# ── Verdict / assessment types ────────────────────────────────────────────────


@dataclass(frozen=True)
class AssessmentResult:
    """Result returned by an ``assess`` callable.

    Attributes
    ----------
    verdict:
        One of ``"SUPERSEDES"``, ``"RELATED"``, ``"UNRELATED"``, or
        ``"NEEDS_REVIEW"``.

        * ``SUPERSEDES`` / ``RELATED`` → candidate rejected as duplicate.
        * ``UNRELATED`` → candidate queued for promotion.
        * ``NEEDS_REVIEW`` → candidate left ``pending``; no promotion
          enqueued.  Used on any error, empty-store, or uncertain path so
          the system fails closed rather than auto-promoting everything.
    matched_canon_name:
        The mori canon memory name that was matched, or ``None`` when
        verdict is ``"UNRELATED"`` or ``"NEEDS_REVIEW"``.
    score:
        Similarity / confidence score in ``[0.0, 1.0]``.  For the default
        stub this is always ``0.0``.
    """

    verdict: str  # "SUPERSEDES" | "RELATED" | "UNRELATED" | "NEEDS_REVIEW"
    matched_canon_name: str | None = None
    score: float = 0.0


# ── Default stub ──────────────────────────────────────────────────────────────


def _default_stub(body: str, content_hash: str) -> AssessmentResult:  # noqa: ARG001
    """Default assess stub — returns NEEDS_REVIEW (fail closed).

    No real assessment has been wired in.  Rather than auto-promoting every
    candidate (the old UNRELATED default), we leave candidates ``pending``
    until an explicit assess callable is injected.

    Replace in B2 with the real cheap-model vs-canon check via Bifrost.
    Tests that exercise the promotion path must inject an explicit
    UNRELATED stub — see module docstring for the pattern.
    """
    return AssessmentResult(verdict="NEEDS_REVIEW", matched_canon_name=None, score=0.0)


# ── In-memory attempt counter (reset on process restart) ─────────────────────

_attempt_counts: dict[str, int] = {}


# ── Public API ────────────────────────────────────────────────────────────────


async def assess_once(
    pool: "asyncpg.Pool",
    assess: Callable[[str, str], AssessmentResult] | None = None,
    *,
    mori_store: Any = None,
    promotion_enabled: bool | None = None,
    batch_size: int = _BATCH_SIZE,
) -> int:
    """Run one assessor pass over pending candidates.

    Parameters
    ----------
    pool:
        asyncpg pool for the **intake** Postgres.
    assess:
        A callable ``(body: str, content_hash: str) -> AssessmentResult``.
        Pass ``None`` to use the built-in stub (always ``UNRELATED``).
    mori_store:
        A ``BaseStore`` instance (``SQLiteStore`` sync / ``PostgresStore``
        async).  Required for the **human-review** path: on ``UNRELATED`` the
        assessor surfaces a mori ``pending_write`` + ``intake_promotion_tickets``
        row via this store.  Not needed when *promotion_enabled* is True (the
        legacy ``promotion_queue`` path touches only the intake DB).
    promotion_enabled:
        Override for the ``MORI_INTAKE_PROMOTION_ENABLED`` flag.  ``None``
        (default) reads the env via :func:`_promotion_enabled`.  Passed
        explicitly by tests to exercise both paths deterministically.
    batch_size:
        Maximum number of pending candidates to process this pass.

    Returns the number of candidates successfully processed (transitioned
    out of ``pending``) this pass.
    """
    if assess is None:
        assess = _default_stub
    if promotion_enabled is None:
        promotion_enabled = _promotion_enabled()

    rows = await _fetch_pending(pool, batch_size)
    processed = 0
    for row in rows:
        cid = str(row["id"])
        if _attempt_counts.get(cid, 0) >= _MAX_ATTEMPTS:
            continue  # already logged at cap; skip silently
        try:
            result = await _assess_one(
                pool,
                row,
                assess,
                mori_store=mori_store,
                promotion_enabled=promotion_enabled,
            )
            # Only count as processed when the candidate was transitioned out
            # of pending (SUPERSEDES/RELATED → rejected; UNRELATED → under_review).
            # NEEDS_REVIEW leaves the candidate pending — not counted so the
            # caller knows useful work was done only on real transitions.
            if result.verdict != "NEEDS_REVIEW":
                processed += 1
            # Successful call: clear any prior error count (QUAL-001 memory leak fix).
            _attempt_counts.pop(cid, None)
        except Exception as exc:
            _attempt_counts[cid] = _attempt_counts.get(cid, 0) + 1
            count = _attempt_counts[cid]
            if count >= _MAX_ATTEMPTS:
                logger.error(
                    "assessor: candidate %s failed %d times — skipping for this process. "
                    "Last error: %s",
                    cid,
                    count,
                    exc,
                )
            else:
                logger.warning(
                    "assessor: candidate %s failed (attempt %d/%d): %s",
                    cid,
                    count,
                    _MAX_ATTEMPTS,
                    exc,
                )
    return processed


# ── Internal helpers ──────────────────────────────────────────────────────────


async def _fetch_pending(pool: "asyncpg.Pool", batch_size: int) -> list:
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT id, canonicalized_body, content_hash, attempt_count,
                   reinforcement_count
            FROM intake_candidates
            WHERE status = 'pending'
            ORDER BY created_at
            LIMIT $1
            """,
            batch_size,
        )


async def _assess_one(
    pool: "asyncpg.Pool",
    row,
    assess: Callable[[str, str], AssessmentResult],
    *,
    mori_store: Any = None,
    promotion_enabled: bool = False,
) -> AssessmentResult:
    """Assess one candidate and apply the verdict→action mapping.

    Returns the AssessmentResult so the caller can distinguish NEEDS_REVIEW
    (candidate left pending) from genuine terminal verdicts.

    UNRELATED routing
    -----------------
    * ``promotion_enabled`` True  → legacy auto path: candidate ``under_review``
      + ``promotion_queue`` row; ``canon_writer.drain_once`` writes canon
      unattended.
    * ``promotion_enabled`` False → **human-review gate** (default): surface a
      mori ``pending_write`` (source=``agent-intake``) + a bridge-owned
      ``intake_promotion_tickets`` row, then mark the candidate ``under_review``.
      A Trusted Dreamer must approve before the finalizer writes canon.
    """
    candidate_id: uuid.UUID = row["id"]
    body: str = row["canonicalized_body"]
    content_hash_hex: str = row["content_hash"]

    # Call the injected assess function.  The B1 stub and the B2 real assessor
    # are both synchronous (BifrostClient.consult is a blocking OpenAI call).
    # If a caller ever supplies an async assess fn, await it transparently.
    _raw = assess(body, content_hash_hex)
    result: AssessmentResult = await _raw if inspect.isawaitable(_raw) else _raw

    verdict = result.verdict.upper()

    if verdict in ("SUPERSEDES", "RELATED"):
        # Candidate is a duplicate of existing canon — reject it.
        matched = result.matched_canon_name or "unknown"
        rejection_reason = f"duplicate-of-canon:{matched}"
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE intake_candidates
                    SET status = 'rejected',
                        rejection_reason = $1,
                        updated_at = NOW()
                    WHERE id = $2
                    """,
                    rejection_reason,
                    candidate_id,
                )
        logger.info(
            "assessor: candidate %s → rejected (%s, score=%.3f)",
            candidate_id,
            rejection_reason,
            result.score,
        )

    elif verdict == "UNRELATED":
        if promotion_enabled:
            await _enqueue_legacy_promotion(pool, candidate_id)
        else:
            await _surface_for_human_review(pool, mori_store, row, result)

    elif verdict == "NEEDS_REVIEW":
        # Fail-closed path: the assessor could not reach a confident verdict
        # (model error, empty store, search failure, or the default stub is
        # still wired in).  Leave the candidate ``pending`` — re-examined next
        # pass.  Do NOT enqueue or surface.
        logger.info(
            "assessor: candidate %s → NEEDS_REVIEW (left pending; nothing enqueued)",
            candidate_id,
        )

    else:
        raise ValueError(f"assessor: unknown verdict {verdict!r} for candidate {candidate_id}")

    return result


# ── UNRELATED action paths ────────────────────────────────────────────────────


async def _enqueue_legacy_promotion(pool: "asyncpg.Pool", candidate_id: uuid.UUID) -> None:
    """Legacy auto-promotion path (``MORI_INTAKE_PROMOTION_ENABLED`` true).

    Candidate → ``under_review`` + a ``promotion_queue`` row, both in one intake
    transaction.  ``canon_writer.drain_once`` picks the row up and writes canon
    unattended.  Touches only the intake DB.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE intake_candidates
                SET status = 'under_review', updated_at = NOW()
                WHERE id = $1
                """,
                candidate_id,
            )
            # ON CONFLICT DO NOTHING is safe: a prior crash after this UPDATE
            # but before commit leaves the existing queue entry untouched.
            await conn.execute(
                """
                INSERT INTO promotion_queue (id, candidate_id, status)
                VALUES ($1, $2, 'queued')
                ON CONFLICT DO NOTHING
                """,
                uuid.uuid4(),
                candidate_id,
            )
    logger.info(
        "assessor: candidate %s → under_review (enqueued for auto-promotion)",
        candidate_id,
    )


async def _surface_for_human_review(
    pool: "asyncpg.Pool",
    mori_store: Any,
    row,
    result: AssessmentResult,
) -> None:
    """Human-review gate (default): surface an UNRELATED candidate for approval.

    Creates, in order:
      1. a bridge-owned ``intake_promotion_tickets`` row — the **trusted
         carrier** of candidate_id + submission_ids + trust + body_hash;
      2. a mori ``pending_write`` (source=``agent-intake``) whose provenance
         carries ONLY the opaque ``ticket_uuid`` (never the ids the finalizer
         trusts — those are read back from the ticket);
      3. the candidate ``under_review`` claim (intake) — which alone stops
         re-assessment.

    Ordering is deliberate and **self-healing**.  Mori writes happen *before*
    the claim: if any step fails (or the process crashes) before the claim, the
    candidate stays ``pending`` and the next pass re-runs the whole surface with
    a fresh ticket — the prior ticket is an orphan (harmless; the finalizer only
    reads tickets referenced by a live pending_write).  No silent loss, so no
    reconcile sweep is required for correctness.  ``queue_pending_write`` is
    keyed by ``memory_name`` with a partial-unique index on ``status='pending'``,
    so a re-surface updates the existing pending row rather than piling up.
    """
    if mori_store is None:
        raise RuntimeError(
            "assessor: human-review gate requires a mori_store, but none was "
            "injected — set MORI_INTAKE_PROMOTION_ENABLED=true for the legacy "
            "auto-promotion path, or pass mori_store to assess_once()"
        )

    candidate_id: uuid.UUID = row["id"]
    body: str = row["canonicalized_body"]
    content_hash_hex: str = row["content_hash"]
    reinforcement_count: int = row["reinforcement_count"]

    # 1a. Gather corroborating agents + submissions (the ticket's trust evidence).
    async with pool.acquire() as conn:
        corroborations = await conn.fetch(
            "SELECT DISTINCT agent_id, submission_id FROM intake_corroborations "
            "WHERE candidate_id = $1",
            candidate_id,
        )
    agent_ids: list[str] = sorted({str(r["agent_id"]) for r in corroborations})
    submission_ids: list[str] = [str(r["submission_id"]) for r in corroborations]

    ticket_uuid = str(uuid.uuid4())
    canon_name = f"agent-intake-{content_hash_hex[:16]}"
    trust_snapshot = {
        "reinforcement_count": reinforcement_count,
        "corroborating_agent_ids": agent_ids,
    }

    # 1b. Ticket FIRST (the trusted carrier).  ``body_hash`` is the candidate's
    #     own content_hash — the finalizer re-derives it from the canonical body
    #     and compares (GOV-002 body integrity).
    await _call_record_ticket(
        mori_store,
        ticket_uuid=ticket_uuid,
        canon_name=canon_name,
        candidate_id=str(candidate_id),
        submission_ids=submission_ids,
        trust_snapshot=trust_snapshot,
        body_hash=content_hash_hex,
    )

    # 2. pending_write — provenance carries ONLY the opaque ticket_uuid.
    await _call_queue_pending_write(
        mori_store,
        name=canon_name,
        title=f"Agent intake: {body[:60]}{'...' if len(body) > 60 else ''}",
        body=body,
        type="agent-intake",
        tier="working",
        tags=["source:agent-intake"],
        origin_clients=agent_ids,
        proposed_by="intake-assessor",
        source="agent-intake",
        provenance=json.dumps({"ticket_uuid": ticket_uuid}),
        confidence=result.score,
    )

    # 3. Claim — candidate → under_review.  Only now does re-assessment stop.
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE intake_candidates
                SET status = 'under_review', updated_at = NOW()
                WHERE id = $1
                """,
                candidate_id,
            )
    logger.info(
        "assessor: candidate %s → under_review (surfaced for human review as %r, "
        "ticket=%s, score=%.3f)",
        candidate_id,
        canon_name,
        ticket_uuid,
        result.score,
    )


# ── mori store call wrappers (sync SQLiteStore / async PostgresStore) ──────────


async def _call_record_ticket(mori_store: Any, **kwargs) -> None:
    """Call ``mori_store.record_intake_ticket()`` regardless of sync/async."""
    result = mori_store.record_intake_ticket(**kwargs)
    if inspect.isawaitable(result):
        await result


async def _call_queue_pending_write(mori_store: Any, **kwargs) -> str:
    """Call ``mori_store.queue_pending_write()`` regardless of sync/async."""
    result = mori_store.queue_pending_write(**kwargs)
    if inspect.isawaitable(result):
        return await result
    return result  # type: ignore[return-value]
