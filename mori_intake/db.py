"""asyncpg connection pool for mori-intake.

Pool is created once at app startup (via the FastAPI lifespan) and torn down
on shutdown.  All route handlers and the worker share the module-level pool.

Pool size is configurable via environment variables (SCALE-002):
    MORI_INTAKE_POOL_MIN  — minimum pool size (default 5)
    MORI_INTAKE_POOL_MAX  — maximum pool size (default 50)

At 100 concurrent agents, each at up to 120 req/min, peak concurrency can
reach 200+ in-flight requests.  The old hard-coded max_size=10 was a
bottleneck; 50 is a safer default for the stated 100-agent target.
"""

from __future__ import annotations

import logging
import os

import asyncpg

from mori_intake.config import INTAKE_DATABASE_URL

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

# Pool size defaults (SCALE-002).  Override via environment variables.
_DEFAULT_POOL_MIN = 5
_DEFAULT_POOL_MAX = 50


def _pool_min() -> int:
    try:
        return max(1, int(os.environ.get("MORI_INTAKE_POOL_MIN", _DEFAULT_POOL_MIN)))
    except (ValueError, TypeError):
        return _DEFAULT_POOL_MIN


def _pool_max() -> int:
    try:
        return max(_pool_min(), int(os.environ.get("MORI_INTAKE_POOL_MAX", _DEFAULT_POOL_MAX)))
    except (ValueError, TypeError):
        return _DEFAULT_POOL_MAX


async def create_pool() -> asyncpg.Pool:
    """Create and store the module-level connection pool.

    Called once from the FastAPI lifespan before migrations are applied.
    Idempotent: returns the existing pool if already created.

    Pool size is read from ``MORI_INTAKE_POOL_MIN`` / ``MORI_INTAKE_POOL_MAX``
    at startup time.  Defaults: min=5, max=50 (SCALE-002).
    """
    global _pool
    if _pool is not None:
        return _pool

    min_size = _pool_min()
    max_size = _pool_max()

    # ssl=False: asyncpg's SSL probe can fail in some environments
    # (systemd-resolved stub + asyncio thread executor).  Private-network
    # Postgres does not need TLS; enable via ?sslmode=require in the DSN if
    # required.
    _pool = await asyncpg.create_pool(
        INTAKE_DATABASE_URL,
        min_size=min_size,
        max_size=max_size,
        statement_cache_size=0,  # required for pgBouncer session mode
        ssl=False,
    )
    logger.info("intake asyncpg pool created (min=%d, max=%d)", min_size, max_size)
    return _pool


async def close_pool() -> None:
    """Close the pool on app shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("intake asyncpg pool closed")


def get_pool() -> asyncpg.Pool:
    """Return the active pool.  Raises if called before create_pool()."""
    if _pool is None:
        raise RuntimeError(
            "intake pool not initialised — call await db.create_pool() first "
            "(should happen inside the FastAPI lifespan)"
        )
    return _pool
