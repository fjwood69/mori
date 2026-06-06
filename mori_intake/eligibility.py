"""Eligibility gate for mori-intake — default-deny, pure function.

All policy is enforced server-side.  Never trust agent-supplied classification.

Namespace policy (from consult-b1000adc9f89)
---------------------------------------------
target == "memory":
    allow  stable_key prefix: learned-*, fact-*
    deny   stable_key prefix: session-*, scratch-*, temp-*
    deny   all other prefixes (default-deny)

target == "user":
    allow  ONLY: preference-*, accessibility-*
    hard-deny: psychology-*, health-*, mood-*  (and all other prefixes)

Unknown target → deny

Action policy
-------------
add / replace → proceed to proposition check
remove        → always deny ("retraction-requires-human")

Proposition classifier (cheap heuristic, no LLM)
-------------------------------------------------
Reject if:
    - body is empty
    - body has < 12 non-whitespace characters
    - body ends with "?" (question, not a claim)
    - body has < 3 whitespace-separated tokens (bare imperative fragment)

Accept otherwise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ── Type ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Decision:
    eligible: bool
    reason: str


# ── Namespace configuration ────────────────────────────────────────────────────

_MEMORY_ALLOW_PREFIXES: tuple[str, ...] = ("learned-", "fact-")
_MEMORY_DENY_PREFIXES: tuple[str, ...] = ("session-", "scratch-", "temp-")

_USER_ALLOW_PREFIXES: tuple[str, ...] = ("preference-", "accessibility-")
# Hard-deny list for user target: always rejected, even before the general default-deny.
_USER_HARD_DENY_PREFIXES: tuple[str, ...] = ("psychology-", "health-", "mood-")

# ── GOV-001: substring denylist (case-insensitive) ────────────────────────────
# Rejections apply to ANY stable_key that contains one of these substrings
# ANYWHERE — not just at the prefix.  This closes the bypass where an agent
# uses an allowlisted prefix to smuggle denied content (e.g.
# ``learned-psychology-user123`` or ``fact-health-record``).
#
# These terms cover the sensitive categories that must never be inferred or
# stored about users, plus credential/secret material.
_KEY_DENY_SUBSTRINGS: tuple[str, ...] = (
    "psychology",
    "health",
    "mood",
    "medical",
    "diagnos",
    "secret",
    "credential",
    "password",
    "passwd",
    "token",
    "apikey",
    "ssn",
)

# ── GOV-001: strict format constraint ─────────────────────────────────────────
# stable_key must be lowercase alphanumeric + hyphens only, start with a
# letter or digit, and be 1–128 characters long.
_STABLE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,127}$")

# ── Helpers ───────────────────────────────────────────────────────────────────

_DENY = Decision(eligible=False, reason="")  # placeholder — always overridden with a reason
_ACCEPT = Decision(eligible=True, reason="ok")


def _deny(reason: str) -> Decision:
    return Decision(eligible=False, reason=reason)


def _check_stable_key_format(stable_key: str) -> Decision | None:
    """Validate stable_key format and denylist substrings.

    Returns a ``_deny(...)`` Decision if the key is invalid, or ``None``
    when the key passes both checks (caller continues with namespace gate).

    Two checks (GOV-001):
    1. Format: ``^[a-z0-9][a-z0-9\\-]{0,127}$`` — rejects uppercase,
       dots, slashes, spaces, and keys longer than 128 characters.
    2. Substring denylist (case-insensitive): rejects keys that contain
       any sensitive term ANYWHERE, even after a valid allowlisted prefix.
       This closes the bypass ``learned-psychology-user123``.
    """
    if not _STABLE_KEY_RE.match(stable_key):
        return _deny("invalid-stable-key-format")

    lower = stable_key.lower()
    for term in _KEY_DENY_SUBSTRINGS:
        if term in lower:
            return _deny("namespace-not-allowlisted")

    return None  # passes both checks


# ── Public API ────────────────────────────────────────────────────────────────


def evaluate(target: str, action: str, stable_key: str, body: str) -> Decision:
    """Evaluate a submission for eligibility.

    Parameters
    ----------
    target:
        Submission target namespace ("memory" or "user").
    action:
        Requested action ("add", "replace", or "remove").
    stable_key:
        Namespaced key supplied by the agent (drives eligibility + idempotency).
    body:
        Raw content text of the proposal.

    Returns
    -------
    Decision
        ``eligible=True`` with ``reason="ok"`` on accept.
        ``eligible=False`` with a descriptive reason string on deny.
    """
    # ── 1. Action gate ────────────────────────────────────────────────────────
    # remove is NEVER auto-eligible: retraction requires a human in the loop.
    if action == "remove":
        return _deny("retraction-requires-human")

    # ── 1b. stable_key format + denylist (GOV-001) ────────────────────────────
    # Applied before the namespace gate so a smuggled key (e.g.
    # ``learned-psychology-user123``) is caught before prefix matching.
    key_check = _check_stable_key_format(stable_key)
    if key_check is not None:
        return key_check

    # ── 2. Namespace gate ─────────────────────────────────────────────────────
    if target == "memory":
        if any(stable_key.startswith(p) for p in _MEMORY_DENY_PREFIXES):
            return _deny("namespace-not-allowlisted")
        if not any(stable_key.startswith(p) for p in _MEMORY_ALLOW_PREFIXES):
            return _deny("namespace-not-allowlisted")

    elif target == "user":
        # Hard-deny takes priority — never accept inferred personal data.
        if any(stable_key.startswith(p) for p in _USER_HARD_DENY_PREFIXES):
            return _deny("namespace-not-allowlisted")
        if not any(stable_key.startswith(p) for p in _USER_ALLOW_PREFIXES):
            return _deny("namespace-not-allowlisted")

    else:
        # Unknown target → default-deny.
        return _deny("namespace-not-allowlisted")

    # ── 3. Proposition classifier ─────────────────────────────────────────────
    return _check_proposition(body)


def _check_proposition(body: str) -> Decision:
    """Cheap heuristic: is this body a genuine proposition (claim)?

    Keeps the policy in one place so it can be swapped for a real classifier
    in a later slice without touching the gate logic.
    """
    stripped = body.strip()

    if not stripped:
        return _deny("not-a-proposition")

    non_ws_count = sum(1 for ch in stripped if not ch.isspace())
    if non_ws_count < 12:
        return _deny("not-a-proposition")

    if stripped.endswith("?"):
        return _deny("not-a-proposition")

    tokens = stripped.split()
    if len(tokens) < 3:
        return _deny("not-a-proposition")

    return _ACCEPT
