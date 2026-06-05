"""Server startup / lifespan wiring — regression guard.

The unit + route suites exercise store logic but never boot the FastMCP lifespan.
A misplaced ``@asynccontextmanager`` once decorated the throttle cleanup loop
instead of ``_lifespan``, leaving ``_lifespan`` a bare async generator →
``'async_generator' object does not support the asynchronous context manager
protocol`` at server startup. 192 unit tests + CI were green; only running the
real server caught it. These tests guard the wiring directly so it can't regress.
"""

from __future__ import annotations

import asyncio
import inspect

from mori_advisor.main import _lifespan, _throttle_cleanup_loop


def test_lifespan_is_async_context_manager():
    cm = _lifespan(None)
    assert hasattr(cm, "__aenter__") and hasattr(cm, "__aexit__"), (
        "_lifespan must be @asynccontextmanager-decorated — FastMCP enters it as a "
        "context manager at startup; a bare async generator crashes the server."
    )


def test_lifespan_actually_enters_and_exits():
    """Boot the lifespan for real — the check the store-level unit tests can't do."""

    async def boot():
        async with _lifespan(None):
            pass  # entered cleanly; the cleanup task starts and is cancelled on exit

    asyncio.run(boot())


def test_throttle_cleanup_loop_is_plain_coroutine():
    # Scheduled via asyncio.create_task() — must be a coroutine fn, NOT a CM.
    assert inspect.iscoroutinefunction(_throttle_cleanup_loop)
    coro = _throttle_cleanup_loop()
    assert inspect.iscoroutine(coro)
    assert not hasattr(coro, "__aenter__")
    coro.close()  # avoid an un-awaited-coroutine warning
