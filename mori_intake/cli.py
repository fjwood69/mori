"""Manual CLI trigger for the B1 assess→promote pipeline.

Runs one full pass:
    1. ``assess_once()``  — transitions all pending candidates out of pending.
    2. ``drain_once()``   — promotes all queued rows to mori canon + lineage.

This replaces the dream as the enqueue trigger for B1 (MVV, deterministic,
no dream integration).  B3 will wire ``DreamPipeline`` to call ``assess_once``
and ``drain_once`` instead.

Usage
-----
    python -m mori_intake.cli [--assess-only | --drain-only]

Environment variables
---------------------
MORI_INTAKE_DATABASE_URL
    asyncpg DSN for the intake Postgres.  Required.

MORI_DATABASE_URL
    mori's own Postgres DSN.  If set, the mori store uses PostgresStore;
    otherwise falls back to SQLiteStore (MORI_ADVISOR_DATA path).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _build_mori_store():
    """Return a mori BaseStore instance configured from the environment."""
    from mori_advisor.store import get_store

    return get_store()


async def _run(assess_only: bool, drain_only: bool) -> None:
    import asyncpg

    from mori_intake.config import check_data_boundary
    from mori_intake.migrations import apply as apply_intake

    check_data_boundary()

    dsn = os.environ["MORI_INTAKE_DATABASE_URL"]
    pool = await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=5,
        statement_cache_size=0,
        ssl=False,
    )

    try:
        await apply_intake(pool)

        mori_store = _build_mori_store()
        # Bootstrap mori store if needed (SQLite: runs migrations synchronously).
        if hasattr(mori_store, "bootstrap"):
            bootstrap = mori_store.bootstrap()
            if asyncio.iscoroutine(bootstrap):
                await bootstrap

        if not drain_only:
            from mori_intake.assessor import assess_once

            assessed = await assess_once(pool)
            logger.info("cli: assess_once() processed %d candidate(s)", assessed)

        if not assess_only:
            from mori_intake.canon_writer import drain_once

            promoted = await drain_once(pool, mori_store)
            logger.info("cli: drain_once() committed %d promotion(s)", promoted)

    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one assess+promote pass (B1 manual trigger).")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--assess-only",
        action="store_true",
        help="Run assessor only (do not drain the promotion queue).",
    )
    group.add_argument(
        "--drain-only",
        action="store_true",
        help="Drain the promotion queue only (skip the assessor pass).",
    )
    args = parser.parse_args()

    if not os.environ.get("MORI_INTAKE_DATABASE_URL"):
        logger.critical("MORI_INTAKE_DATABASE_URL is not set — cannot run.")
        sys.exit(1)

    asyncio.run(_run(args.assess_only, args.drain_only))


if __name__ == "__main__":
    main()
