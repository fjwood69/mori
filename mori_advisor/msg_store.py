"""MsgStore — persistence layer for mori-msg.

Owns msg.db (separate from memories.db). The mori-msg daemon is the sole
writer; mori-advisor reads from it for msg_recv / msg_thread MCP tools.
"""

import sqlite3
from pathlib import Path
from typing import Optional

from .msg import MoriMessage

_SCHEMA = """
CREATE TABLE IF NOT EXISTS msg_log (
    id        TEXT PRIMARY KEY,
    from_host TEXT NOT NULL,
    to_host   TEXT NOT NULL,
    type      TEXT NOT NULL,
    ts        TEXT NOT NULL,
    body      TEXT NOT NULL,
    reply_to  TEXT,
    status    TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_msg_log_to_host ON msg_log (to_host);
CREATE INDEX IF NOT EXISTS idx_msg_log_status  ON msg_log (status);
CREATE INDEX IF NOT EXISTS idx_msg_log_reply_to ON msg_log (reply_to);
"""


class MsgStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._bootstrap()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _bootstrap(self) -> None:
        conn = self._get_conn()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def upsert(self, msg: MoriMessage, status: str = "pending") -> None:
        """Persist a message. INSERT OR IGNORE — safe to call on re-delivery."""
        conn = self._get_conn()
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO msg_log
                    (id, from_host, to_host, type, ts, body, reply_to, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg.id,
                    msg.from_agent,
                    msg.to,
                    msg.type,
                    msg.ts,
                    msg.body,
                    msg.reply_to,
                    status,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def set_status(self, msg_id: str, status: str) -> None:
        """Update the status of a message by ID."""
        conn = self._get_conn()
        try:
            conn.execute(
                "UPDATE msg_log SET status = ? WHERE id = ?",
                (status, msg_id),
            )
            conn.commit()
        finally:
            conn.close()

    def get_pending(
        self,
        hostname: str,
        types: Optional[list] = None,
        from_host: Optional[str] = None,
        unacked: bool = False,
        include_broadcast: bool = True,
    ) -> list[dict]:
        """Return messages addressed to hostname, newest-first."""
        conn = self._get_conn()
        try:
            clauses = []
            params: list = []

            # Addressed to this host or broadcast
            if include_broadcast:
                clauses.append("(to_host = ? OR to_host = 'broadcast')")
                params.append(hostname)
            else:
                clauses.append("to_host = ?")
                params.append(hostname)

            if types:
                placeholders = ",".join("?" * len(types))
                clauses.append(f"type IN ({placeholders})")
                params.extend(types)

            if from_host:
                clauses.append("from_host = ?")
                params.append(from_host)

            if unacked:
                clauses.append("status = 'pending'")

            where = " AND ".join(clauses)
            rows = conn.execute(
                f"SELECT * FROM msg_log WHERE {where} ORDER BY ts DESC",
                params,
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_thread(self, root_id: str) -> list[dict]:
        """Return root message + all replies, chronological order."""
        conn = self._get_conn()
        try:
            root = conn.execute("SELECT * FROM msg_log WHERE id = ?", (root_id,)).fetchone()
            if not root:
                return []
            replies = conn.execute(
                "SELECT * FROM msg_log WHERE reply_to = ? ORDER BY ts",
                (root_id,),
            ).fetchall()
            return [dict(root)] + [dict(r) for r in replies]
        finally:
            conn.close()

    def count(self, status: str | None = None) -> int:
        """Total message count, optionally filtered by status."""
        conn = self._get_conn()
        try:
            if status is not None:
                return conn.execute(
                    "SELECT COUNT(*) FROM msg_log WHERE status = ?", (status,)
                ).fetchone()[0]
            return conn.execute("SELECT COUNT(*) FROM msg_log").fetchone()[0]
        finally:
            conn.close()
