"""asyncpg connection pool for mori-intake.

Pool is created once at app startup (via the FastAPI lifespan) and torn down
on shutdown.  All route handlers and the worker share the module-level pool.

Pool parameters mirror mori_advisor/store/postgres_store.py:
    min_size=2, max_size=10, statement_cache_size=0, ssl=False
"""

from __future__ import annotations

import logging

import asyncpg

from mori_intake.config import INTAKE_DATABASE_URL

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def create_pool() -> asyncpg.Pool:
    """Create and store the module-level connection pool.

    Called once from the FastAPI lifespan before migrations are applied.
    Idempotent: returns the existing pool if already created.
    """
    global _pool
    if _pool is not None:
        return _pool

    # ssl=False: asyncpg's SSL probe can fail in some environments
    # (systemd-resolved stub + asyncio thread executor).  Private-network
    # Postgres does not need TLS; enable via ?sslmode=require in the DSN if
    # required.
    _pool = await asyncpg.create_pool(
        INTAKE_DATABASE_URL,
        min_size=2,
        max_size=10,
        statement_cache_size=0,  # required for pgBouncer session mode
        ssl=False,
    )
    logger.info("intake asyncpg pool created (min=2, max=10)")
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
