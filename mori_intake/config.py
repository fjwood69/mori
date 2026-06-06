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

MORI_INTAKE_RATE_LIMIT_PER_MIN
    Maximum POST /intake/submissions requests per minute per API-key name
    (default 120).  Set to 0 to disable.  Applies to writes only — GET paths
    are never rate-limited.

MORI_INTAKE_HOST
    Bind address for the uvicorn server (default 0.0.0.0).  On the GCE VM (no
    public IP, Tailscale-only ingress) 0.0.0.0 is reachable only over Tailscale
    + localhost; set to a specific interface IP to bind tighter.

MORI_INTAKE_PENDING_TTL_HOURS
    Pending-candidate time-to-live in hours (default 168 = 7 days).  Pending
    candidates idle (no reinforcement) beyond this are reaped by the worker's
    TTL purge (P3).  Set to 0 to disable the purge.

MORI_INTAKE_PURGE_INTERVAL_SEC
    How often (seconds) the worker runs the TTL purge (default 3600).

MORI_INTAKE_MAX_CONTENT_BYTES
    Maximum byte length of a submission ``content`` field (default 65536).
    Larger payloads are rejected 422 before any DB work.  Set to 0 to disable.
"""

from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)

# ── Primary settings ──────────────────────────────────────────────────────────

INTAKE_DATABASE_URL: str = os.environ.get("MORI_INTAKE_DATABASE_URL", "")
INTAKE_PORT: int = int(os.environ.get("MORI_INTAKE_PORT", "8971"))
INTAKE_HOST: str = os.environ.get("MORI_INTAKE_HOST", "0.0.0.0")
WORKER_INTERVAL: float = float(os.environ.get("MORI_INTAKE_WORKER_INTERVAL", "2"))

# ── P3 — pending-candidate TTL purge ──────────────────────────────────────────
PENDING_TTL_HOURS: float = float(os.environ.get("MORI_INTAKE_PENDING_TTL_HOURS", "168"))
PURGE_INTERVAL_SEC: float = float(os.environ.get("MORI_INTAKE_PURGE_INTERVAL_SEC", "3600"))

# ── Submission payload guard ──────────────────────────────────────────────────
MAX_CONTENT_BYTES: int = int(os.environ.get("MORI_INTAKE_MAX_CONTENT_BYTES", "65536"))

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
