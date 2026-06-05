"""GovernedWriteOutbox — crash-durable, non-blocking queue for mori proposals.

Architecture
------------
* SQLite backing store under ``hermes_home / "mori_outbox.db"`` — survives
  process restarts; rows written atomically before the caller returns.
* A single background **daemon thread** drains the queue by calling
  ``MoriRestClient.propose`` in a loop.
* Backpressure: if unflushed row count exceeds ``max_pending`` (default 100),
  new enqueue calls log a WARNING and are silently dropped — this prevents
  mori's governance queue from being flooded.
* Retry / back-off for transport errors and 429 responses:
    - 429 (rate limited) → exponential back-off, capped at ``max_backoff``.
    - Transport error  → same back-off schedule.
    - 4xx non-429 (client error, bad payload) → mark FAILED, do not retry.
    - 2xx / 202        → mark DONE.
* ``flush(timeout)`` blocks the caller until the queue is drained or the
  timeout expires — useful for best-effort drain on session end.

Dependency injection
--------------------
The drainer accepts a ``_sleep`` callable so tests can replace ``time.sleep``
with a fast/deterministic alternative.  The client is passed in so tests can
use a fake without a live mori server.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Status values stored in the ``status`` column.
_PENDING = "pending"
_DONE = "done"
_FAILED = "failed"  # permanent failure (4xx, not retried)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    title       TEXT    NOT NULL DEFAULT '',
    description TEXT    NOT NULL DEFAULT '',
    type        TEXT    NOT NULL DEFAULT 'project',
    body        TEXT    NOT NULL DEFAULT '',
    tags        TEXT    NOT NULL DEFAULT '[]',
    idempotency TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL
)
"""


def _now() -> float:
    return time.time()


