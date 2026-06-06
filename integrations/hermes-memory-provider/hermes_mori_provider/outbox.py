"""GovernedWriteOutbox — crash-durable proposal queue + Local Working Memory.

Two SQLite-backed tables in one DB file under ``hermes_home / "mori_outbox.db"``:

``outbox``
    The async governed-proposal pipeline. Rows are written atomically before
    the caller returns; a single background daemon thread drains them by calling
    ``MoriRestClient.submit_intake`` via the configured ``intake_client``.

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
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL,
    title             TEXT    NOT NULL DEFAULT '',
    description       TEXT    NOT NULL DEFAULT '',
    type              TEXT    NOT NULL DEFAULT 'project',
    body              TEXT    NOT NULL DEFAULT '',
    tags              TEXT    NOT NULL DEFAULT '[]',
    idempotency       TEXT    NOT NULL DEFAULT '',
    op                TEXT    NOT NULL DEFAULT 'propose',
    action            TEXT    NOT NULL DEFAULT '',
    intake_stable_key TEXT    NOT NULL DEFAULT '',
    target            TEXT    NOT NULL DEFAULT 'memory',
    session_id        TEXT    NOT NULL DEFAULT '',
    status            TEXT    NOT NULL DEFAULT 'pending',
    attempts          INTEGER NOT NULL DEFAULT 0,
    created_at        REAL    NOT NULL,
    updated_at        REAL    NOT NULL
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

-- SCALE-001: indexes on the outbox table so drain/count queries avoid full-table scans.
-- (status, id) covers both _pending_count() and _next_row() which filter on status and
-- sort by id.  (name, status) covers _unsent_row_for() which filters on both.
CREATE INDEX IF NOT EXISTS idx_outbox_status_id ON outbox(status, id);
CREATE INDEX IF NOT EXISTS idx_outbox_name_status ON outbox(name, status);
"""

# Columns added in v0.3.0 — applied via ALTER TABLE when an existing DB is
# opened (idempotent: silently skipped if the column already exists).
_V030_COLUMNS: list[tuple[str, str]] = [
    ("action", "TEXT NOT NULL DEFAULT ''"),
    ("intake_stable_key", "TEXT NOT NULL DEFAULT ''"),
    ("target", "TEXT NOT NULL DEFAULT 'memory'"),
    ("session_id", "TEXT NOT NULL DEFAULT ''"),
]


def _now() -> float:
    return time.time()


