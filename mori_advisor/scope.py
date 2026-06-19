"""Flat, deterministic scope filter — the H2 scope router's hot-path primitive.

The keep/drop decision is pure tag **set-membership**: NO graph traversal, NO
embedding similarity, NO model. A memory's scope map declares "valid in these
contexts"; a memory is in-scope if its tags intersect the context tags
(``match="any"``) or are a subset of them (``match="all"``). An absent/empty
scope map = **global** = always in scope.

Deliberately OUT of this primitive (they belong to the resolver, client-side):
  * CWD/worktree -> context-tags resolution (core never learns filesystem paths);
  * legacy-tag -> scope-map compilation (``project:`` / ``scope:global`` /
    ``legacy:type-global``) for un-migrated rows.

This module is intentionally tiny and dependency-free so it can be called on the
retrieval hot path and unit-tested exhaustively.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

VALID_MATCH = ("any", "all")


@dataclass(frozen=True)
class ScopeMap:
    """A per-memory scope: a set of tags + a match policy.

    Empty ``tags`` == global (valid everywhere). Frozen + hashable so it can be
    cached and used as a dict key.
    """

    tags: frozenset[str] = frozenset()
    match: str = "any"

    @property
    def is_global(self) -> bool:
        return not self.tags

    @classmethod
    def parse(cls, raw: object) -> "ScopeMap":
        """Parse a stored scope value (JSON str | dict | None) into a ScopeMap.

        Malformed or absent input -> **global**, mirroring the existing
        ``_parse_tags`` fail-open convention (an un-scoped memory is global today,
        so a fresh ``scope`` column that is NULL/empty must not change behaviour).
        The resolver supplies a *derived* scope for legacy rows, so this default
        is only reached for genuinely un-scoped data.
        """
        if raw is None or raw == "":
            return cls()
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                logger.warning("scope: malformed JSON, treating as global: %r", raw[:80])
                return cls()
        if not isinstance(raw, dict):
            return cls()
        tags = raw.get("tags") or []
        if not isinstance(tags, list):
            return cls()
        match = raw.get("match", "any")
        if match not in VALID_MATCH:
            match = "any"
        return cls(tags=frozenset(str(t) for t in tags), match=match)


def in_scope(scope: ScopeMap, context_tags: object) -> bool:
    """Flat set-membership decision — the whole hot path.

    Global scope -> always in. Otherwise ``match="any"`` keeps the memory if any
    scope tag is present in the context; ``match="all"`` requires every scope tag
    to be present. Pure, total, side-effect-free.
    """
    if scope.is_global:
        return True
    ctx = context_tags if isinstance(context_tags, (set, frozenset)) else set(context_tags)
    if scope.match == "all":
        return scope.tags <= ctx
    return bool(scope.tags & ctx)
