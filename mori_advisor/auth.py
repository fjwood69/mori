"""API key authentication for mori-advisor.

Keys are configured via MORI_API_KEYS=name:secret,name:secret,...
where secret is a 32-byte hex string (64 chars).

v2.3.17 (H2): the server stores HASHES — ``name:sha256:<64 lowercase hex>``, the hex being
sha256(secret). Clients keep their plain secrets; the server hashes what is presented and compares
in constant time against every entry. In api mode the server refuses to start if any entry is plain
(naming the entry, never its value), so reading the env file yields no usable key.
``scripts/mori-hash-keys.py`` converts a table in place. Host mode (intake) still accepts plain.

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

    v2.3.17: in api mode, any plain (unhashed) entry refuses startup, naming the entry only."""
    global _KEYS
    _KEYS = _load_keys()
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
            logger.critical(
                "Refusing to start: %d plain API key entr%s in api mode (%s). v2.3.17 stores only "
                "hashes — run scripts/mori-hash-keys.py on the env file.",
                len(plain),
                "y" if len(plain) == 1 else "ies",
                ", ".join(plain),
            )
            raise SystemExit(
                f"plain API key in MORI_API_KEYS (name={', '.join(plain)}); v2.3.17 needs hashed "
                "entries (name:sha256:<hex>) in api mode."
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
        else:  # plain: host mode only (api mode refuses plain entries at startup)
            ok = hmac.compare_digest(provided, stored)
        if ok and match is None:
            match = name
    return match


def configured_clients() -> list[str]:
    """Return list of configured client names (for smoke test / audit)."""
    return list(_KEYS.keys())
