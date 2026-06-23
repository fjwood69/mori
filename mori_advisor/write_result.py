"""WriteResult — structured outcome of a ``store.write`` (chokepoint Phase 2).

The chokepoint authorizes the tier target by ``provenance.actor`` (``ACTOR_TIER_CAPS``).
An unauthorized canonical write is DOWNGRADED to the pending/review queue — not lost, not
silently accepted. ``write()`` therefore can no longer just return a name string; it returns
a ``WriteResult`` whose ``disposition`` the caller must reckon with.

Backward-compat (board-ratified, board-2026-06-23): the public ``write()`` is a thin adapter
that returns ``result.memory_name`` ONLY when ACCEPTED, and otherwise raises
``TierDowngradedError`` (carrying the full result) — so a caller that assumed a canonical row
exists fails LOUD instead of silently reading a ghost. A real dataclass (NOT a str-subclass:
that silently drops ``disposition`` on serialization) + an explicit ``to_dict()`` for response
models. Callers that legitimately want the pending lane opt in via ``allow_downgrade=True``.

PURE module: imports nothing from mori (keeps the store low-level — see provenance.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Disposition(str, Enum):
    ACCEPTED = "accepted"  # written at the intended tier
    DOWNGRADED_TO_PENDING = "downgraded_to_pending"  # not authorized for tier → queued for review
    REJECTED = "rejected"  # malformed / missing provenance — nothing written


@dataclass(frozen=True)
class WriteResult:
    memory_name: str
    intended_tier: str
    stored_tier: str  # where it actually landed ("pending" when downgraded; "" when rejected)
    disposition: Disposition
    audit_id: int | None = None
    anatomy_verdict: str | None = None
    pending_id: int | None = None  # set when DOWNGRADED_TO_PENDING
    reason: str = ""

    @property
    def accepted(self) -> bool:
        return self.disposition is Disposition.ACCEPTED

    def require_accepted(self) -> str:
        """Return the name if ACCEPTED; otherwise raise — for callers that need the row to exist
        at the intended tier (read-after-write integrity)."""
        if not self.accepted:
            raise TierDowngradedError(self)
        return self.memory_name

    def to_dict(self) -> dict:
        """Explicit serialization — disposition must survive JSON/MCP/Pydantic boundaries
        (a str-subclass shim drops it silently; GLM#5)."""
        return {
            "memory_name": self.memory_name,
            "intended_tier": self.intended_tier,
            "stored_tier": self.stored_tier,
            "disposition": self.disposition.value,
            "audit_id": self.audit_id,
            "anatomy_verdict": self.anatomy_verdict,
            "pending_id": self.pending_id,
            "reason": self.reason,
        }


class TierDowngradedError(Exception):
    """Raised by the ``write()`` adapter when a write was not ACCEPTED (downgraded/rejected).
    Carries the full ``WriteResult`` so the caller can inspect what was intercepted without
    parsing strings (GLM#1)."""

    def __init__(self, result: WriteResult) -> None:
        self.result = result
        super().__init__(
            f"write '{result.memory_name}' to tier '{result.intended_tier}' "
            f"not authorized → {result.disposition.value}"
            + (f" ({result.reason})" if result.reason else "")
        )


def accepted(memory_name: str, tier: str, audit_id: int | None = None) -> WriteResult:
    """Convenience constructor for the ACCEPTED path (the only disposition until enforcement)."""
    return WriteResult(
        memory_name=memory_name,
        intended_tier=tier,
        stored_tier=tier,
        disposition=Disposition.ACCEPTED,
        audit_id=audit_id,
    )
