"""Assessor worker — Step 2 of the agent-memory-governance pipeline.

Reads ``intake_candidates WHERE status='pending'``, calls an **injected
``assess`` function** to compare each candidate against mori canon, and
applies the verdict→action mapping:

    SUPERSEDES / RELATED  → candidate ``rejected``
                            (``rejection_reason='duplicate-of-canon:<name>'``)
    UNRELATED             → candidate ``under_review``
                            + enqueue ``promotion_queue``

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

Default stub
------------
When no ``assess`` function is supplied, the built-in stub always returns
``UNRELATED`` — i.e. treat every pending candidate as novel and forward it
to the promotion queue.  This makes the B1 end-to-end path (seed → assess →
promote → canon) fully exercisable with a deterministic stub.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

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
        One of ``"SUPERSEDES"``, ``"RELATED"``, or ``"UNRELATED"``.
    matched_canon_name:
        The mori canon memory name that was matched, or ``None`` when
        verdict is ``"UNRELATED"``.
    score:
        Similarity / confidence score in ``[0.0, 1.0]``.  For the default
        stub this is always ``0.0``.
    """

    verdict: str  # "SUPERSEDES" | "RELATED" | "UNRELATED"
    matched_canon_name: str | None = None
    score: float = 0.0


# ── Default stub ──────────────────────────────────────────────────────────────


def _default_stub(body: str, content_hash: str) -> AssessmentResult:  # noqa: ARG001
    """Default assess stub — always returns UNRELATED (treat as novel).

    This is the B1 placeholder.  Replace in B2 with the real cheap-model
    vs-canon check via Bifrost.
    """
    return AssessmentResult(verdict="UNRELATED", matched_canon_name=None, score=0.0)


# ── In-memory attempt counter (reset on process restart) ─────────────────────

_attempt_counts: dict[str, int] = {}


# ── Public API ────────────────────────────────────────────────────────────────


async def assess_once(
    pool: "asyncpg.Pool",
    assess: Callable[[str, str], AssessmentResult] | None = None,
    *,
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
    batch_size:
        Maximum number of pending candidates to process this pass.

    Returns the number of candidates successfully processed (transitioned
    out of ``pending``) this pass.
    """
    if assess is None:
        assess = _default_stub

    rows = await _fetch_pending(pool, batch_size)
    processed = 0
    for row in rows:
        cid = str(row["id"])
        if _attempt_counts.get(cid, 0) >= _MAX_ATTEMPTS:
            continue  # already logged at cap; skip silently
        try:
            await _assess_one(pool, row, assess)
            processed += 1
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
            SELECT id, canonicalized_body, content_hash, attempt_count
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
) -> None:
    """Assess one candidate and apply the verdict→action mapping."""
    candidate_id: uuid.UUID = row["id"]
    body: str = row["canonicalized_body"]
    content_hash_hex: str = row["content_hash"]

    # Call the injected assess function (synchronous; B2 may make it async,
    # at which point this call site will need an await or inspect.isawaitable).
    result: AssessmentResult = assess(body, content_hash_hex)

    verdict = result.verdict.upper()

    async with pool.acquire() as conn:
        async with conn.transaction():
            if verdict in ("SUPERSEDES", "RELATED"):
                # Candidate is a duplicate of existing canon — reject it.
                matched = result.matched_canon_name or "unknown"
                rejection_reason = f"duplicate-of-canon:{matched}"
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
                # Novel candidate — hand off to the promotion queue (Step 3).
                await conn.execute(
                    """
                    UPDATE intake_candidates
                    SET status = 'under_review',
                        updated_at = NOW()
                    WHERE id = $1
                    """,
                    candidate_id,
                )
                # Enqueue for the canon writer.  ON CONFLICT DO NOTHING is safe:
                # if the row was already queued (e.g. a prior crash after this
                # UPDATE but before commit), we simply leave the existing entry.
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
                    "assessor: candidate %s → under_review (enqueued for promotion)",
                    candidate_id,
                )

            else:
                raise ValueError(
                    f"assessor: unknown verdict {verdict!r} for candidate {candidate_id}"
                )
