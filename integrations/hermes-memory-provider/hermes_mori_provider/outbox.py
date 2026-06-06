"""GovernedWriteOutbox — crash-durable proposal queue + Local Working Memory.

Two SQLite-backed tables in one DB file under ``hermes_home / "mori_outbox.db"``:

``outbox``
    The async governed-proposal pipeline. Rows are written atomically before
    the caller returns; a single background daemon thread drains them by calling
    ``MoriRestClient.propose`` in a loop.

``lwm`` (Local Working Memory)
    A strongly-consistent optimistic overlay so ``prefetch`` sees the agent's
    own writes instantly (read-your-writes), before mori governance has
    approved anything. One row per memory name. Columns mirror the validated
    architecture: ``name, target, content, content_hash, status, session_id,
    proposed_at, last_reconciled_at`` (status in pending|canon|rejected).

Outbox behaviour
----------------
* Backpressure: if unsent row count exceeds ``max_pending`` (default 100), new
  enqueues log a WARNING and are dropped (returns False).
* Coalescing on enqueue: a ``supersede`` whose prior proposal for the same name
  is still unsent updates that row in place instead of emitting a second
  proposal. A ``retract`` whose prior proposal is still unsent deletes the row
  (add-then-remove while local = net no-op) rather than sending anything.
* Retry / back-off for transport errors and 429s, capped at ``max_backoff``.
* Circuit breaker: after ``breaker_threshold`` consecutive transport failures
  the drainer "opens" — it stops hammering mori and sleeps ``breaker_cooldown``
  before probing again. Resets on first success.
* 4xx (non-429) -> mark FAILED (dead-letter), do not retry.

Metrics hooks
-------------
Lightweight in-process counters (``metrics`` dict) + structured log lines. No
external metrics dependency. Exposed via ``metrics_snapshot()``: ``outbox_depth``
(pending rows), ``lwm_pending`` (pending LWM rows), ``proposals_sent``,
``proposals_failed``, ``breaker_trips``, ``breaker_open``.

Dependency injection
--------------------
``_sleep`` is injected so tests replace ``time.sleep`` with an instant stub.
The client is injected so tests use a fake without a live mori server.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# outbox.status values.
_PENDING = "pending"
_DONE = "done"
_FAILED = "failed"  # permanent failure (4xx, not retried)

# lwm.status values.
LWM_PENDING = "pending"
LWM_CANON = "canon"
LWM_REJECTED = "rejected"

# Internal op vocabulary (from the normalizer).
_OP_PROPOSE = "propose"
_OP_SUPERSEDE = "supersede"
_OP_RETRACT = "retract"

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
    op          TEXT    NOT NULL DEFAULT 'propose',
    status      TEXT    NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS lwm (
    name               TEXT PRIMARY KEY,
    target             TEXT NOT NULL DEFAULT 'memory',
    content            TEXT NOT NULL DEFAULT '',
    content_hash       TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL DEFAULT 'pending',
    session_id         TEXT NOT NULL DEFAULT '',
    proposed_at        REAL NOT NULL,
    last_reconciled_at REAL
);
"""


def _now() -> float:
    return time.time()


