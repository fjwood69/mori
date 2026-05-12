"""Session event log — SQLite-backed append-only event store.

Stored in the same DB file as memories.db (session_events + dream_state tables).
Used by hooks (append) and dream phase (read). Append-only by design:
events are never modified once written.
"""

from __future__ import annotations

import datetime
import json
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


class SessionLog:
    """SQLite-backed event store for session lifecycle events.

    Stores structured event data (tool calls, prompts, errors) in the
    session_events table. Tracks dream phase watermark in dream_state table.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self):
        self._conn = self._connect()
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS session_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id      TEXT NOT NULL,
                event_name      TEXT NOT NULL,
                client          TEXT NOT NULL DEFAULT '',
                timestamp       TEXT NOT NULL,
                tool_name       TEXT,
                tool_input      TEXT,
                tool_response   TEXT,
                tool_error      TEXT,
                model           TEXT,
                cwd             TEXT,
                transcript_path TEXT,
                prompt          TEXT,
                stop_reason     TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_events_session ON session_events(session_id);
            CREATE INDEX IF NOT EXISTS idx_events_time ON session_events(timestamp);
            CREATE INDEX IF NOT EXISTS idx_events_client ON session_events(client);
            CREATE TABLE IF NOT EXISTS dream_state (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        self._conn.commit()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── write ───────────────────────────────────────────────────────────

    def append_event(
        self,
        session_id: str,
        event_name: str,
        client: str = "",
        tool_name: str | None = None,
        tool_input: str | None = None,
        tool_response: str | None = None,
        tool_error: str | None = None,
        model: str | None = None,
        cwd: str | None = None,
        transcript_path: str | None = None,
        prompt: str | None = None,
        stop_reason: str | None = None,
    ) -> int:
        """Insert a new event into the session log.

        Returns the new row id.
        """
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        cur = self._conn.execute(
            """
            INSERT INTO session_events
                (session_id, event_name, client, timestamp,
                 tool_name, tool_input, tool_response, tool_error,
                 model, cwd, transcript_path, prompt, stop_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id, event_name, client, now,
                tool_name, tool_input, tool_response, tool_error,
                model, cwd, transcript_path, prompt, stop_reason,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    # ── compat: old dict-based API for a gradual migration ──────────────

    def append_event_dict(self, session_id: str, event_type: str, data: dict | None = None) -> None:
        """Backwards-compatible wrapper for the old JSONL-based API.

        Maps from the old flat dict format to the structured columns.
        This is the version called by existing code in main.py.
        """
        d = data or {}
        return self.append_event(
            session_id=session_id,
            event_name=event_type,
            client=d.get("client", ""),
            tool_name=d.get("tool_name"),
            tool_input=d.get("tool_input"),
            tool_response=d.get("tool_response"),
            tool_error=d.get("tool_error"),
            model=d.get("model"),
            cwd=d.get("cwd"),
            transcript_path=d.get("transcript_path"),
            prompt=d.get("prompt"),
            stop_reason=d.get("stop_reason"),
        )

    # ── read ────────────────────────────────────────────────────────────

    def read_events(
        self,
        session_id: str | None = None,
        since_event_id: int | None = None,
        since: str | None = None,
        client: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Read events, newest first.

        Args:
            session_id: Filter to a single session.
            since_event_id: Only events with id > this value.
            since: ISO timestamp — only events after this time.
            client: Filter by client hostname.
            limit: Max events to return.

        Returns:
            List of event dicts.
        """
        query = "SELECT * FROM session_events WHERE 1=1"
        params: list = []

        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        if since_event_id is not None:
            query += " AND id > ?"
            params.append(since_event_id)
        if since:
            query += " AND timestamp > ?"
            params.append(since)
        if client:
            query += " AND client = ?"
            params.append(client)

        query += " ORDER BY id DESC"
        if limit:
            query += " LIMIT ?"
            params.append(limit)

        try:
            cur = self._conn.execute(query, params)
            rows = cur.fetchall()
        except sqlite3.Error as e:
            logger.warning("Error querying events: %s", e)
            return []

        return [dict(row) for row in rows]

    def read_events_grouped(
        self,
        since_event_id: int | None = None,
        group_limit: int = 5,
    ) -> list[dict]:
        """Read events grouped by session, newest-first, for dream phase.

        Returns the latest events from each session (up to group_limit per
        session), ordered by session recency. Used by the dream phase to
        get a representative sample of recent activity across all devices.

        Args:
            since_event_id: Only events with id > this value.
            group_limit: Max events to return per session.

        Returns:
            List of event dicts, newest session first.
        """
        query = """
            SELECT * FROM session_events
            WHERE session_id IN (
                SELECT session_id FROM session_events
                WHERE (? IS NULL OR id > ?)
                GROUP BY session_id
                ORDER BY MAX(id) DESC
            )
            AND (? IS NULL OR id > ?)
            ORDER BY session_id, id DESC
        """
        params = [since_event_id, since_event_id, since_event_id, since_event_id]

        try:
            cur = self._conn.execute(query, params)
            rows = cur.fetchall()
        except sqlite3.Error as e:
            logger.warning("Error in grouped query: %s", e)
            return []

        # Group by session client-side
        sessions: dict[str, list[dict]] = {}
        for row in rows:
            d = dict(row)
            sid = d["session_id"]
            if sid not in sessions:
                sessions[sid] = []
            if len(sessions[sid]) < group_limit:
                sessions[sid].append(d)

        # Flatten, newest session first
        result: list[dict] = []
        for sid in sorted(sessions, key=lambda s: max(
            e["id"] for e in sessions[s]
        ), reverse=True):
            result.extend(sessions[sid])

        return result

    # ── dream state watermark ──────────────────────────────────────────

    def get_dream_state(self, key: str, default: str | None = None) -> str | None:
        """Read a value from the dream_state table."""
        cur = self._conn.execute(
            "SELECT value FROM dream_state WHERE key = ?", (key,)
        )
        row = cur.fetchone()
        return row["value"] if row else default

    def set_dream_state(self, key: str, value: str) -> None:
        """Upsert a value into the dream_state table."""
        self._conn.execute(
            "INSERT INTO dream_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    # ── maintenance ────────────────────────────────────────────────────

    def count_events(self) -> int:
        """Total event count (for monitoring)."""
        cur = self._conn.execute("SELECT COUNT(*) FROM session_events")
        return cur.fetchone()[0]

    def prune_events(self, before_event_id: int) -> int:
        """Delete events older than the given event id.

        Returns number of rows deleted.
        """
        cur = self._conn.execute(
            "DELETE FROM session_events WHERE id <= ?", (before_event_id,)
        )
        self._conn.commit()
        return cur.rowcount

    def list_sessions(self) -> list[str]:
        """List all distinct session IDs that have events."""
        cur = self._conn.execute(
            "SELECT DISTINCT session_id FROM session_events ORDER BY session_id"
        )
        return [row["session_id"] for row in cur.fetchall()]

    def clear(self) -> None:
        """Delete ALL events and dream state (irreversible)."""
        self._conn.execute("DELETE FROM session_events")
        self._conn.execute("DELETE FROM dream_state")
        self._conn.commit()