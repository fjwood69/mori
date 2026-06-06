"""Real fast-model vs-canon assessor — Stream B2.

Provides :func:`make_canon_assessor`, a factory that returns a callable with
the same signature as the B1 stub::

    assess(body: str, content_hash: str) -> AssessmentResult

The returned function:

1. Retrieves the top-k most relevant *canonical* memories from the mori store
   using ``store.search_json(query=body, limit=top_k)``.  (MVV: text-search
   top-k; vector/pgvector similarity is Slice 3.)
2. Compares the candidate body against each neighbour using the same prompt
   that ``run_contradiction_scan`` in ``mori_advisor/utils.py`` uses — verbatim
   reuse of ``CONTRADICTION_SCAN_PROMPT`` and the ``"fast"`` VK.
3. Returns the first SUPERSEDES or RELATED match found (highest-ranked by the
   store), or UNRELATED when no match is found / the store is empty.

Safe defaults
-------------
* Empty store or no canon neighbours → UNRELATED, score 0.0.
* Model returns anything other than SUPERSEDES / RELATED / UNRELATED →
  UNRELATED (no crash).
* Any exception during retrieval or model call → UNRELATED + logged.

Injectability
-------------
Both ``store`` and ``llm_client`` are constructor arguments so tests can pass
mocks; real Bifrost is only called in production.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from mori_advisor.bifrost_client import BifrostClient
    from mori_advisor.store.base import BaseStore

from mori_advisor.utils import CONTRADICTION_SCAN_PROMPT
from mori_intake.assessor import AssessmentResult

logger = logging.getLogger(__name__)

# Verdicts the model may return — anything else collapses to UNRELATED.
_VALID_VERDICTS = frozenset({"SUPERSEDES", "RELATED", "UNRELATED"})

# Confidence score assigned to a SUPERSEDES/RELATED match.  The fast model
# returns one word — there is no numeric probability.  A fixed high score is
# appropriate because the verdict already encodes the classification certainty.
_MATCH_SCORE = 0.85


def make_canon_assessor(
    store: "BaseStore",
    llm_client: "BifrostClient",
    *,
    top_k: int = 5,
) -> Callable[[str, str], AssessmentResult]:
    """Return a synchronous ``assess(body, content_hash) -> AssessmentResult`` callable.

    Parameters
    ----------
    store:
        A mori :class:`~mori_advisor.store.base.BaseStore` instance.  Only
        ``search_json`` is called; the store is never written to (stateless).
    llm_client:
        A :class:`~mori_advisor.bifrost_client.BifrostClient` instance.  The
        ``"fast"`` VK (DeepSeek V4 Flash / equivalent cheap model) is used,
        matching the ``run_contradiction_scan`` path in the dream pipeline.
    top_k:
        Number of canon neighbours to retrieve and check.  Default 5.
    """

    def _search_canon(body: str) -> list[dict]:
        """Retrieve top-k canonical memories most relevant to *body*.

        SQLiteStore.search_json is synchronous; PostgresStore.search_json is
        async.  Both stores expose a ``search_json`` method — we call the sync
        form here.  Postgres callers will need to run this in an executor or
        use the async path (Slice 3 concern).

        Filters by ``type_filter=None`` — the store's FTS/LIKE ranking already
        surfaces the most relevant memories regardless of type.  Canon-tier
        filtering is applied post-retrieval (spec says "retrieve top-k canon
        neighbours"; in MVV the store mixes tiers, so we filter here).
        """
        try:
            raw = store.search_json(query=body, limit=top_k * 2)
            # Filter to canonical tier only (MVV store may return working-tier rows).
            return [m for m in raw if m.get("tier") == "canonical"][:top_k]
        except Exception as exc:
            logger.warning("assess_model: canon search failed: %s", exc)
            return []

    def _classify(body: str, neighbour: dict) -> str:
        """Ask the fast model to classify *body* vs *neighbour*.

        Reuses ``CONTRADICTION_SCAN_PROMPT`` verbatim from
        ``mori_advisor.utils`` — the same prompt the dream pipeline's
        contradiction scan uses.  Returns the raw verdict word (upper-case),
        or ``"UNRELATED"`` on any error / unrecognised token.
        """
        prompt = CONTRADICTION_SCAN_PROMPT.format(
            new_title="Candidate memory",
            new_body=body[:2000],
            existing_title=neighbour.get("title", ""),
            existing_body=(neighbour.get("body") or neighbour.get("description") or "")[:2000],
        )
        try:
            response = llm_client.consult(
                system=prompt,
                user=f"new: (candidate)\nexisting: {neighbour.get('name', '')}",
                vk="fast",
                max_tokens=16,
                temperature=0.0,
            )
            word = (response or "").strip().upper()
            if word in _VALID_VERDICTS:
                return word
            # Model may return a sentence; take the first word and retry.
            first = word.split()[0] if word else "UNRELATED"
            return first if first in _VALID_VERDICTS else "UNRELATED"
        except Exception as exc:
            logger.warning(
                "assess_model: model call failed for neighbour %s: %s",
                neighbour.get("name"),
                exc,
            )
            return "UNRELATED"

    def assess(body: str, content_hash: str) -> AssessmentResult:  # noqa: ARG001
        """Compare *body* against top-k canon neighbours; return the verdict.

        Steps
        -----
        1. Retrieve top-k canonical memories via text search.
        2. For each neighbour (highest-ranked first), ask the fast model to
           classify candidate vs neighbour.
        3. Return the first SUPERSEDES or RELATED match found.
        4. If all neighbours are UNRELATED (or there are none), return UNRELATED.
        """
        neighbours = _search_canon(body)
        if not neighbours:
            logger.debug("assess_model: no canon neighbours found — UNRELATED")
            return AssessmentResult(verdict="UNRELATED", matched_canon_name=None, score=0.0)

        for neighbour in neighbours:
            verdict = _classify(body, neighbour)
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

        return AssessmentResult(verdict="UNRELATED", matched_canon_name=None, score=0.0)

    return assess
