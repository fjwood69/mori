"""AsyncStore — async facade over a backend store (issue #59).

The synchronous ``sqlite3`` driver must never run on the asyncio event loop: on a
single-worker uvicorn one blocking call freezes every concurrent request. This facade
off-loads SQLite work onto a DEDICATED single-thread executor (``max_workers=1`` —
SQLite is one-writer, so serialising in Python's queue preserves submission order and
read-after-write, with zero DB-lock contention). It is SEPARATE from the LLM executor:
a slow model call must never be able to starve DB ops (that was the v2.2.26 freeze).

Postgres methods are already coroutines — they are **awaited directly, never off-loaded**
(asyncpg connections are loop-bound; running one in a thread breaks them).

Multi-statement transactions go through :meth:`run_in_txn`, which runs the WHOLE
transaction (open conn → all writes → commit → close) on the executor thread as one
unit. The earlier "run inline when ``_conn`` is passed" rule was wrong: a coroutine is
never on the executor thread, so an inline in-transaction write runs on the *loop*
thread — defeating the off-load and risking a 30s ``busy_timeout`` loop-block. See the
issue-#59 design history.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import threading
from concurrent.futures import ThreadPoolExecutor

# Attributes returned UNWRAPPED from the backend (not turned into async off-loaders):
# transaction primitives (handled by run_in_txn), lifecycle, and raw escape hatches.
_RAW_ATTRS = frozenset(
    {
        "begin_transaction",
        "bootstrap",
        "ping",
        "get_conn",
        "db_path",
        "parse_tags",  # pure helper, no DB I/O — must not be wrapped as an async off-loader
        "_mem",
        "_log",
        "_msg",
    }
)


def _running_loop_or_none():
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _assert_off_loop() -> None:
    """Loud tripwire: a synchronous DB body must NOT execute on the event-loop thread.
    The executor thread has no running loop, so this passes there; if a sync DB call
    ever runs on the loop (a missed migration), it raises instead of silently blocking."""
    if _running_loop_or_none() is not None:
        raise RuntimeError("synchronous DB call executed on the event loop thread (issue #59)")


class AsyncStore:
    def __init__(self, backend) -> None:
        self._backend = backend
        self._executor: ThreadPoolExecutor | None = None
        self._async_txn = inspect.iscoroutinefunction(getattr(backend, "begin_transaction", None))

    # ── executor lifecycle ────────────────────────────────────────────────
    def _exec(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mori-db")
        return self._executor

    def aclose(self) -> None:
        """Drain in-flight DB work on shutdown — do NOT drop committed-but-unreturned
        writes. (No cancel_futures: a cancelled future would not cancel the thread, so a
        mid-transaction write could be abandoned — issue #59 design decision.)"""
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=False)
            self._executor = None

    # ── core off-load ─────────────────────────────────────────────────────
    async def _run(self, fn, *args, **kwargs):
        loop = asyncio.get_running_loop()

        def _guarded():
            _assert_off_loop()
            return fn(*args, **kwargs)

        return await loop.run_in_executor(self._exec(), _guarded)

    async def run_in_txn(self, work):
        """Run a whole transaction as ONE unit on the executor thread.

        ``work(conn)`` is a SYNCHRONOUS callable performing every statement of the
        transaction. The connection is created AND used entirely on the executor
        thread — never crossing a thread boundary, never inline on the loop.
        """
        if self._async_txn:
            raise NotImplementedError(
                "run_in_txn is for the synchronous (SQLite) backend; the async backend "
                "uses its native `async with begin_transaction()` path."
            )
        backend = self._backend

        def _txn():
            _assert_off_loop()
            with backend.begin_transaction() as conn:
                return work(conn)

        return await self._run(_txn)

    # ── facade dispatch ───────────────────────────────────────────────────
    def __getattr__(self, name):
        # __getattr__ only fires for names not found on AsyncStore itself.
        backend = object.__getattribute__(self, "_backend")
        attr = getattr(backend, name)
        if name in _RAW_ATTRS or name.startswith("_") or not callable(attr):
            return attr
        if inspect.iscoroutinefunction(attr):
            # Postgres — already async; await directly, NEVER off-load (loop-bound).
            return attr

        @functools.wraps(attr)
        async def _offloaded(*args, **kwargs):
            return await object.__getattribute__(self, "_run")(attr, *args, **kwargs)

        return _offloaded


def loop_thread_ident() -> int:
    """Identity of the current (event-loop) thread, for tests/asserts."""
    return threading.get_ident()
