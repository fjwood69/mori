"""API key authentication for mori-advisor.

Keys are configured via MORI_API_KEYS=name:secret,name:secret,...
where secret is a 32-byte hex string (64 chars).

v2.3.17: an entry MAY be stored as a hash — ``name:sha256:<64 lowercase hex>``, the hex being
sha256(secret). Clients keep their plain secrets; the server hashes what is presented and compares in
constant time against every entry. Plain entries are accepted (with a warning in api mode); hashing is
recommended for production deployments. ``scripts/mori-hash-keys.py`` converts a table in place — an
operator's choice, never run by the deploy, and it leaves the FIRST entry plain by default: the GCE CD
template's contract check reads that entry's secret from the env file.

Backward compat: if MORI_ADVISOR_API_KEY is set and MORI_API_KEYS is not,
the single key is loaded as {"legacy": <key>}.

If neither is set, the server runs in open mode with a startup warning.
"""

import hashlib
import hmac
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

_KEYS: dict[str, str] = {}

HASH_PREFIX = "sha256:"
_HASHED = re.compile(r"sha256:[0-9a-f]{64}")


def is_hashed(stored: str) -> bool:
    return bool(_HASHED.fullmatch(stored or ""))


def hash_secret(secret: str) -> str:
    """The stored form of *secret*: ``sha256:<64 lowercase hex>``."""
    return HASH_PREFIX + hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _load_keys() -> dict[str, str]:
    raw = os.environ.get("MORI_API_KEYS", "").strip()
    if not raw:
        legacy = os.environ.get("MORI_ADVISOR_API_KEY", "").strip()
        return {"legacy": legacy} if legacy else {}
    keys: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" in entry:
            name, secret = entry.split(":", 1)
            name = name.strip()
            secret = secret.strip()
            if name and secret:
                keys[name] = secret
    return keys


def init_auth() -> None:
    """Load keys from environment. Call once at startup.

    v2.3.17: hashed entries are supported; a plain entry in api mode logs a warning (never refuses startup)."""
    global _KEYS
    _KEYS = _load_keys()
    # An entry that LOOKS hashed but isn't 64 lowercase hex (a typo, upper case) would be compared as a literal
    # plain secret that no client presents — a silently dead key. Say so (names only).
    lookalike = sorted(
        name
        for name, stored in _KEYS.items()
        if stored.lower().startswith(HASH_PREFIX) and not is_hashed(stored)
    )
    if lookalike:
        logger.warning(
            "API key entr%s %s start%s with 'sha256:' but %s not 64 lowercase hex characters: treated as a "
            "plain secret, which no client presents. Fix the entry.",
            "y" if len(lookalike) == 1 else "ies",
            ", ".join(lookalike),
            "s" if len(lookalike) == 1 else "",
            "is" if len(lookalike) == 1 else "are",
        )
    if not _KEYS:
        logger.warning(
            "MORI_API_KEYS not set — server is open to anyone on the network. "
            "Set MORI_API_KEYS=name:secret to enable authentication."
        )
    else:
        logger.info("Auth enabled: %d client key(s) configured (%s)", len(_KEYS), ", ".join(_KEYS))

    # Initialise the capability policy alongside auth so startup logging is ordered.
    from mori_advisor import policy
    from mori_advisor.policy import init_policy

    init_policy()
    if policy._TD_MODE == "api":
        plain = sorted(name for name, stored in _KEYS.items() if not is_hashed(stored))
        if plain:
            # Hashing is optional (v2.3.17): plain entries work; hashes are recommended for production
            # deployments (scripts/mori-hash-keys.py, which keeps the first entry plain for the deploy).
            logger.warning(
                "%d plain API key entr%s (%s): accepted. Storing hashes (name:sha256:<hex>, "
                "scripts/mori-hash-keys.py) is recommended for production deployments.",
                len(plain),
                "y" if len(plain) == 1 else "ies",
                ", ".join(plain),
            )


def check_key(provided: Optional[str]) -> Optional[str]:
    """Validate a provided API key.

    Returns the client name if valid, None if invalid.
    Returns 'anonymous' if no keys are configured (open mode).
    """
    if not _KEYS:
        return "anonymous"
    if not provided:
        return None
    presented_hash = hash_secret(provided)
    match: Optional[str] = None
    # Every entry is compared (no early return), so timing doesn't depend on the entry's position.
    for name, stored in _KEYS.items():
        if is_hashed(stored):
            ok = hmac.compare_digest(presented_hash, stored)
        else:  # plain (supported; hashing is recommended for production deployments)
            ok = hmac.compare_digest(provided, stored)
        if ok and match is None:
            match = name
    return match


def configured_clients() -> list[str]:
    """Return list of configured client names (for smoke test / audit)."""
    return list(_KEYS.keys())
