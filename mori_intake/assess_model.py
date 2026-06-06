"""Real fast-model vs-canon assessor — Stream B2.

Provides :func:`make_canon_assessor`, a factory that returns a callable with
the same signature as the B1 stub::

    assess(body: str, content_hash: str) -> AssessmentResult

The returned function:

1. Retrieves the top-k most relevant *canonical* memory NAMES from mori via
   ``search(query, limit)`` (read-only search callable).
2. Fetches each neighbour's FULL BODY via ``fetch_body(name)`` (read-only
   body callable) so the contradiction-scan prompt receives the complete text,
   not merely the one-line description that ``search_json`` returns.
3. Compares the candidate body against each neighbour using the same prompt
   that ``run_contradiction_scan`` in ``mori_advisor/utils.py`` uses — verbatim
   reuse of ``CONTRADICTION_SCAN_PROMPT`` and the ``"fast"`` VK.
4. Returns the first SUPERSEDES or RELATED match found (highest-ranked by the
   store), or UNRELATED when no match is found / the store is empty.

Safe defaults
-------------
* Empty store or no canon neighbours → UNRELATED, score 0.0.
* Model returns anything other than SUPERSEDES / RELATED / UNRELATED →
  UNRELATED (no crash).
* Any exception during retrieval or model call → UNRELATED + logged.

Data boundary
-------------
The assessor receives two **read-only callables** (``search`` and
``fetch_body``) rather than the full mori store object.  This ensures the
assessor has NO write path to canon whatsoever — it cannot call
``store.write()`` because it never holds a store reference.

Injectability
-------------
``search``, ``fetch_body``, and ``llm_client`` are all constructor arguments
so tests can pass mocks; real Bifrost is only called in production.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from mori_advisor.bifrost_client import BifrostClient

from mori_advisor.utils import CONTRADICTION_SCAN_PROMPT
from mori_intake.assessor import AssessmentResult

logger = logging.getLogger(__name__)

# Verdicts the model may return — these are the ONLY terminal verdicts
# accepted from the model.  Anything else collapses to NEEDS_REVIEW.
_VALID_VERDICTS = frozenset({"SUPERSEDES", "RELATED", "UNRELATED"})

# Confidence score assigned to a SUPERSEDES/RELATED match.  The fast model
# returns one word — there is no numeric probability.  A fixed high score is
# appropriate because the verdict already encodes the classification certainty.
_MATCH_SCORE = 0.85


@dataclass(frozen=True)
class CanonReader:
    """Frozen pair of read-only callables over mori canon.

    The assessor accepts this instead of a full BaseStore to enforce the
    data boundary: no write method is reachable, not even indirectly.

    Attributes
    ----------
    search:
        ``(query: str, limit: int) -> list[dict]`` — returns lightweight
        dicts with at least ``name`` and ``tier`` keys (same shape as
        ``search_json``).  No side-effects; no retrieval-count bump.
        On the Postgres backend this is an **async** callable (a coroutine);
        callers must ``await`` it.
    fetch_body:
        ``(name: str) -> str`` — returns the full body text of a single
        memory, or an empty string when the memory is not found.  No
        side-effects; no retrieval-count bump.
        On the Postgres backend this is an **async** callable (a coroutine);
        callers must ``await`` it.
    """

    search: Callable  # (query: str, limit: int) -> list[dict] | Awaitable[list[dict]]
    fetch_body: Callable  # (name: str) -> str | Awaitable[str]


def make_canon_reader_from_store(store) -> CanonReader:
    """Build a :class:`CanonReader` from a mori store instance.

    Delegates to ``store.canon_reader()``.  On a ``PostgresStore`` this
    returns **async** callables; the returned ``assess`` function from
    :func:`make_canon_assessor` is therefore also **async** — which is
    correct: the assessor call-site in ``assessor.py`` already handles
    awaitables transparently via ``inspect.isawaitable``.

    On a ``SQLiteStore`` ``store.canon_reader()`` raises
    ``NotImplementedError`` immediately — agent-intake promotion is a
    Postgres-only feature by design.

    The previous implementation accessed ``store._mem`` directly (a private
    attribute coupling).  This version uses only the public ``canon_reader()``
    interface on ``BaseStore``.
    """
    return store.canon_reader()


def make_canon_assessor(
    reader: "CanonReader",
    llm_client: "BifrostClient",
    *,
    top_k: int = 5,
) -> Callable:
    """Return an **async** ``assess(body, content_hash) -> AssessmentResult`` callable.

    The returned callable is a coroutine function because the ``reader``
    callables provided by ``PostgresStore.canon_reader()`` are themselves
    async (``search_json`` and ``get_memory`` are coroutines on the Postgres
    backend).

    The assessor call-site in ``assessor.py::_assess_one`` already handles
    awaitables transparently::

        _raw = assess(body, content_hash_hex)
        result = await _raw if inspect.isawaitable(_raw) else _raw

    So promoting ``assess`` to async is backward-compatible — the call-site
    awaits it correctly whether it is a coroutine or a plain callable.

    Parameters
    ----------
    reader:
        A :class:`CanonReader` holding two read-only callables over mori
        canon.  The assessor has NO write path — ``reader`` exposes only
        ``search`` and ``fetch_body``.  On the Postgres backend both
        callables are async; this function awaits them.
    llm_client:
        A :class:`~mori_advisor.bifrost_client.BifrostClient` instance.  The
        ``"fast"`` VK (DeepSeek V4 Flash / equivalent cheap model) is used,
        matching the ``run_contradiction_scan`` path in the dream pipeline.
    top_k:
        Number of canon neighbours to retrieve and check.  Default 5.
    """

    async def _search_canon(body: str) -> list[dict] | None:
        """Retrieve top-k canonical memories most relevant to *body*.

        Awaits the reader's search callable (async on Postgres, sync on any
        stub).  Filters by ``tier == 'canonical'`` post-retrieval.  Fetches
        ``top_k * 2`` so the canonical filter has headroom.

        Returns ``None`` on any exception (signals NEEDS_REVIEW to caller),
        an empty list when the store has no canonical neighbours (store is
        genuinely empty — UNRELATED is safe), or the canonical rows.
        """
        import inspect as _inspect

        try:
            _raw = reader.search(body, top_k * 2)
            raw: list[dict] = await _raw if _inspect.isawaitable(_raw) else _raw
            # Filter to canonical tier only.
            return [m for m in raw if m.get("tier") == "canonical"][:top_k]
        except Exception as exc:
            logger.warning("assess_model: canon search failed — NEEDS_REVIEW: %s", exc)
            return None  # signals caller to return NEEDS_REVIEW

    async def _classify(body: str, neighbour: dict) -> str:
        """Ask the fast model to classify *body* vs *neighbour*.

        Awaits the reader's fetch_body callable (async on Postgres, sync on
        any stub).  Falls back to the search-result dict fields when
        fetch_body returns empty.

        Reuses ``CONTRADICTION_SCAN_PROMPT`` verbatim from
        ``mori_advisor.utils``.  Returns the raw verdict word (upper-case),
        or ``"UNRELATED"`` on any error / unrecognised token.
        """
        import inspect as _inspect

        name = neighbour.get("name", "")
        try:
            _raw_body = reader.fetch_body(name)
            full_body: str = await _raw_body if _inspect.isawaitable(_raw_body) else _raw_body
        except Exception as exc:
            logger.warning("assess_model: fetch_body failed for %s: %s", name, exc)
            full_body = ""

        # Fallback: if fetch_body returned nothing, use whatever came back
        # in the search result dict (description or body field if present).
        existing_body = full_body or neighbour.get("body") or neighbour.get("description") or ""

        prompt = CONTRADICTION_SCAN_PROMPT.format(
            new_title="Candidate memory",
            new_body=body[:2000],
            existing_title=neighbour.get("title", ""),
            existing_body=existing_body[:2000],
        )
        try:
            response = llm_client.consult(
                system=prompt,
                user=f"new: (candidate)\nexisting: {name}",
                vk="fast",
                max_tokens=16,
                temperature=0.0,
            )
            word = (response or "").strip().upper()
            if word in _VALID_VERDICTS:
                return word
            # Model may return a sentence; take the first word and retry.
            first = word.split()[0] if word else ""
            if first in _VALID_VERDICTS:
                return first
            # Unrecognised / empty response → NEEDS_REVIEW (fail closed).
            logger.warning(
                "assess_model: unrecognised model output %r for neighbour %s — NEEDS_REVIEW",
                word,
                name,
            )
            return "NEEDS_REVIEW"
        except Exception as exc:
            logger.warning(
                "assess_model: model call failed for neighbour %s — NEEDS_REVIEW: %s",
                name,
                exc,
            )
            return "NEEDS_REVIEW"

    async def assess(body: str, content_hash: str) -> AssessmentResult:  # noqa: ARG001
        """Compare *body* against top-k canon neighbours; return the verdict.

        This is an async coroutine because the underlying ``reader`` callables
        (``search`` and ``fetch_body``) are async on the Postgres backend.

        Steps
        -----
        1. Retrieve top-k canonical memories via text search.
           Search failure → NEEDS_REVIEW (fail closed, not UNRELATED).
        2. Empty store (no canonical neighbours) → UNRELATED (safe: nothing
           in canon means this IS genuinely novel).
        3. For each neighbour (highest-ranked first), fetch its full body
           and ask the fast model to classify candidate vs neighbour.
           Any model error / malformed response → NEEDS_REVIEW for that call.
        4. Return the first SUPERSEDES or RELATED match found.
        5. If any neighbour returned NEEDS_REVIEW, propagate NEEDS_REVIEW
           (uncertainty from any classification step → fail closed).
        6. If all neighbours are UNRELATED, return UNRELATED.

        The only path that returns UNRELATED is a genuine model confirmation
        that the candidate is novel.  Every uncertain/error path → NEEDS_REVIEW.
        """
        neighbours = await _search_canon(body)

        # None → search raised an exception → fail closed.
        if neighbours is None:
            return AssessmentResult(verdict="NEEDS_REVIEW", matched_canon_name=None, score=0.0)

        # Empty list → store has no canonical memories → genuinely novel.
        if not neighbours:
            logger.debug("assess_model: no canon neighbours found — UNRELATED")
            return AssessmentResult(verdict="UNRELATED", matched_canon_name=None, score=0.0)

        seen_needs_review = False
        for neighbour in neighbours:
            verdict = await _classify(body, neighbour)
            name = neighbour.get("name")
            logger.debug(
                "assess_model: candidate vs %s → %s",
                name,
                verdict,
            )
            if verdict in ("SUPERSEDES", "RELATED"):
                return AssessmentResult(
                    verdict=verdict,
                    matched_canon_name=name,
                    score=_MATCH_SCORE,
                )
            if verdict == "NEEDS_REVIEW":
                seen_needs_review = True
                # Continue scanning — a later neighbour may give a definitive
                # SUPERSEDES/RELATED that still allows rejection.  But if no
                # match is found, uncertainty propagates.

        if seen_needs_review:
            # At least one classification was uncertain → fail closed.
            return AssessmentResult(verdict="NEEDS_REVIEW", matched_canon_name=None, score=0.0)

        return AssessmentResult(verdict="UNRELATED", matched_canon_name=None, score=0.0)

    return assess
