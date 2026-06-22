"""Write provenance — identity threaded to the ``store.write`` chokepoint.

The board-ratified *identity-aware chokepoint* (2026-06-22): every write carries a
structured :class:`Provenance` so the one door (``store.write``) can audit *universally*
and (Phase 2) authorize *tier targets* — instead of per-caller audit/protection that
drifts. v2.2.26 added ``write_audit`` at the callers (memory_write, import_standards),
re-forming the exact per-caller drift that orphaned the completeness gate; the dreamer —
the bulk writer — was left unaudited. This module is the fix's foundation.

PURE module: imports nothing from mori. ``store`` imports this; this imports nothing of
mori — which keeps the store low-level and avoids a ``store -> policy`` circular import
(the request boundary reads ``current_actor`` and builds a Provenance; the store never
reads the ContextVar — its propagation is unreliable across the dual backend, the ``_conn``
txn path, the separate cron process, and ``to_thread`` copy-semantics).

Phasing (see identity-aware-chokepoint-epic.md):
  - Phase 1 (this commit): thread Provenance + universal in-transaction audit. Log-only
    for tier capability — nothing is blocked.
  - Phase 2: enforce ``ACTOR_TIER_CAPS`` (canonical-eligibility), remove ``_skip_protection``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


def content_hash(body: str) -> str:
    """Stable short content hash for the audit ledger (matches the legacy _write_audit)."""
    return hashlib.sha256((body or "").strip().encode("utf-8", errors="replace")).hexdigest()[:16]


# Known actors — validated at the chokepoint so a *raw* ``store.write`` caller cannot
# spoof ``actor="dreamer"``. Request handlers derive the actor from ``current_actor``;
# internal callers are trusted code passing their own constant.
KNOWN_ACTORS: frozenset[str] = frozenset(
    {
        "dreamer",  # dream.py:_write_memory — the bulk writer
        "governed-promotion",  # canon_writer — intake promotion
        "init",  # standards importer / bootstrap
        "mcp",  # MCP tool write (request-bound)
        "rest",  # REST API write (request-bound)
        "ingestion",  # ingestion pipeline
        "msg",  # msg daemon
        "import",  # memory import / restore
        "system",  # internal / migration / rollback / tests
        "legacy",  # migration sentinel — logs loud, never authorized for canonical
    }
)

# Tier-target capability table. Phase 2 ENFORCES this; Phase 1 only LOGS a would-block.
# Which tiers each actor may target. Canonical is restricted to the trusted writers —
# the dreamer is working-only by design, and MCP/REST reach canonical only via review.
_ALL_TIERS: frozenset[str] = frozenset({"working", "canonical", "ephemeral"})
ACTOR_TIER_CAPS: dict[str, frozenset[str]] = {
    "dreamer": frozenset({"working", "ephemeral"}),
    "governed-promotion": _ALL_TIERS,
    "init": _ALL_TIERS,
    "mcp": frozenset({"working", "ephemeral"}),
    "rest": frozenset({"working", "ephemeral"}),
    "ingestion": frozenset({"working", "ephemeral"}),
    "msg": frozenset({"working", "ephemeral"}),
    "import": _ALL_TIERS,
    "system": _ALL_TIERS,
    "legacy": frozenset({"working", "ephemeral"}),
}


@dataclass(frozen=True)
class Provenance:
    """Identity + origin of a single write, threaded to ``store.write``.

    actor:      a KNOWN_ACTORS key (validated at the chokepoint).
    source:     stable ``module:function`` origin (no prose / PII — it lands in the audit log).
    request_id: optional idempotency / correlation id (request-bound writes).
    """

    actor: str
    source: str = ""
    request_id: str | None = None

    def is_known(self) -> bool:
        return self.actor in KNOWN_ACTORS

    def may_target(self, tier: str) -> bool:
        """True if this actor is authorized to target *tier* (Phase 2 enforcement)."""
        return tier in ACTOR_TIER_CAPS.get(self.actor, frozenset())


# Convenience constants for the common internal (non-request) writers.
DREAMER = Provenance(actor="dreamer", source="dream.py:_write_memory")
GOVERNED_PROMOTION = Provenance(actor="governed-promotion", source="canon_writer")
INIT = Provenance(actor="init", source="bootstrap")
SYSTEM = Provenance(actor="system", source="internal")
LEGACY = Provenance(actor="legacy", source="unmigrated-caller")


def from_actor_name(
    actor_name: str | None, source: str, request_id: str | None = None
) -> Provenance:
    """Build a Provenance at the request boundary from a resolved actor key name.

    ``actor_name`` is the policy actor's ``key_name`` (or None for an unauthenticated /
    stdio call). Unknown / None names map to ``mcp`` by default for request writes — they
    are still audited; tier authorization (Phase 2) is what gates canonical, not this.
    """
    name = (actor_name or "").strip().lower()
    actor = name if name in KNOWN_ACTORS else "mcp"
    return Provenance(actor=actor, source=source, request_id=request_id)