class GovernedWriteOutbox:
    """Crash-durable proposal queue + LWM overlay, backed by SQLite.

    Parameters
    ----------
    client:
        A ``MoriRestClient``-compatible object with a ``propose()`` method.
    db_path:
        Path to the SQLite database file. Created if absent.
    max_pending:
        Maximum unsent outbox rows before new enqueues are dropped.
    initial_backoff / max_backoff:
        Exponential back-off bounds for retry-able failures.
    breaker_threshold:
        Consecutive transport failures that trip the circuit breaker.
    breaker_cooldown:
        Seconds the drainer waits while the breaker is open before probing.
    _sleep:
        Sleep callable; override in tests for instant, deterministic runs.
    """

    def __init__(
        self,
        client: Any,
        db_path: Path,
        *,
        max_pending: int = 100,
        initial_backoff: float = 1.0,
        max_backoff: float = 60.0,
        breaker_threshold: int = 5,
        breaker_cooldown: float = 30.0,
        autostart_drain: bool = True,
        _sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._db_path = db_path
        self._max_pending = max_pending
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._breaker_threshold = breaker_threshold
        self._breaker_cooldown = breaker_cooldown
        self._sleep = _sleep

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._flush_event = threading.Event()
        # Gate the drainer until released. In production it is released
        # immediately; tests can hold it to enqueue+coalesce before any send.
        self._drain_gate = threading.Event()
        if autostart_drain:
            self._drain_gate.set()

        # Circuit-breaker state.
        self._consecutive_failures = 0

        # Lightweight metrics counters.
        self.metrics: dict[str, int] = {
            "proposals_sent": 0,
            "proposals_failed": 0,
            "breaker_trips": 0,
        }

        self._db: sqlite3.Connection = self._open_db()
        # Drain any rows left over from a previous run immediately.
        self._flush_event.set()
        self._thread = threading.Thread(target=self._drain_loop, daemon=True, name="mori-outbox")
        self._thread.start()

    # ── Outbox public API ────────────────────────────────────────────────────

    def enqueue(self, payload: dict[str, Any]) -> bool:
        """Append (or coalesce) a proposal. Returns immediately.

        ``payload`` should carry an ``op`` key (propose|supersede|retract) plus
        the standard proposal fields. Coalescing:

        * ``supersede`` with an unsent prior row for the same name -> update
          that row in place (no second proposal).
        * ``retract`` with an unsent prior row for the same name -> delete the
          row (add-then-remove while local = net no-op, nothing sent).
        * Otherwise -> INSERT a new pending row.

        Returns ``True`` if enqueued/coalesced, ``False`` if dropped due to
        backpressure.
        """
        op = payload.get("op", _OP_PROPOSE)
        name = payload.get("name", "")

        with self._lock:
            existing = self._unsent_row_for(name)

            # Coalescing fast paths against an unsent prior row.
            if existing is not None and op == _OP_SUPERSEDE:
                self._update_row_in_place(existing["id"], payload)
                self._db.commit()
                self._flush_event.set()
                return True

            if existing is not None and op == _OP_RETRACT:
                # add-then-remove while still local => cancel entirely.
                self._db.execute("DELETE FROM outbox WHERE id = ?", (existing["id"],))
                self._db.commit()
                logger.info("outbox: coalesced retract cancelled unsent proposal %r", name)
                return True

            # Backpressure check applies only to genuinely new rows.
            count = self._pending_count()
            if count >= self._max_pending:
                logger.warning(
                    "outbox: backpressure threshold %d reached (%d pending) — dropping %r",
                    self._max_pending,
                    count,
                    name,
                )
                return False

            now = _now()
            self._db.execute(
                """
                INSERT INTO outbox
                    (name, title, description, type, body, tags, idempotency,
                     op, status, attempts, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    name,
                    payload.get("title", ""),
                    payload.get("description", ""),
                    payload.get("type", "project"),
                    payload.get("body", ""),
                    json.dumps(payload.get("tags", [])),
                    payload.get("idempotency_key", ""),
                    op,
                    _PENDING,
                    now,
                    now,
                ),
            )
            self._db.commit()

        self._flush_event.set()
        return True

    def pending_count(self) -> int:
        """Return the number of unsent outbox rows."""
        with self._lock:
            return self._pending_count()

    def flush(self, timeout: float = 10.0) -> bool:
        """Block until the outbox is empty or *timeout* expires.

        Returns ``True`` if empty, ``False`` if timed out. Best-effort.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._pending_count() == 0:
                    return True
            self._flush_event.set()
            time.sleep(0.05)
        return self.pending_count() == 0

    def shutdown(self) -> None:
        """Stop the drain thread and close the DB after it has joined."""
        self._stop_event.set()
        self._flush_event.set()
        self._thread.join(timeout=5.0)
        with self._lock:
            try:
                self._db.close()
            except Exception:
                pass

    # ── LWM (Local Working Memory) public API ────────────────────────────────

    def lwm_upsert(
        self,
        *,
        name: str,
        target: str,
        content: str,
        content_hash: str,
        session_id: str = "",
        status: str = LWM_PENDING,
    ) -> None:
        """Synchronously upsert an LWM row (read-your-writes overlay)."""
        now = _now()
        with self._lock:
            self._db.execute(
                """
                INSERT INTO lwm
                    (name, target, content, content_hash, status,
                     session_id, proposed_at, last_reconciled_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(name) DO UPDATE SET
                    target=excluded.target,
                    content=excluded.content,
                    content_hash=excluded.content_hash,
                    status=excluded.status,
                    session_id=excluded.session_id,
                    proposed_at=excluded.proposed_at,
                    last_reconciled_at=NULL
                """,
                (name, target, content, content_hash, status, session_id, now),
            )
            self._db.commit()

    def lwm_mark(self, name: str, status: str) -> None:
        """Update the status of an LWM row, stamping last_reconciled_at."""
        with self._lock:
            self._db.execute(
                "UPDATE lwm SET status = ?, last_reconciled_at = ? WHERE name = ?",
                (status, _now(), name),
            )
            self._db.commit()

    def lwm_set_content(self, name: str, content: str, content_hash: str, status: str) -> None:
        """Overwrite an LWM row's content (e.g. dreamer-edited canon wins)."""
        with self._lock:
            self._db.execute(
                """
                UPDATE lwm
                SET content = ?, content_hash = ?, status = ?, last_reconciled_at = ?
                WHERE name = ?
                """,
                (content, content_hash, status, _now(), name),
            )
            self._db.commit()

    def lwm_delete(self, name: str) -> None:
        """Remove an LWM row entirely."""
        with self._lock:
            self._db.execute("DELETE FROM lwm WHERE name = ?", (name,))
            self._db.commit()

    def lwm_get(self, name: str) -> dict[str, Any] | None:
        """Return a single LWM row as a dict, or None."""
        with self._lock:
            row = self._db.execute("SELECT * FROM lwm WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None

    def lwm_all(self, *, exclude_rejected: bool = True) -> list[dict[str, Any]]:
        """Return all LWM rows (newest first), optionally excluding rejected."""
        sql = "SELECT * FROM lwm"
        if exclude_rejected:
            sql += f" WHERE status != '{LWM_REJECTED}'"
        sql += " ORDER BY proposed_at DESC"
        with self._lock:
            rows = self._db.execute(sql).fetchall()
        return [dict(r) for r in rows]

    def lwm_pending_count(self) -> int:
        """Return the number of LWM rows still in ``pending`` status."""
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM lwm WHERE status = ?", (LWM_PENDING,)
            ).fetchone()
        return row[0] if row else 0

    # ── Metrics ──────────────────────────────────────────────────────────────

    def metrics_snapshot(self) -> dict[str, int]:
        """Return a snapshot of metrics counters plus live gauges."""
        snap = dict(self.metrics)
        snap["outbox_depth"] = self.pending_count()
        snap["lwm_pending"] = self.lwm_pending_count()
        snap["breaker_open"] = 1 if self._consecutive_failures >= self._breaker_threshold else 0
        return snap

    # ── Internals ────────────────────────────────────────────────────────────

    def _open_db(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        return conn

    def _pending_count(self) -> int:
        """Count unsent rows. Must be called with ``self._lock`` held."""
        row = self._db.execute(
            "SELECT COUNT(*) FROM outbox WHERE status = ?", (_PENDING,)
        ).fetchone()
        return row[0] if row else 0

    def _unsent_row_for(self, name: str) -> sqlite3.Row | None:
        """Newest unsent row for *name*. Must hold ``self._lock``."""
        return self._db.execute(
            "SELECT * FROM outbox WHERE name = ? AND status = ? ORDER BY id DESC LIMIT 1",
            (name, _PENDING),
        ).fetchone()

    def _update_row_in_place(self, row_id: int, payload: dict[str, Any]) -> None:
        """Rewrite an unsent row with a superseding payload. Must hold lock."""
        self._db.execute(
            """
            UPDATE outbox
            SET title = ?, description = ?, type = ?, body = ?, tags = ?,
                idempotency = ?, op = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                payload.get("title", ""),
                payload.get("description", ""),
                payload.get("type", "project"),
                payload.get("body", ""),
                json.dumps(payload.get("tags", [])),
                payload.get("idempotency_key", ""),
                payload.get("op", _OP_SUPERSEDE),
                _now(),
                row_id,
            ),
        )
        logger.debug(
            "outbox: coalesced supersede into unsent row %d (%r)", row_id, payload.get("name")
        )

    def _next_row(self) -> sqlite3.Row | None:
        """Oldest pending row. Must hold ``self._lock``."""
        return self._db.execute(
            "SELECT * FROM outbox WHERE status = ? ORDER BY id ASC LIMIT 1",
            (_PENDING,),
        ).fetchone()

    def _mark(self, row_id: int, status: str) -> None:
        """Update row status. Must hold ``self._lock``."""
        self._db.execute(
            "UPDATE outbox SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), row_id),
        )
        self._db.commit()

    def _increment_attempts(self, row_id: int) -> None:
        """Bump attempt counter. Must hold ``self._lock``."""
        self._db.execute(
            "UPDATE outbox SET attempts = attempts + 1, updated_at = ? WHERE id = ?",
            (_now(), row_id),
        )
        self._db.commit()

    def _record_failure(self, backoff: float) -> float:
        """Account a transport failure, trip the breaker if needed, sleep.

        Returns the next back-off value (doubled, capped).
        """
        self._consecutive_failures += 1
        self.metrics["proposals_failed"] += 1
        if self._consecutive_failures == self._breaker_threshold:
            self.metrics["breaker_trips"] += 1
            logger.warning(
                "outbox: circuit breaker OPEN after %d consecutive failures — cooling down %.1fs",
                self._consecutive_failures,
                self._breaker_cooldown,
            )
            self._sleep(self._breaker_cooldown)
        else:
            self._sleep(backoff)
        return min(backoff * 2, self._max_backoff)

    def _reset_breaker(self) -> None:
        self._consecutive_failures = 0

    def resume_drain(self) -> None:
        """Release the drain gate (test seam; no-op if already draining)."""
        self._drain_gate.set()
        self._flush_event.set()

    def _drain_loop(self) -> None:
        """Background thread: drain pending rows one at a time."""
        from .rest_client import MoriTransportError

        # Hold until released (production: released in __init__).
        while not self._drain_gate.wait(timeout=0.05):
            if self._stop_event.is_set():
                return

        backoff = self._initial_backoff

        while not self._stop_event.is_set():
            self._flush_event.wait(timeout=5.0)
            self._flush_event.clear()

            if self._stop_event.is_set():
                break

            while not self._stop_event.is_set():
                with self._lock:
                    row = self._next_row()

                if row is None:
                    backoff = self._initial_backoff
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
                    if not self._stop_event.is_set():
                        with self._lock:
                            self._increment_attempts(row_id)
                    logger.warning(
                        "outbox: transport error for %r — retry after %.1fs: %s",
                        name,
                        backoff,
                        exc,
                    )
                    backoff = self._record_failure(backoff)
                    continue
                except Exception as exc:
                    if not self._stop_event.is_set():
                        with self._lock:
                            self._increment_attempts(row_id)
                    logger.error(
                        "outbox: unexpected error for %r — retry after %.1fs: %s",
                        name,
                        backoff,
                        exc,
                    )
                    backoff = self._record_failure(backoff)
                    continue

                if status_code == 429:
                    if not self._stop_event.is_set():
                        with self._lock:
                            self._increment_attempts(row_id)
                    logger.warning(
                        "outbox: 429 rate-limited for %r — retry after %.1fs", name, backoff
                    )
                    backoff = self._record_failure(backoff)
                    continue

                if 200 <= status_code < 300:
                    with self._lock:
                        self._mark(row_id, _DONE)
                    self.metrics["proposals_sent"] += 1
                    self._reset_breaker()
                    logger.debug("outbox: sent %r (HTTP %d)", name, status_code)
                    backoff = self._initial_backoff
                    continue

                # 4xx (not 429) — permanent failure, dead-letter.
                with self._lock:
                    self._mark(row_id, _FAILED)
                self.metrics["proposals_failed"] += 1
                logger.warning(
                    "outbox: permanent failure for %r (HTTP %d) — dead-lettering: %s",
                    name,
                    status_code,
                    resp,
                )
                # A clean 4xx response means mori is reachable; reset breaker.
                self._reset_breaker()
                continue
