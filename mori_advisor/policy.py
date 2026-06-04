"""Capability-scoped access policy for mori-advisor.

Roles and mode switch
---------------------
Roles (least → most privileged): read < write < dreamer

The mode switch is controlled by ``MORI_TD_MODE``:

  host (default)
      Trusted-dreamer logic falls through to the existing hostname-based check
      (``_is_trusted_client``).  No key-role enforcement.  Existing deployments
      that have not set ``MORI_TD_MODE`` or ``MORI_API_KEY_ROLES`` behave exactly
      as before.

  api
      The API key's role is the sole authority for write/approve operations.
      Hostname trust is NOT consulted for authorisation.

Open mode (no ``MORI_API_KEYS`` set) is unchanged — the switch only matters once
keys are configured.

Env vars
--------
  MORI_TD_MODE          host | api  (default: host)
  MORI_API_KEY_ROLES    name:role,name:role,...  (roles: read, write, dreamer)
  MORI_LOCAL_FULL_ACCESS  true | false  (default: false)
      When true, a missing actor (e.g. stdio transport with no ASGI request) is
      treated as having dreamer access.  Only set on fully-trusted single-user
      deployments.

Notes for future #14/#15 work
-------------------------------
``require_role("write")`` / ``require_role("dreamer")`` in api mode will fail
closed for any caller that has not set ``MORI_TD_MODE=api`` — so the new write
REST API and review queue can simply call ``require_role`` and the behaviour falls
out correctly depending on the operator's mode choice.  Audit logging is a planned
dependency of #15; add ``actor`` attribution there.
"""

from __future__ import annotations

import logging
import os
from contextvars import ContextVar
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── Role hierarchy ────────────────────────────────────────────────────────────

VALID_ROLES = {"read", "write", "dreamer"}
ROLE_LEVELS: dict[str, int] = {"read": 0, "write": 1, "dreamer": 2}

# ── Actor (the identity attached to the current request/task) ─────────────────


@dataclass(frozen=True)
class Actor:
    key_name: str
    role: str


# Module-level ContextVar holding the actor for the current async task chain.
# Set in ApiKeyMiddleware after a successful key validation; read in require_role.
# Default is None (no actor — unauthenticated / stdio transport).
current_actor: ContextVar[Actor | None] = ContextVar("current_actor", default=None)

# ── Mode switch ───────────────────────────────────────────────────────────────

_TD_MODE: str = os.environ.get("MORI_TD_MODE", "host").lower().strip()
if _TD_MODE not in ("host", "api"):
    logger.warning(
        "MORI_TD_MODE=%r is not a valid value (expected 'host' or 'api'); "
        "defaulting to 'host' for safety.",
        _TD_MODE,
    )
    _TD_MODE = "host"

_LOCAL_FULL_ACCESS: bool = os.environ.get("MORI_LOCAL_FULL_ACCESS", "false").lower() == "true"

# ── Role loading ──────────────────────────────────────────────────────────────

_ROLES: dict[str, str] = {}


def _load_roles() -> dict[str, str]:
    """Parse ``MORI_API_KEY_ROLES=name:role,...`` into a name→role dict.

    Rejects unknown role strings at startup (fail closed, loud log).
    Missing names default to 'read' at lookup time (see ``role_for``).
    """
    raw = os.environ.get("MORI_API_KEY_ROLES", "").strip()
    if not raw:
        return {}
    roles: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            logger.error(
                "MORI_API_KEY_ROLES entry %r is malformed (expected name:role) — skipped",
                entry,
            )
            continue
        name, _, role = entry.partition(":")
        name = name.strip()
        role = role.strip().lower()
        if not name:
            logger.error("MORI_API_KEY_ROLES entry %r has an empty name — skipped", entry)
            continue
        if role not in VALID_ROLES:
            logger.error(
                "MORI_API_KEY_ROLES entry %r has unknown role %r "
                "(valid: read, write, dreamer) — defaulting to 'read' for %r",
                entry,
                role,
                name,
            )
            role = "read"
        roles[name] = role
    return roles


def init_policy() -> None:
    """Load roles and log the active mode.  Call once at startup alongside ``init_auth``."""
    global _ROLES
    _ROLES = _load_roles()
    logger.info(
        "Policy: mode=%s, %d role(s) configured, local_full_access=%s",
        _TD_MODE,
        len(_ROLES),
        _LOCAL_FULL_ACCESS,
    )
    if _ROLES:
        for name, role in _ROLES.items():
            logger.debug("  role: %s → %s", name, role)


def role_for(key_name: str) -> str:
    """Return the role for a named key.  Defaults to 'read' if absent (fail closed)."""
    return _ROLES.get(key_name, "read")


# ── Permission denied exception ───────────────────────────────────────────────


class PermissionDenied(Exception):
    """Raised when the current actor lacks the required role.

    Carries a human-readable ``detail`` suitable for returning to the caller.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


# ── Core policy checks ────────────────────────────────────────────────────────


def _mode() -> str:
    """Return the active TD mode (allows override in tests via monkeypatch)."""
    return _TD_MODE


def can_read(actor: Actor | None) -> bool:  # noqa: ARG001
    """Read is always allowed — any authenticated actor (or open mode) may read."""
    return True


def can_write(actor: Actor | None) -> bool:
    """Write requires write or dreamer role in api mode; always allowed in host mode."""
    if _mode() == "host":
        return True  # legacy — existing hostname trust handles privileged ops
    if actor is None:
        return _LOCAL_FULL_ACCESS
    return ROLE_LEVELS.get(actor.role, 0) >= ROLE_LEVELS["write"]


def can_approve(actor: Actor | None) -> bool:
    """Approve/reject requires dreamer role in api mode; always allowed in host mode."""
    if _mode() == "host":
        return True  # legacy — existing hostname trust handles privileged ops
    if actor is None:
        return _LOCAL_FULL_ACCESS
    return ROLE_LEVELS.get(actor.role, 0) >= ROLE_LEVELS["dreamer"]


# ── require_role helper ───────────────────────────────────────────────────────


def require_role(min_role: str) -> None:
    """Assert that the current actor satisfies *min_role*.

    Reads ``current_actor`` from the ContextVar.  Raises ``PermissionDenied``
    if the actor is insufficiently privileged.

    In host mode this is a no-op (returns immediately) — backward compatible.
    In api mode, a None actor (no ASGI request / stdio transport) is denied
    unless ``MORI_LOCAL_FULL_ACCESS=true``.

    Args:
        min_role: One of 'read', 'write', 'dreamer'.
    """
    if min_role not in VALID_ROLES:
        raise ValueError(f"require_role: unknown role {min_role!r}")

    if _mode() == "host":
        return  # host mode — no enforcement; legacy hostname trust applies elsewhere

    actor = current_actor.get()

    if actor is None:
        if _LOCAL_FULL_ACCESS:
            return
        raise PermissionDenied(
            f"No authenticated actor — a valid API key with at least '{min_role}' role "
            "is required. If this is a local stdio deployment, set MORI_LOCAL_FULL_ACCESS=true."
        )

    actor_level = ROLE_LEVELS.get(actor.role, 0)
    required_level = ROLE_LEVELS[min_role]
    if actor_level < required_level:
        raise PermissionDenied(
            f"Key '{actor.key_name}' has role '{actor.role}' but '{min_role}' is required "
            f"for this operation."
        )