class GovernedWriteOutbox:
    """Crash-durable proposal queue + LWM overlay, backed by SQLite.

    Parameters
    ----------
    client:
        A ``MoriRestClient``-compatible object used for reads (search,
        get_memory, list_pending).  The ``propose()`` method on this object
        is never called by the drain loop — all writes go through
        ``intake_client``.
    db_path:
        Path to the SQLite database file. Created if absent.
    intake_client:
        Client pointed at the intake governance service base URL.  The drain
        loop calls ``submit_intake()`` on this object for every pending row.
        When ``None`` the drain **FAILS CLOSED**: rows remain queued, an
        ERROR is logged (once) naming ``MORI_INTAKE_URL``, and the
        ungoverned ``/api/memories`` path is NEVER used as a fallback.
        Reads (prefetch / search / reconcile) are unaffected.
    intake_agent_id:
        Agent identifier sent in every intake submission (default ``"hermes"``).
    intake_session_id:
        Session identifier sent in intake submissions.  A stable per-instance
        value is acceptable (intake idempotency key is ``(session_id,
        stable_key)`` — stable enough for the provider's use-case).
    max_pending:
        Maximum unsent outbox rows before new enqueues are dropped.
    initial_backoff / max_backoff:
        Exponential back-off bounds for retry-able failures.
    breaker_threshold:
        Consecutive transport failures that trip the circuit breaker.
    breaker_cooldown:
        Seconds the drainer waits while the breaker is open before probing.
    terminal_max_age:
        Age (seconds) after which terminal rows (``done`` / ``failed``) are
        purged from the outbox so the SQLite file does not grow without bound
        over months of operation (default 7 days).  Pending rows are never
        purged here — they are bounded by ``max_pending`` backpressure.  Set to
        0 to disable terminal-row purging.
    terminal_purge_interval:
        Minimum seconds between terminal-row purge passes (default 1h).
    _sleep:
        Sleep callable; override in tests for instant, deterministic runs.
    """

    def __init__(
        self,
        client: Any,
        db_path: Path,
        *,
        intake_client: Any = None,
        intake_agent_id: str = "hermes",
        intake_session_id: str = "hermes",
        max_pending: int = 100,
        initial_backoff: float = 1.0,
        max_backoff: float = 60.0,
        breaker_threshold: int = 5,
        breaker_cooldown: float = 30.0,
        terminal_max_age: float = 604800.0,
        terminal_purge_interval: float = 3600.0,
        autostart_drain: bool = True,
        _sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._intake_client = intake_client
        self._intake_agent_id = intake_agent_id
        self._intake_session_id = intake_session_id
        self._db_path = db_path
        self._max_pending = max_pending
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._breaker_threshold = breaker_threshold
        self._breaker_cooldown = breaker_cooldown
        self._terminal_max_age = terminal_max_age
        self._terminal_purge_interval = terminal_purge_interval
        self._last_terminal_purge = 0.0
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
            "terminal_purged": 0,
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
                     op, action, intake_stable_key, target, session_id,
                     status, attempts, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
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
                    payload.get("action", ""),
                    payload.get("intake_stable_key", ""),
                    payload.get("target", "memory"),
                    payload.get("session_id", ""),
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

    def has_unsent(self, name: str) -> bool:
        """Return True if an unsent outbox row exists for *name*.

        Thread-safe (acquires ``self._lock`` internally).  Use this instead of
        calling the private ``_unsent_row_for`` directly, which requires the
        caller to hold the lock — an invariant that is easy to violate across
        module boundaries (INTAKE-02).
        """
        with self._lock:
            return self._unsent_row_for(name) is not None

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
        # v0.3.0 migration: add new columns to existing outbox tables.
        self._apply_v030_columns(conn)
        return conn

    @staticmethod
    def _apply_v030_columns(conn: sqlite3.Connection) -> None:
        """Add v0.3.0 columns to an existing outbox table (idempotent)."""
        existing: set[str] = set()
        for row in conn.execute("PRAGMA table_info(outbox)"):
            existing.add(row["name"])
        for col_name, col_def in _V030_COLUMNS:
            if col_name not in existing:
                try:
                    conn.execute(f"ALTER TABLE outbox ADD COLUMN {col_name} {col_def}")
                    conn.commit()
                    logger.debug(
                        "outbox: added column %r to outbox table (v0.3.0 migration)", col_name
                    )
                except sqlite3.OperationalError as exc:
                    # Silently skip if the column already exists (race or repeat call).
                    if "duplicate column" not in str(exc).lower():
                        raise

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
                idempotency = ?, op = ?, action = ?, intake_stable_key = ?,
                target = ?, session_id = ?, updated_at = ?
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
                payload.get("action", ""),
                payload.get("intake_stable_key", ""),
                payload.get("target", "memory"),
                payload.get("session_id", ""),
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

        ARCH-001: the breaker cooldown uses ``_stop_event.wait`` rather than a
        plain ``_sleep`` so that a shutdown request is honoured promptly even
        during the full cooldown period.  The short retry back-offs also use
        ``_stop_event.wait`` for the same reason.
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
            # ARCH-001: use stop_event.wait so shutdown is responsive during
            # the full breaker cooldown (replacing the old self._sleep call
            # which would block the drain thread for the full 30s).
            self._stop_event.wait(self._breaker_cooldown)
        else:
            # Regular retry back-off uses the injected _sleep (which tests can
            # replace with an instant no-op to keep tests fast).
            self._sleep(backoff)
        return min(backoff * 2, self._max_backoff)

    def _reset_breaker(self) -> None:
        self._consecutive_failures = 0

    def _maybe_purge_terminal(self) -> None:
        """Periodically delete aged terminal (done/failed) rows.

        Bounds the SQLite file over long uptimes.  Rate-limited to one pass per
        ``_terminal_purge_interval``.  Pending rows are untouched (they are
        bounded separately by ``max_pending`` backpressure).  No-op when
        ``_terminal_max_age <= 0``.
        """
        if self._terminal_max_age <= 0:
            return
        now = time.monotonic()
        if now - self._last_terminal_purge < self._terminal_purge_interval:
            return
        self._last_terminal_purge = now
        cutoff = _now() - self._terminal_max_age
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM outbox WHERE status IN (?, ?) AND updated_at < ?",
                (_DONE, _FAILED, cutoff),
            )
            self._db.commit()
            purged = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        if purged:
            self.metrics["terminal_purged"] += purged
            logger.info(
                "outbox: purged %d aged terminal row(s) (older than %.0fs)",
                purged,
                self._terminal_max_age,
            )

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

            # Housekeeping: bound the SQLite file by reaping aged terminal rows.
            self._maybe_purge_terminal()

            while not self._stop_event.is_set():
                with self._lock:
                    row = self._next_row()

                if row is None:
                    backoff = self._initial_backoff
                    break

                row_id: int = row["id"]
                name: str = row["name"]

                # ── Governed write path (FAIL CLOSED) ───────────────────────
                # POST to the intake service /intake/submissions. If no intake
                # client is configured we FAIL CLOSED — we never fall back to the
                # ungoverned /api/memories path (which lands working-tier-direct,
                # the exact hole the governance pipeline closes). See else branch.
                if self._intake_client is not None:
                    try:
                        status_code, resp = self._send_to_intake(row)
                    except MoriTransportError as exc:
                        if not self._stop_event.is_set():
                            with self._lock:
                                self._increment_attempts(row_id)
                        logger.warning(
                            "outbox: intake transport error for %r — retry after %.1fs: %s",
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
                            "outbox: unexpected intake error for %r — retry after %.1fs: %s",
                            name,
                            backoff,
                            exc,
                        )
                        backoff = self._record_failure(backoff)
                        continue
                else:
                    # Fail closed: no governed intake target configured. NEVER
                    # fall back to the ungoverned /api/memories path — leave rows
                    # queued and warn loudly (once) until MORI_INTAKE_URL is set.
                    if not getattr(self, "_warned_no_intake", False):
                        logger.error(
                            "outbox: MORI_INTAKE_URL not configured — refusing the "
                            "ungoverned /api/memories fallback; writes remain queued "
                            "until the intake service URL is set."
                        )
                        self._warned_no_intake = True
                    break

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
                    duplicate = resp.get("duplicate", False) if isinstance(resp, dict) else False
                    with self._lock:
                        self._mark(row_id, _DONE)
                    self.metrics["proposals_sent"] += 1
                    self._reset_breaker()
                    logger.debug(
                        "outbox: sent %r (HTTP %d%s)",
                        name,
                        status_code,
                        " duplicate" if duplicate else "",
                    )
                    backoff = self._initial_backoff
                    continue

                # 422 from the intake service — eligibility rejection (policy).
                # Dead-letter immediately; never retry (the gate is server-side
                # and the payload won't change).
                if status_code == 422:
                    with self._lock:
                        self._mark(row_id, _FAILED)
                    self.metrics["proposals_failed"] += 1
                    reason = resp.get("reason", "unknown") if isinstance(resp, dict) else "unknown"
                    logger.warning(
                        "outbox: intake eligibility rejection for %r — dead-lettering"
                        " (reason=%r): %s",
                        name,
                        reason,
                        resp,
                    )
                    self._reset_breaker()
                    continue

                # Other 4xx (not 429/422) — permanent failure, dead-letter.
                with self._lock:
                    self._mark(row_id, _FAILED)
                self.metrics["proposals_failed"] += 1
                logger.warning(
                    "outbox: permanent failure for %r (HTTP %d) — dead-lettering: %s",
                    name,
                    status_code,
                    resp,
                )
                # A clean 4xx response means the server is reachable; reset breaker.
                self._reset_breaker()
                continue

    def _send_to_intake(self, row: sqlite3.Row) -> tuple[int, dict]:
        """Build an intake payload from *row* and call ``submit_intake``.

        The ``session_id`` comes from the row (set at enqueue time from the
        provider's session).  ``agent_id`` comes from the outbox config.
        The ``action`` is the original hermes action (add/replace/remove).
        The ``intake_stable_key`` is the eligibility-namespaced key stored on
        enqueue.

        Provenance includes lineage fields for audit.
        """
        action = row["action"] or "add"
        intake_stable_key = row["intake_stable_key"] or row["name"]
        session_id = row["session_id"] or self._intake_session_id
        target = row["target"] or "memory"

        try:
            tags = json.loads(row["tags"] or "[]")
        except (json.JSONDecodeError, TypeError):
            tags = []

        provenance: dict = {
            "mori_name": row["name"],
            "content_hash": row["idempotency"],  # idempotency stores content_hash
            "op": row["op"],
            "type": row["type"],
            "title": row["title"],
            "tags": tags,
            "plugin_version": "0.3.0",
        }

        return self._intake_client.submit_intake(
            session_id=session_id,
            agent_id=self._intake_agent_id,
            target=target,
            action=action,
            stable_key=intake_stable_key,
            content=row["body"],
            provenance=provenance,
        )