class GovernedWriteOutbox:
    """Non-blocking, crash-durable proposal queue backed by SQLite.

    Parameters
    ----------
    client:
        A ``MoriRestClient``-compatible object with a ``propose()`` method.
        Injected so tests can supply a fake.
    db_path:
        Path to the SQLite database file.  Created if absent.
    max_pending:
        Maximum number of unflushed rows before new enqueues are dropped.
    initial_backoff:
        Seconds to wait after the first retry-able failure.
    max_backoff:
        Upper bound on the exponential back-off delay.
    _sleep:
        Callable used for sleeping in the drainer loop.  Override in tests for
        deterministic, instant execution.
    """

    def __init__(
        self,
        client: Any,
        db_path: Path,
        *,
        max_pending: int = 100,
        initial_backoff: float = 1.0,
        max_backoff: float = 60.0,
        _sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._db_path = db_path
        self._max_pending = max_pending
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._sleep = _sleep

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._flush_event = threading.Event()

        self._db: sqlite3.Connection = self._open_db()
        # Pre-set the flush event so the drainer scans immediately on startup
        # — this ensures any pending rows from a previous run are drained
        # without waiting for the first 5-second poll cycle.
        self._flush_event.set()
        self._thread = threading.Thread(target=self._drain_loop, daemon=True, name="mori-outbox")
        self._thread.start()

    # ── Public API ──────────────────────────────────────────────────────────

    def enqueue(self, payload: dict[str, Any]) -> bool:
        """Append a proposal to the queue.  Returns immediately.

        Returns ``True`` if enqueued, ``False`` if dropped due to backpressure.
        """
        with self._lock:
            count = self._pending_count()
            if count >= self._max_pending:
                logger.warning(
                    "outbox: backpressure threshold %d reached (%d pending) — dropping proposal %r",
                    self._max_pending,
                    count,
                    payload.get("name", "?"),
                )
                return False

            now = _now()
            # Store tags as a JSON array string.
            import json

            tags_json = json.dumps(payload.get("tags", []))
            self._db.execute(
                """
                INSERT INTO outbox
                    (name, title, description, type, body, tags, idempotency,
                     status, attempts, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    payload.get("name", ""),
                    payload.get("title", ""),
                    payload.get("description", ""),
                    payload.get("type", "project"),
                    payload.get("body", ""),
                    tags_json,
                    payload.get("idempotency_key", ""),
                    _PENDING,
                    now,
                    now,
                ),
            )
            self._db.commit()

        # Wake the drainer so it doesn't wait for its next poll cycle.
        self._flush_event.set()
        return True

    def pending_count(self) -> int:
        """Return the number of pending (not yet successfully sent) rows."""
        with self._lock:
            return self._pending_count()

    def flush(self, timeout: float = 10.0) -> bool:
        """Block until the queue is empty or timeout expires.

        Returns ``True`` if the queue is empty, ``False`` if timed out.
        Best-effort — suitable for session-end drain.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._pending_count() == 0:
                    return True
            self._flush_event.set()
            time.sleep(0.05)
        return self._pending_count() == 0

    def shutdown(self) -> None:
        """Signal the drain thread to stop and wait for it to exit.

        The DB connection is closed AFTER the thread has joined so that the
        drainer never operates on a closed connection.
        """
        self._stop_event.set()
        self._flush_event.set()
        self._thread.join(timeout=5.0)
        # Thread has exited (or timed out) — safe to close the DB now.
        with self._lock:
            try:
                self._db.close()
            except Exception:
                pass

    # ── Internals ───────────────────────────────────────────────────────────

    def _open_db(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_SCHEMA)
        conn.commit()
        return conn

    def _pending_count(self) -> int:
        """Count unflushed rows.  Must be called with ``self._lock`` held."""
        row = self._db.execute(
            "SELECT COUNT(*) FROM outbox WHERE status = ?", (_PENDING,)
        ).fetchone()
        return row[0] if row else 0

    def _next_row(self) -> sqlite3.Row | None:
        """Fetch the oldest pending row.  Must be called with ``self._lock`` held."""
        return self._db.execute(
            "SELECT * FROM outbox WHERE status = ? ORDER BY id ASC LIMIT 1",
            (_PENDING,),
        ).fetchone()

    def _mark(self, row_id: int, status: str) -> None:
        """Update row status.  Must be called with ``self._lock`` held."""
        self._db.execute(
            "UPDATE outbox SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), row_id),
        )
        self._db.commit()

    def _increment_attempts(self, row_id: int) -> None:
        """Bump attempt counter.  Must be called with ``self._lock`` held."""
        self._db.execute(
            "UPDATE outbox SET attempts = attempts + 1, updated_at = ? WHERE id = ?",
            (_now(), row_id),
        )
        self._db.commit()

    def _drain_loop(self) -> None:
        """Background thread: drain pending rows one at a time."""
        import json

        from .rest_client import MoriTransportError

        backoff = self._initial_backoff

        while not self._stop_event.is_set():
            # Wait for work or a periodic wake.
            self._flush_event.wait(timeout=5.0)
            self._flush_event.clear()

            if self._stop_event.is_set():
                break

            while not self._stop_event.is_set():
                with self._lock:
                    row = self._next_row()

                if row is None:
                    backoff = self._initial_backoff  # reset on idle
                    break

                row_id: int = row["id"]
                name: str = row["name"]
                try:
                    tags = json.loads(row["tags"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    tags = []

                try:
                    status_code, resp = self._client.propose(
                        name=name,
                        title=row["title"],
                        description=row["description"],
                        type=row["type"],
                        body=row["body"],
                        tags=tags,
                        idempotency_key=row["idempotency"],
                    )
                except MoriTransportError as exc:
                    # Transport failure or 5xx — back off and retry later.
                    if not self._stop_event.is_set():
                        with self._lock:
                            self._increment_attempts(row_id)
                    logger.warning(
                        "outbox: transport error for %r — retry after %.1fs: %s",
                        name,
                        backoff,
                        exc,
                    )
                    self._sleep(backoff)
                    backoff = min(backoff * 2, self._max_backoff)
                    # Stay in the inner loop — re-fetch the same row immediately
                    # rather than waiting for the outer flush_event timeout.
                    continue

                except Exception as exc:
                    # Unexpected error — treat as transport failure.
                    if not self._stop_event.is_set():
                        with self._lock:
                            self._increment_attempts(row_id)
                    logger.error(
                        "outbox: unexpected error for %r — retry after %.1fs: %s",
                        name,
                        backoff,
                        exc,
                    )
                    self._sleep(backoff)
                    backoff = min(backoff * 2, self._max_backoff)
                    continue

                if status_code == 429:
                    # Rate limited — back off.
                    if not self._stop_event.is_set():
                        with self._lock:
                            self._increment_attempts(row_id)
                    logger.warning(
                        "outbox: 429 rate-limited for %r — retry after %.1fs",
                        name,
                        backoff,
                    )
                    self._sleep(backoff)
                    backoff = min(backoff * 2, self._max_backoff)
                    continue

                if 200 <= status_code < 300:
                    with self._lock:
                        self._mark(row_id, _DONE)
                    logger.debug("outbox: sent %r (HTTP %d)", name, status_code)
                    backoff = self._initial_backoff  # success resets back-off
                    continue  # try next row immediately

                # 4xx (not 429) — permanent failure, drop.
                with self._lock:
                    self._mark(row_id, _FAILED)
                logger.warning(
                    "outbox: permanent failure for %r (HTTP %d) — dropping: %s",
                    name,
                    status_code,
                    resp,
                )
                continue  # next row
