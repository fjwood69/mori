"""API key authentication for mori-advisor.

Keys are configured via MORI_API_KEYS=name:secret,name:secret,...
where secret is a 32-byte hex string (64 chars).

Backward compat: if MORI_ADVISOR_API_KEY is set and MORI_API_KEYS is not,
the single key is loaded as {"legacy": <key>}.

If neither is set, the server runs in open mode with a startup warning.
"""

import hmac
import logging
import os
import secrets
from typing import Optional

logger = logging.getLogger(__name__)

_KEYS: dict[str, str] = {}


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
    """Load keys from environment. Call once at startup."""
    global _KEYS
    _KEYS = _load_keys()
    if not _KEYS:
        logger.warning(
            "MORI_API_KEYS not set — server is open to anyone on the network. "
            "Set MORI_API_KEYS=name:secret to enable authentication."
        )
    else:
        logger.info("Auth enabled: %d client key(s) configured (%s)", len(_KEYS), ", ".join(_KEYS))


def check_key(provided: Optional[str]) -> Optional[str]:
    """Validate a provided API key.

    Returns the client name if valid, None if invalid.
    Returns 'anonymous' if no keys are configured (open mode).
    """
    if not _KEYS:
        return "anonymous"
    if not provided:
        return None
    for name, secret in _KEYS.items():
        if hmac.compare_digest(provided, secret):
            return name
    return None


def generate_key() -> str:
    """Generate a new 32-byte hex API key secret."""
    return secrets.token_hex(32)


def configured_clients() -> list[str]:
    """Return list of configured client names (for smoke test / audit)."""
    return list(_KEYS.keys())
