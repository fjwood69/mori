"""Manual CLI trigger for the B1/B2 assess→promote pipeline.

Runs one full pass:
    1. ``assess_once()``  — transitions all pending candidates out of pending.
    2. ``drain_once()``   — promotes all queued rows to mori canon + lineage.

This replaces the dream as the enqueue trigger for B1/B2 (MVV, deterministic,
no dream integration).  B3 will wire ``DreamPipeline`` to call ``assess_once``
and ``drain_once`` instead.

Usage
-----
    python -m mori_intake.cli [--assess-only | --drain-only] [--real-assessor]

Environment variables
---------------------
MORI_INTAKE_DATABASE_URL
    asyncpg DSN for the intake Postgres.  Required.

MORI_DATABASE_URL
    mori's own Postgres DSN.  If set, the mori store uses PostgresStore;
    otherwise falls back to SQLiteStore (MORI_ADVISOR_DATA path).

MORI_BASE_URL
    Bifrost base URL used by the real assessor (default http://localhost:8787).
    Only relevant when ``--real-assessor`` is passed.
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


def _build_real_assess_fn(mori_store):
    """Build and return the real fast-model assess callable (B2).

    Constructs a :class:`~mori_advisor.bifrost_client.BifrostClient` from
    environment variables (``MORI_BASE_URL``, ``MORI_PROVIDER_MODE``, etc.)
    and wraps it with :func:`~mori_intake.assess_model.make_canon_assessor`.

    The assessor receives a read-only :class:`~mori_intake.assess_model.CanonReader`
    built from the store's read-only methods — it has NO write path to canon.

    Only called when ``--real-assessor`` is passed.  Tests never exercise this
    path — they inject their own mock.
    """
    from mori_advisor.bifrost_client import BifrostClient
    from mori_intake.assess_model import CanonReader, make_canon_assessor

    client = BifrostClient()
    logger.info(
        "cli: real assessor using BifrostClient (mode=%s, base_url=%s)",
        client.mode,
        client.base_url,
    )

    # Build read-only reader from the store's READ-ONLY methods only.
    # SQLiteStore wraps MemoryStore via _mem; we extract only search_json
    # and get_memory (no side-effects, no write capability).
    if hasattr(mori_store, "_mem"):
        # SQLiteStore path
        mem = mori_store._mem

        def _search(query: str, limit: int) -> list[dict]:
            return mem.search_json(query=query, limit=limit)

        def _fetch_body(name: str) -> str:
            row = mem.get_memory(name)
            return (row or {}).get("body") or ""

    else:
        # PostgresStore path: search_json is async — not supported by the
        # synchronous assessor in this CLI.  Raise early with a clear message.
        raise RuntimeError(
            "cli: PostgresStore detected — the synchronous assessor CLI does not "
            "support the async PostgresStore search path.  Use an async runner or "
            "provide a custom CanonReader with sync-wrapped coroutines."
        )

    reader = CanonReader(search=_search, fetch_body=_fetch_body)
    return make_canon_assessor(reader, client)


async def _run(assess_only: bool, drain_only: bool, real_assessor: bool) -> None:
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

            assess_fn = None
            if real_assessor:
                assess_fn = _build_real_assess_fn(mori_store)
                logger.info("cli: using real fast-model assessor (B2)")
            else:
                logger.info("cli: using default stub assessor (B1 — treats all as novel)")

            assessed = await assess_once(pool, assess=assess_fn)
            logger.info("cli: assess_once() processed %d candidate(s)", assessed)

        if not assess_only:
            from mori_intake.canon_writer import drain_once

            promoted = await drain_once(pool, mori_store)
            logger.info("cli: drain_once() committed %d promotion(s)", promoted)

    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one assess+promote pass (B1/B2 manual trigger)."
    )
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
    parser.add_argument(
        "--real-assessor",
        action="store_true",
        help=(
            "Use the real fast-model vs-canon assessor (B2) instead of the default stub. "
            "Requires Bifrost reachable at MORI_BASE_URL (default http://localhost:8787). "
            "The stub treats every candidate as novel (UNRELATED); the real assessor "
            "checks candidates against mori canon via the fast model."
        ),
    )
    args = parser.parse_args()

    if not os.environ.get("MORI_INTAKE_DATABASE_URL"):
        logger.critical("MORI_INTAKE_DATABASE_URL is not set — cannot run.")
        sys.exit(1)

    asyncio.run(_run(args.assess_only, args.drain_only, args.real_assessor))


if __name__ == "__main__":
    main()
