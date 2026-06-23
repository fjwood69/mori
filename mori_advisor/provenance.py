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
import os
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
    """Identity + origin + disposition of a single write, threaded to ``store.write``.

    Two axes, board-ratified (B + C, 2026-06-22):
      actor:        the CLASS (a KNOWN_ACTORS key) — drives capability / tier authorization
                    (Phase 2). A small, stable table; the audit never keys on it.
      actor_detail: the SPECIFIC principal for the audit ledger (WHO — e.g. ``nuc15pro``,
                    ``hermes``, a device/session key). REQUIRED for machine-scoped actors
                    (``mcp``/``rest``/``import``/device-bound); MAY be "" for singleton writers
                    (``dreamer``/``governed-promotion``/``init``) — the ledger then falls back to
                    ``actor`` so queries stay uniform without inventing fake specificity (GP).
      source:       stable ``module:function`` origin (no prose / PII — it lands in the log).
      op:           caller DISPOSITION recorded verbatim in the ledger ``op`` column
                    (``write`` | ``propose_new`` | ``propose_pending`` | ``update_working`` |
                    ``import`` | …). The store records it; it does NOT interpret it — the caller
                    supplies the semantic, the one door logs it universally (no second home).
      request_id:   optional idempotency / correlation id.
    """

    actor: str
    actor_detail: str = ""
    source: str = ""
    op: str = "write"
    request_id: str | None = None

    @property
    def ledger_actor(self) -> str:
        """Principal recorded in the audit ledger — the specific key if known, else the class."""
        return self.actor_detail or self.actor

    def is_known(self) -> bool:
        return self.actor in KNOWN_ACTORS

    def may_target(self, tier: str) -> bool:
        """True if this actor is authorized to target *tier* (Phase 2 enforcement)."""
        return tier in ACTOR_TIER_CAPS.get(self.actor, frozenset())


# Convenience constants for the common internal singleton writers (actor_detail falls back
# to actor in the ledger — GP's "may equal actor for singletons" invariant).
DREAMER = Provenance(actor="dreamer", source="dream.py:_write_memory")
GOVERNED_PROMOTION = Provenance(actor="governed-promotion", source="canon_writer")
INIT = Provenance(actor="init", source="import_standards", op="import")
SYSTEM = Provenance(actor="system", source="internal")
LEGACY = Provenance(actor="legacy", source="unmigrated-caller")


def request_provenance(
    actor_class: str,
    actor_name: str | None,
    source: str,
    op: str = "write",
    request_id: str | None = None,
) -> Provenance:
    """Build a Provenance at the request boundary (MCP / REST handler).

    actor_class: the capability CLASS — ``mcp`` for MCP tools, ``rest`` for REST endpoints.
    actor_name:  the resolved specific key (``current_actor.key_name``) for the ledger (WHO).
    op:          the caller's disposition (``write``/``propose_new``/``update_working``/…).
    """
    cls = actor_class if actor_class in KNOWN_ACTORS else "mcp"
    name = (actor_name or "").strip()
    return Provenance(
        actor=cls, actor_detail=name or cls, source=source, op=op, request_id=request_id
    )


# --- The write-authorization pipeline stages (Phase 2) -----------------------
# Pure policy, the SINGLE authority both backends consume — SQLite (memory_store)
# and Postgres (postgres_store) are sinks that call these; the decision lives here.


def validate_provenance(provenance: Provenance, name: str, log) -> None:
    """Stage 1 — provenance validation (audit-only, never blocks).

    Logs unmigrated (``legacy``) and unknown actors so the migration tail stays visible.
    Sentinel actors are tolerated until every caller threads a real Provenance.
    """
    if provenance.actor == "legacy":
        log.warning(
            "WRITE-AUDIT actor=legacy (unmigrated caller) name=%s — thread Provenance", name
        )
    elif not provenance.is_known():
        log.warning(
            "WRITE-AUDIT actor=%r is UNKNOWN (not in KNOWN_ACTORS) name=%s", provenance.actor, name
        )


def authorize_tier(provenance: Provenance, intended_tier: str) -> tuple[bool, str]:
    """Stage 2 — tier-target authorization. Returns ``(authorized, reason)``.

    The cap is inferred from the *actor* (``may_target``), never trusting a handler-supplied
    tier blindly (GLM#4) — the #5 fix. Callers in AUDIT-MODE log the reason but do not act;
    enforcement lands behind ``MORI_TIER_ENFORCE`` (step 3).
    """
    if provenance.may_target(intended_tier):
        return True, ""
    caps = sorted(ACTOR_TIER_CAPS.get(provenance.actor, frozenset()))
    return False, (
        f"actor '{provenance.actor}' may not target tier '{intended_tier}' (caps: {caps})"
    )


def tier_enforce_mode(actor: str) -> str:
    """Resolve the tier-enforcement mode for *actor* from ``$MORI_TIER_ENFORCE``.

    Read ONCE per write (the store snapshots it at txn-start — never re-read mid-write; GLM).
      unset / ``audit``     -> ``audit``   (observe only; the default — zero behaviour change)
      ``enforce``           -> ``enforce``  (all actors)
      ``enforce:mcp,rest``  -> ``enforce`` for the listed actors, ``audit`` for the rest
                               (the board's per-actor flip — flip mcp/rest first, ingestion later)
    Unrecognised values fail SAFE to ``audit``.
    """
    raw = (os.environ.get("MORI_TIER_ENFORCE") or "").strip().lower()
    if not raw or raw == "audit":
        return "audit"
    if raw == "enforce":
        return "enforce"
    if raw.startswith("enforce:"):
        allow = {a.strip() for a in raw[len("enforce:") :].split(",") if a.strip()}
        return "enforce" if actor in allow else "audit"
    return "audit"


def tier_decision(provenance: Provenance, intended_tier: str) -> tuple[bool, str, str, str]:
    """The full tier-authorization decision, snapshot once at ``store.write`` txn-start.

    Returns ``(reject, decision, mode, reason)``:
      reject:   ``True`` -> the store MUST reject (enforce mode + unauthorized tier); persists nothing.
      decision: ``allowed`` | ``would_block`` (unauthorized but audit-mode) | ``rejected`` — metric cut.
      mode:     ``audit`` | ``enforce`` for this actor.
      reason:   the would-block reason (``""`` when allowed).

    R2 (board-ratified): an unauthorized tier target is HARD-REJECTED on both backends — no
    downgrade-to-pending (that lane is for the name/tag protection path only).
    """
    ok, reason = authorize_tier(provenance, intended_tier)
    mode = tier_enforce_mode(provenance.actor)
    if ok:
        return False, "allowed", mode, ""
    if mode == "enforce":
        return True, "rejected", mode, reason
    return False, "would_block", mode, reason
