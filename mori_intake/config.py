"""Configuration for mori-intake.  All env reads live here — one place to audit.

Environment variables
---------------------
MORI_INTAKE_DATABASE_URL
    asyncpg DSN for the intake-dedicated Postgres.  Required for startup.
    MUST differ from MORI_DATABASE_URL (enforced at startup — data-boundary
    guard).

MORI_INTAKE_PORT
    TCP port to listen on (default 8971).

MORI_INTAKE_WORKER_INTERVAL
    Drain-loop poll interval in seconds (default 2).
"""

from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)

# ── Primary settings ──────────────────────────────────────────────────────────

INTAKE_DATABASE_URL: str = os.environ.get("MORI_INTAKE_DATABASE_URL", "")
INTAKE_PORT: int = int(os.environ.get("MORI_INTAKE_PORT", "8971"))
WORKER_INTERVAL: float = float(os.environ.get("MORI_INTAKE_WORKER_INTERVAL", "2"))

# ── Data-boundary guard ───────────────────────────────────────────────────────
# Refuse to start if MORI_INTAKE_DATABASE_URL matches MORI_DATABASE_URL.
# This is the config-layer enforcement of the architectural data boundary:
# agent writes must never land in mori's own Postgres.

_MORI_DATABASE_URL: str = os.environ.get("MORI_DATABASE_URL", "")


def check_data_boundary() -> None:
    """Raise SystemExit if intake is pointed at mori's own database.

    Call once at startup *before* connecting to the pool.
    """
    if not INTAKE_DATABASE_URL:
        logger.critical(
            "MORI_INTAKE_DATABASE_URL is not set — cannot start. "
            "Provide an asyncpg DSN for the intake-dedicated Postgres."
        )
        sys.exit(1)

    if _MORI_DATABASE_URL and INTAKE_DATABASE_URL == _MORI_DATABASE_URL:
        logger.critical(
            "MORI_INTAKE_DATABASE_URL == MORI_DATABASE_URL — refusing to start. "
            "The intake service MUST use a physically separate Postgres to maintain "
            "the data boundary between agent proposals and mori canon."
        )
        sys.exit(1)

    logger.info("Data-boundary guard: intake DSN is distinct from mori DSN.")
