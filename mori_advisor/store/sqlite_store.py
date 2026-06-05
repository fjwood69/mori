"""SQLiteStore — delegates to the existing MemoryStore, SessionLog, and MsgStore.

This is a thin wrapper: zero SQL changes. All existing SQL stays byte-for-byte
identical inside the composed objects. The store/ layer is the interface;
the composed objects are the implementation.

Also promotes the ad-hoc sqlite3.connect() calls from main.py and ingestion.py
into clean store methods so PostgresStore can implement them properly later.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .base import BaseStore

logger = logging.getLogger(__name__)


class SQLiteStore(BaseStore):
    """Composes MemoryStore + SessionLog + MsgStore behind the BaseStore ABC.

    msg.db lives alongside memories.db in the same data directory.
    WAL mode is already configured inside each composed object.
    """

    def __init__(self, db_path: str | Path, msg_db_path: str | Path | None = None) -> None:
        from mori_advisor.memory_store import MemoryStore
        from mori_advisor.msg_store import MsgStore
        from mori_advisor.session_log import SessionLog

        self.db_path = Path(db_path)
        msg_path = Path(msg_db_path) if msg_db_path else self.db_path.parent / "msg.db"

        self._mem = MemoryStore(self.db_path)
        self._log = SessionLog(self.db_path)
        self._msg = MsgStore(msg_path)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def bootstrap(self) -> None:
        # Schema is owned by the migration runner (single source of truth across
        # both backends). Migration 1 ("baseline") invokes the same MemoryStore/
        # SessionLog/MsgStore bootstrap this method used to call directly, so this
        # is behaviour-preserving for existing databases.
        from .migrations import MIGRATIONS, apply_sqlite

        apply_sqlite(self.db_path, tuple(m for m in MIGRATIONS if m.target == "memories"))
        apply_sqlite(self._msg.db_path, tuple(m for m in MIGRATIONS if m.target == "msg"))

    def ping(self) -> None:
        conn = self._mem._get_conn()
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()

    @contextmanager
    def begin_transaction(self):
        """Yield a sqlite3.Connection inside a DEFERRED transaction.

        Used by DreamPipeline to wrap write() + set_dream_state() + prune_events()
        atomically. The yielded connection is the same object that write(_conn=...)
        and set_dream_state(_conn=...) already accept — no caller changes needed.
        """
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN DEFERRED")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_conn(self) -> sqlite3.Connection:
        """Transitional escape hatch for ad-hoc queries in main.py.

        DEPRECATED: migrate callers to specific store methods.
        """
        return self._mem._get_conn()

    # ── Memory CRUD ────────────────────────────────────────────────────────

    def write(
        self,
        name=None,
        title="",
        description="",
        type="project",
        tier="working",
        body="",
        tags=None,
        origin_session_id=None,
        origin_session_ids=None,
        origin_clients=None,
        client=None,
        _skip_protection=False,
        _conn=None,
    ) -> str:
        return self._mem.write(
            name=name,
            title=title,
            description=description,
            type=type,
            tier=tier,
            body=body,
            tags=tags,
            origin_session_id=origin_session_id,
            origin_session_ids=origin_session_ids,
            origin_clients=origin_clients,
            client=client,
            _skip_protection=_skip_protection,
            _conn=_conn,
        )

    def read(self, name: str) -> str:
        return self._mem.read(name)

    def list(self, type_filter=None, tag=None, session=None, client=None, limit=50) -> str:
        return self._mem.list(
            type_filter=type_filter, tag=tag, session=session, client=client, limit=limit
        )

    def search(
        self, query=None, type_filter=None, tag=None, client=None, since=None, limit=10
    ) -> str:
        return self._mem.search(
            query=query, type_filter=type_filter, tag=tag, client=client, since=since, limit=limit
        )

    def delete(self, name: str) -> str:
        return self._mem.delete(name)

    def export(self, name: str, output_path=None) -> str:
        return self._mem.export(name, output_path=output_path)

    def export_all(self, output_dir: str) -> str:
        return self._mem.export_all(output_dir)

    def import_memories(self, source_dir: str) -> str:
        return self._mem.import_memories(source_dir)

    # ── Memory metadata ────────────────────────────────────────────────────

    def get_memories_by_project(self, project: str, include_global: bool = True) -> dict:
        return self._mem.get_memories_by_project(project, include_global=include_global)

    def get_memories_changed_since(
        self,
        since: str,
        project: str | None = None,
        include_global: bool = True,
        limit: int = 30,
    ) -> list[dict]:
        return self._mem.get_memories_changed_since(
            since, project=project, include_global=include_global, limit=limit
        )

    def session_summary(self, session_id: str) -> str:
        return self._mem.session_summary(session_id)

    def history(self, name: str, limit: int = 10) -> str:
        return self._mem.history(name, limit=limit)

    def diff(self, name: str, from_version: int, to_version: int) -> str:
        return self._mem.diff(name, from_version, to_version)

    def rollback(self, name: str, version_id: int) -> str:
        return self._mem.rollback(name, version_id)

    # ── Counts / observability ─────────────────────────────────────────────

    def count(self, tier: str | None = None, protected: bool | None = None) -> int:
        return self._mem.count(tier=tier, protected=protected)

    def pending_count(self, status: str | None = None) -> int:
        return self._mem.pending_count(status=status)

    def eviction_count(self) -> int:
        return self._mem.eviction_count()

    # ── Approval workflow ──────────────────────────────────────────────────

    def queue_pending_write(
        self,
        name: str,
        title: str = "",
        description: str = "",
        type: str = "project",
        body: str = "",
        tags: list | None = None,
        origin_clients: list | None = None,
        proposed_by: str = "api",
        source: str = "",
        provenance: str | None = None,
        confidence: float | None = None,
        focus_mode: str = "",
        tier: str = "",
    ) -> str:
        return self._mem.queue_pending_write(
            name=name,
            title=title,
            description=description,
            type=type,
            body=body,
            tags=tags,
            origin_clients=origin_clients,
            proposed_by=proposed_by,
            source=source,
            provenance=provenance,
            confidence=confidence,
            focus_mode=focus_mode,
            tier=tier,
        )

    def pending_list(self, status: str = "pending") -> str:
        return self._mem.pending_list(status=status)

    def pending_list_json(self, status: str = "pending") -> list[dict]:
        return self._mem.pending_list_json(status=status)

    def approve(self, write_id: int, note: str = "", reviewer: str = "") -> str:
        return self._mem.approve(write_id, note=note, reviewer=reviewer)

    def reject(self, write_id: int, note: str = "", reviewer: str = "") -> str:
        return self._mem.reject(write_id, note=note, reviewer=reviewer)

    def protect(self, name: str, domains=None) -> str:
        return self._mem.protect(name, domains=domains)

    # ── Freshness and eviction ─────────────────────────────────────────────

    def check_freshness(self, llm_consult, limit: int = 20) -> dict:
        return self._mem.check_freshness(llm_consult, limit=limit)

    def scan_orphans(self, days: int = 30, dry_run: bool = True) -> str:
        return self._mem.scan_orphans(days=days, dry_run=dry_run)

    # ── Internal helpers (transitional) ────────────────────────────────────

    def get_config(self, key: str, default: str = "") -> str:
        return self._mem._get_config(key, default)

    def is_trusted_client(self, client) -> bool:
        return self._mem._is_trusted_client(client)

    def parse_tags(self, raw: str) -> list:
        return self._mem._parse_tags(raw)

    # ── Session / dream ────────────────────────────────────────────────────

    def append_event(
        self,
        session_id,
        event_name,
        client="",
        tool_name=None,
        tool_input=None,
        tool_response=None,
        tool_error=None,
        model=None,
        cwd=None,
        transcript_path=None,
        prompt=None,
        stop_reason=None,
    ) -> int:
        return self._log.append_event(
            session_id=session_id,
            event_name=event_name,
            client=client,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_response=tool_response,
            tool_error=tool_error,
            model=model,
            cwd=cwd,
            transcript_path=transcript_path,
            prompt=prompt,
            stop_reason=stop_reason,
        )

    def append_event_dict(self, session_id: str, event_type: str, data=None) -> None:
        return self._log.append_event_dict(session_id, event_type, data=data)

    def read_events(
        self, session_id=None, since_event_id=None, since=None, client=None, limit=None
    ) -> list:
        return self._log.read_events(
            session_id=session_id,
            since_event_id=since_event_id,
            since=since,
            client=client,
            limit=limit,
        )

    def read_events_grouped(self, since_event_id=None, group_limit=5) -> list:
        return self._log.read_events_grouped(since_event_id=since_event_id, group_limit=group_limit)

    def get_dream_state(self, key: str, default=None):
        return self._log.get_dream_state(key, default=default)

    def set_dream_state(self, key: str, value: str, _conn=None) -> None:
        return self._log.set_dream_state(key, value, _conn=_conn)

    def count_events(self) -> int:
        return self._log.count_events()

    def prune_events(self, before_event_id: int, _conn=None) -> int:
        return self._log.prune_events(before_event_id, _conn=_conn)

    def list_sessions(self) -> list:
        return self._log.list_sessions()

    # ── Ingestion log ──────────────────────────────────────────────────────

    def is_ingested_by_hash(self, file_hash: str, status_filter=None) -> bool:
        """Check if a file with this hash has already been ingested."""
        conn = self._mem._get_conn()
        try:
            q = "SELECT id FROM ingestion_log WHERE source_hash = ? AND dry_run = 0"
            params: list = [file_hash]
            if status_filter:
                q += " AND status = ?"
                params.append(status_filter)
            row = conn.execute(q, params).fetchone()
            return row is not None
        finally:
            conn.close()

    def log_ingestion(
        self,
        source_path,
        source_hash,
        memories_written=0,
        model="",
        focus="all",
        tier="working",
        tags=None,
        dry_run=False,
        error_count=0,
        status="committed",
    ) -> None:
        """Record an ingestion run in the ingestion_log table."""
        tags_json = json.dumps(tags or [])
        conn = self._mem._get_conn()
        try:
            conn.execute(
                """
                INSERT INTO ingestion_log
                    (source_path, source_hash, memories_written, model,
                     focus, tier, tags, dry_run, error_count, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_path,
                    source_hash,
                    memories_written,
                    model,
                    focus,
                    tier,
                    tags_json,
                    1 if dry_run else 0,
                    error_count,
                    status,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_ingestion_status(self, limit: int = 20) -> str:
        """Return a formatted table of recent ingestion runs."""
        conn = self._mem._get_conn()
        try:
            rows = conn.execute(
                """
                SELECT source_path, ingested_at, memories_written, model, focus,
                       tier, dry_run, error_count, status
                FROM ingestion_log
                ORDER BY ingested_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return "No ingestion runs recorded."

        lines = [
            "| Source | When | Memories | Model | Focus | Tier | Status |",
            "|--------|------|----------|-------|-------|------|--------|",
        ]
        for row in rows:
            path, ts, mems, model, focus, tier, dry_run, errors, status = row
            src = Path(path).name if path else "?"
            when = str(ts)[:16] if ts else "?"
            dry = " (dry)" if dry_run else ""
            err = f" ({errors} err)" if errors else ""
            lines.append(
                f"| {src} | {when} | {mems}{err} | {model or '—'} | {focus} | {tier} | {status}{dry} |"
            )
        return "\n".join(lines)

    def count_ingestion(self) -> int:
        return self._mem.count_ingestion()

    # ── Msg ────────────────────────────────────────────────────────────────

    def log_message(self, msg, status: str = "pending") -> None:
        return self._msg.upsert(msg, status=status)

    def set_message_status(self, msg_id: str, status: str) -> None:
        return self._msg.set_status(msg_id, status)

    def get_pending_messages(
        self, hostname, types=None, from_host=None, unacked=False, include_broadcast=True
    ) -> list:
        return self._msg.get_pending(
            hostname,
            types=types,
            from_host=from_host,
            unacked=unacked,
            include_broadcast=include_broadcast,
        )

    def get_message_thread(self, root_id: str) -> list:
        return self._msg.get_thread(root_id)

    def count_messages(self, status: str | None = None) -> int:
        return self._msg.count(status=status)

    # ── Ad-hoc query helpers (promoted from main.py) ───────────────────────

    def get_unresolved_goals(self) -> list:
        """Return memories of type='requirement' with status-pending or status-in-progress tags."""
        conn = self._mem._get_conn()
        try:
            rows = conn.execute(
                """
                SELECT name, title, tags, description
                FROM memories
                WHERE type = 'requirement'
                  AND (tags LIKE '%"status-pending"%' OR tags LIKE '%"status-in-progress"%')
                ORDER BY updated_at DESC
                """,
            ).fetchall()
            return [
                {
                    "name": r[0],
                    "title": r[1],
                    "tags": self._mem._parse_tags(r[2]),
                    "description": r[3],
                }
                for r in rows
            ]
        finally:
            conn.close()

    def get_stale_memories(self, days: int = 90) -> list:
        """Return working-tier memories not retrieved in `days` days."""
        conn = self._mem._get_conn()
        try:
            rows = conn.execute(
                """
                SELECT name, title, type, last_retrieved_at, retrieval_count
                FROM memories
                WHERE tier = 'working'
                  AND last_retrieved_at IS NOT NULL
                  AND last_retrieved_at < datetime('now', ?)
                  AND protected = 0
                ORDER BY last_retrieved_at ASC
                """,
                (f"-{days} days",),
            ).fetchall()
            return [
                {
                    "name": r[0],
                    "title": r[1],
                    "type": r[2],
                    "last_retrieved_at": r[3],
                    "retrieval_count": r[4],
                }
                for r in rows
            ]
        finally:
            conn.close()

    def get_superseded_memories(self) -> list:
        """Return memories that have been marked as superseded."""
        conn = self._mem._get_conn()
        try:
            rows = conn.execute(
                """
                SELECT name, title, superseded_by, updated_at
                FROM memories
                WHERE superseded_by IS NOT NULL AND superseded_by != ''
                ORDER BY updated_at DESC
                """,
            ).fetchall()
            return [
                {"name": r[0], "title": r[1], "superseded_by": r[2], "updated_at": r[3]}
                for r in rows
            ]
        finally:
            conn.close()

    def get_eviction_summary(self) -> list:
        """Return unresolved eviction queue entries."""
        conn = self._mem._get_conn()
        try:
            rows = conn.execute(
                """
                SELECT id, memory_name, reason, detail, detected_at
                FROM eviction_queue
                WHERE resolved = 0
                ORDER BY detected_at DESC
                """,
            ).fetchall()
            return [
                {
                    "id": r[0],
                    "memory_name": r[1],
                    "reason": r[2],
                    "detail": r[3],
                    "detected_at": r[4],
                }
                for r in rows
            ]
        finally:
            conn.close()

    def get_stale_canonical_memories(self) -> list:
        """Return stale/invalid canonical memories."""
        conn = self._mem._get_conn()
        try:
            rows = conn.execute(
                """
                SELECT name, title, freshness_status, freshness_checked_at FROM memories
                WHERE freshness_status IN ('stale', 'no') ORDER BY freshness_checked_at DESC
                """
            ).fetchall()
            return [
                {
                    "name": r[0],
                    "title": r[1],
                    "freshness_status": r[2],
                    "freshness_checked_at": r[3],
                }
                for r in rows
            ]
        finally:
            conn.close()

    def get_eviction_queue_summary(self) -> list:
        """Return a count of eviction queue entries grouped by reason."""
        conn = self._mem._get_conn()
        try:
            rows = conn.execute(
                """
                SELECT reason, COUNT(*), SUM(CASE WHEN resolved THEN 1 ELSE 0 END)
                FROM eviction_queue GROUP BY reason
                """
            ).fetchall()
            return [
                {
                    "reason": r[0],
                    "total": r[1],
                    "resolved": r[2] or 0,
                }
                for r in rows
            ]
        finally:
            conn.close()

    def get_requirements(
        self, project: str = "", status: str = "", tag: str = "", limit: int = 50
    ) -> list:
        """Return requirement memories filtered by project, status, or tag."""
        conn = self._mem._get_conn()
        try:
            sql = "SELECT name, title, tags, description, body FROM memories WHERE type = 'requirement'"
            params = []
            if tag:
                sql += " AND tags LIKE ?"
                params.append(f'%"{tag}"%')
            else:
                if project:
                    sql += " AND tags LIKE ?"
                    params.append(f'%"project-{project}"%')
                if status:
                    sql += " AND tags LIKE ?"
                    params.append(f'%"status-{status}"%')
            sql += " ORDER BY name ASC LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()
            return [
                {
                    "name": r[0],
                    "title": r[1],
                    "tags": r[2],
                    "description": r[3],
                    "body": r[4],
                }
                for r in rows
            ]
        finally:
            conn.close()
