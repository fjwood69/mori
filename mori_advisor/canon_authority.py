"""Canon authority (v2.3.16, the canon lock): only a reviewed decision changes canon.

A change that would create or alter a canonical row applies only with canon authority. Anything
else is stored as a proposal (``pending_writes``) and the row is unchanged. Board ruling,
7 Nov 2026 (dotfiles ``docs/mori-planning/canon-lock-records/BOARD-RULING-2026-11-07.md``).

Canon authority comes from exactly two places:

* the :class:`CanonAuthority` token, which only ``approve()`` (both backends) and the standards
  import construct. "Private" is a naming convention in Python, so a test pins every place the
  token is created and fails if a new one appears (``tests/test_canon_lock_token.py``);
* an mcp/rest request whose AUTHENTICATED key has the ``dreamer`` role in api mode. The identity is
  read from ``current_actor`` (set by the auth middleware), never from a provenance string — a
  provenance only names the CLASS of the caller. Where the ContextVar does not propagate (a cron
  process, a worker thread) there is no actor and therefore no authority: the change is queued.

``MORI_LOCAL_FULL_ACCESS=true`` (api mode, no authenticated actor) is a documented exception, as in
:func:`mori_advisor.policy.require_role`.

Host mode has no roles, so no request holds authority there: canonical changes are queued, and with
role checks off any local caller can approve. That mode is not described as the same lock.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

# Proposal caps (board ruling, answer 2).
REQUEST_KEY_OPEN_CAP = 20  # open proposals per request key
INGESTION_OPEN_CAP = 50  # open proposals for the proposer "ingestion"
CHAIN_CAP = 10  # links in one supersede chain

# The closed status of a superseded proposal (immutable proposals, hybrid supersede).
SUPERSEDED_STATUS = "superseded-by-proposal"

OPERATIONS = frozenset({"write", "rollback", "delete"})
REQUEST_ACTORS = frozenset({"mcp", "rest"})
STORED_TIERS = frozenset({"ephemeral", "working", "canonical"})


class CanonAuthority:
    """The in-process token that lets a change to a canonical row apply directly.

    Constructed ONLY by ``MemoryStore.approve``, ``PostgresStore.approve`` and
    ``main.import_standards``. A test pins those sites; a new one fails it.
    """

    __slots__ = ("reason",)

    def __init__(self, reason: str) -> None:
        if not reason or not isinstance(reason, str):
            raise ValueError("CanonAuthority needs a reason for the audit ledger")
        self.reason = reason

    def __repr__(self) -> str:
        return f"CanonAuthority({self.reason!r})"


def _current_actor() -> Any:
    from mori_advisor.policy import current_actor

    return current_actor.get()


def has_canon_authority(provenance: Any, authority: Any = None) -> bool:
    """True if this change to canon may apply directly (see the module docstring)."""
    if isinstance(authority, CanonAuthority):
        return True
    if getattr(provenance, "actor", None) not in REQUEST_ACTORS:
        return False
    from mori_advisor import policy

    if policy._mode() != "api":
        return False
    actor = _current_actor()
    if actor is None:
        return bool(policy._LOCAL_FULL_ACCESS)
    return policy.role_for(actor.key_name) == "dreamer"


def proposer_for(provenance: Any) -> str:
    """Who a proposal belongs to. A request's proposer is the AUTHENTICATED key, never a string
    the caller passed; an internal writer names itself through its provenance constant."""
    if getattr(provenance, "actor", None) in REQUEST_ACTORS:
        actor = _current_actor()
        return actor.key_name if actor is not None else "local"
    return getattr(provenance, "ledger_actor", "") or "legacy"


def open_cap_for(proposer: str, is_request: bool) -> int | None:
    """The open-proposal cap for *proposer*: 20 per request key, 50 for ``ingestion``. The msg
    daemon keeps its own caps (20 per sender, 100 total); other internal writers are uncapped."""
    if is_request:
        return REQUEST_KEY_OPEN_CAP
    if proposer == "ingestion":
        return INGESTION_OPEN_CAP
    return None


def normalise_tier(tier: str | None) -> str:
    """There is no ``standard`` tier: it means canonical (board ruling — normalised when queued,
    never allowed to fall through to working)."""
    t = (tier or "").strip().lower()
    if t == "standard":
        return "canonical"
    return t


def _tags_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else []
        except ValueError:
            return [raw]
    if isinstance(raw, (list, tuple)):
        return sorted(str(t) for t in raw)
    return [str(raw)]


def target_state_digest(row: dict[str, Any] | None) -> str:
    """A digest of the target's ACTIVE row at proposal time, or ``absent``.

    Covers the incarnation (row id), every reviewable field, the tier, the protection flag, the
    tombstone and ``superseded_by`` — so a delete-then-recreate, a protect flip, an intermediate
    write or a supersession all make an approval ``stale-proposal``. ``updated_at`` is NOT used: it
    has one-second resolution on SQLite.
    """
    if not row:
        return "absent"
    payload = {
        "id": row.get("id"),
        "title": row.get("title") or "",
        "description": row.get("description") or "",
        "type": row.get("type") or "",
        "body": row.get("body") or "",
        "tags": _tags_list(row.get("tags")),
        "tier": row.get("tier") or "",
        "protected": bool(row.get("protected")),
        "deleted_at": str(row.get("deleted_at") or ""),
        "superseded_by": str(row.get("superseded_by") or ""),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def proposal_key(fields: dict[str, Any]) -> tuple[Any, ...]:
    """What makes two proposals content-equal (a re-proposal of the same content is a no-op)."""
    return (
        fields.get("operation") or "write",
        normalise_tier(fields.get("tier")),
        fields.get("title") or "",
        fields.get("description") or "",
        fields.get("type") or "",
        fields.get("body") or "",
        tuple(_tags_list(fields.get("tags"))),
        fields.get("target_version_id"),
    )


@dataclass(frozen=True)
class ProposalOutcome:
    """The structured result of the proposal sink (consult C3, P1-5)."""

    kind: str  # inserted | superseded | noop | refused | capped | chain-capped
    pending_id: int | None
    reason: str

    @property
    def queued(self) -> bool:
        return self.kind in ("inserted", "superseded", "noop")


def outcome_message(name: str, outcome: ProposalOutcome) -> str:
    return outcome.reason or f"Memory '{name}': {outcome.kind}"
