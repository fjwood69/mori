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

# Verdicts the model may return — anything else collapses to UNRELATED.
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
    fetch_body:
        ``(name: str) -> str`` — returns the full body text of a single
        memory, or an empty string when the memory is not found.  No
        side-effects; no retrieval-count bump.
    """

    search: Callable[[str, int], list[dict]]
    fetch_body: Callable[[str], str]


def make_canon_reader_from_store(store) -> CanonReader:
    """Build a :class:`CanonReader` from a mori store instance.

    Extracts only the two read-only operations required by the assessor.
    Works with both ``SQLiteStore`` (delegates to ``MemoryStore``) and
    ``PostgresStore`` (synchronous wrapper around the async pool not
    available here — Postgres callers should build the reader manually with
    appropriate sync wrappers).

    For the SQLite path this is the idiomatic construction site; the
    ``cli.py`` wiring calls this helper so no store reference leaks into
    the assessor closure.
    """

    def _search(query: str, limit: int) -> list[dict]:
        # SQLiteStore delegates to MemoryStore.search_json (synchronous).
        # PostgresStore.search_json is async — the Postgres wiring must
        # supply its own sync-wrapped callable; this helper is SQLite-only.
        return store._mem.search_json(query=query, limit=limit)  # type: ignore[attr-defined]

    def _fetch_body(name: str) -> str:
        # BaseStore.read() returns a formatted markdown string; we want raw
        # body.  Use MemoryStore.get_memory() which returns a dict with a
        # 'body' key and does NOT bump retrieval_count.
        mem = store._mem.get_memory(name)  # type: ignore[attr-defined]
        if mem is None:
            return ""
        return mem.get("body") or ""

    return CanonReader(search=_search, fetch_body=_fetch_body)


def make_canon_assessor(
    reader: "CanonReader",
    llm_client: "BifrostClient",
    *,
    top_k: int = 5,
) -> Callable[[str, str], AssessmentResult]:
    """Return a synchronous ``assess(body, content_hash) -> AssessmentResult`` callable.

    Parameters
    ----------
    reader:
        A :class:`CanonReader` holding two read-only callables over mori
        canon.  The assessor has NO write path — ``reader`` exposes only
        ``search`` and ``fetch_body``.
    llm_client:
        A :class:`~mori_advisor.bifrost_client.BifrostClient` instance.  The
        ``"fast"`` VK (DeepSeek V4 Flash / equivalent cheap model) is used,
        matching the ``run_contradiction_scan`` path in the dream pipeline.
    top_k:
        Number of canon neighbours to retrieve and check.  Default 5.
    """

    def _search_canon(body: str) -> list[dict]:
        """Retrieve top-k canonical memories most relevant to *body*.

        Filters by ``tier == 'canonical'`` post-retrieval (MVV store may
        return working-tier rows).  Fetches ``top_k * 2`` so the canonical
        filter has headroom even when the store returns a mix of tiers.
        """
        try:
            raw = reader.search(body, top_k * 2)
            # Filter to canonical tier only.
            return [m for m in raw if m.get("tier") == "canonical"][:top_k]
        except Exception as exc:
            logger.warning("assess_model: canon search failed: %s", exc)
            return []

    def _classify(body: str, neighbour: dict) -> str:
        """Ask the fast model to classify *body* vs *neighbour*.

        Fetches the neighbour's FULL BODY via ``reader.fetch_body`` so the
        prompt receives complete content — not the truncated ``description``
        field that ``search`` returns.  Falls back to the description from
        the search result only if fetch_body returns empty (e.g. memory not
        found on a race between search and fetch).

        Reuses ``CONTRADICTION_SCAN_PROMPT`` verbatim from
        ``mori_advisor.utils`` — the same prompt the dream pipeline's
        contradiction scan uses.  Returns the raw verdict word (upper-case),
        or ``"UNRELATED"`` on any error / unrecognised token.
        """
        name = neighbour.get("name", "")
        try:
            full_body = reader.fetch_body(name)
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
            first = word.split()[0] if word else "UNRELATED"
            return first if first in _VALID_VERDICTS else "UNRELATED"
        except Exception as exc:
            logger.warning(
                "assess_model: model call failed for neighbour %s: %s",
                name,
                exc,
            )
            return "UNRELATED"

    def assess(body: str, content_hash: str) -> AssessmentResult:  # noqa: ARG001
        """Compare *body* against top-k canon neighbours; return the verdict.

        Steps
        -----
        1. Retrieve top-k canonical memories via text search.
        2. For each neighbour (highest-ranked first), fetch its full body
           and ask the fast model to classify candidate vs neighbour.
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
