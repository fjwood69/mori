"""BaseStore — abstract interface for the mori persistence layer.

Two implementations:
  SQLiteStore  — default, zero-dep, WAL mode (MORI_DATABASE_URL not set)
  PostgresStore — asyncpg pool, JSONB, TIMESTAMPTZ (MORI_DATABASE_URL=postgresql://...)

All methods that thread a raw connection for atomicity accept an optional
`_conn` parameter — SQLiteStore passes sqlite3.Connection, PostgresStore
passes asyncpg.Connection. Neither caller changes its logic.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Generator


class BaseStore(ABC):
    # ── Lifecycle ──────────────────────────────────────────────────────────

    @abstractmethod
    def bootstrap(self) -> None:
        """Create all tables and indexes. Call once at startup."""

    @abstractmethod
    def ping(self) -> None:
        """Verify DB connectivity. Raise on failure."""

    @abstractmethod
    @contextmanager
    def begin_transaction(self) -> Generator:
        """Context manager that yields a connection for atomic writes.

        Used by DreamPipeline to wrap write() + set_dream_state() + prune_events()
        in a single DEFERRED transaction.

        SQLiteStore yields sqlite3.Connection.
        PostgresStore yields asyncpg.Connection.
        PostgresStore callers must be async — use `async with store.begin_transaction()`.
        """

    def get_conn(self) -> sqlite3.Connection:
        """Transitional escape hatch — returns raw SQLite connection for ad-hoc queries.

        DEPRECATED: Use specific store methods instead.
        Raises NotImplementedError on PostgresStore (async connections only).
        """
        raise NotImplementedError("get_conn() is not supported on this store backend")

    # ── Memory CRUD ────────────────────────────────────────────────────────

    @abstractmethod
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
    ) -> str: ...

    @abstractmethod
    def read(self, name: str) -> str: ...

    @abstractmethod
    def list(self, type_filter=None, tag=None, session=None, client=None, limit=50) -> str: ...

    @abstractmethod
    def search(
        self, query=None, type_filter=None, tag=None, client=None, since=None, limit=10
    ) -> str: ...

    @abstractmethod
    def delete(self, name: str) -> str: ...

    @abstractmethod
    def soft_delete(self, name: str) -> str: ...

    @abstractmethod
    def hard_delete(self, name: str) -> str: ...

    @abstractmethod
    def restore_memory(self, name: str) -> tuple: ...

    @abstractmethod
    def insert_audit(
        self, op: str, actor: str, name: str, content_hash: str, detail: str = ""
    ) -> None: ...

    @abstractmethod
    def get_audit_log(self, memory_name: str = "", actor: str = "", limit: int = 100) -> list: ...

    @abstractmethod
    def export(self, name: str, output_path=None) -> str: ...

    @abstractmethod
    def export_all(self, output_dir: str) -> str: ...

    @abstractmethod
    def import_memories(self, source_dir: str) -> str: ...

    # ── Memory metadata ────────────────────────────────────────────────────

    @abstractmethod
    def get_memories_by_project(self, project: str, include_global: bool = True) -> dict: ...

    @abstractmethod
    def get_memories_changed_since(
        self,
        since: str,
        project: str | None = None,
        include_global: bool = True,
        limit: int = 30,
    ) -> list[dict]: ...

    @abstractmethod
    def session_summary(self, session_id: str) -> str: ...

    @abstractmethod
    def history(self, name: str, limit: int = 10) -> str: ...

    @abstractmethod
    def diff(self, name: str, from_version: int, to_version: int) -> str: ...

    @abstractmethod
    def rollback(self, name: str, version_id: int) -> str: ...

    # ── Counts / observability ─────────────────────────────────────────────

    @abstractmethod
    def count(self, tier: str | None = None, protected: bool | None = None) -> int: ...

    @abstractmethod
    def pending_count(self, status: str | None = None) -> int: ...

    @abstractmethod
    def eviction_count(self) -> int: ...

    # ── Approval workflow ──────────────────────────────────────────────────

    @abstractmethod
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
    ) -> str: ...

    @abstractmethod
    def pending_list(self, status: str = "pending") -> str: ...

    @abstractmethod
    def pending_list_json(self, status: str = "pending") -> list[dict]: ...

    @abstractmethod
    def approve(self, write_id: int, note: str = "", reviewer: str = "") -> str: ...

    @abstractmethod
    def reject(self, write_id: int, note: str = "", reviewer: str = "") -> str: ...

    @abstractmethod
    def protect(self, name: str, domains=None) -> str: ...

    # ── Freshness and eviction ─────────────────────────────────────────────

    @abstractmethod
    def check_freshness(self, llm_consult, limit: int = 20) -> dict: ...

    @abstractmethod
    def scan_orphans(self, days: int = 30, dry_run: bool = True) -> str: ...

    # ── Internal helpers (exposed for transitional callers in main.py) ─────

    @abstractmethod
    def get_config(self, key: str, default: str = "") -> str: ...

    @abstractmethod
    def is_trusted_client(self, client) -> bool: ...

    @abstractmethod
    def parse_tags(self, raw: str) -> list: ...

    # ── Session / dream ────────────────────────────────────────────────────

    @abstractmethod
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
    ) -> int: ...

    @abstractmethod
    def append_event_dict(self, session_id: str, event_type: str, data=None) -> None: ...

    @abstractmethod
    def read_events(
        self, session_id=None, since_event_id=None, since=None, client=None, limit=None
    ) -> list: ...

    @abstractmethod
    def read_events_grouped(self, since_event_id=None, group_limit=5) -> list: ...

    @abstractmethod
    def get_dream_state(self, key: str, default=None): ...

    @abstractmethod
    def set_dream_state(self, key: str, value: str, _conn=None) -> None: ...

    @abstractmethod
    def count_events(self) -> int: ...

    @abstractmethod
    def prune_events(self, before_event_id: int, _conn=None) -> int: ...

    @abstractmethod
    def list_sessions(self) -> list: ...

    # ── Ingestion log ──────────────────────────────────────────────────────

    @abstractmethod
    def is_ingested_by_hash(self, file_hash: str, status_filter=None) -> bool: ...

    @abstractmethod
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
    ) -> None: ...

    @abstractmethod
    def get_ingestion_status(self, limit: int = 20) -> str: ...

    @abstractmethod
    def count_ingestion(self) -> int: ...

    # ── Msg ────────────────────────────────────────────────────────────────

    @abstractmethod
    def log_message(self, msg, status: str = "pending") -> None: ...

    @abstractmethod
    def set_message_status(self, msg_id: str, status: str) -> None: ...

    @abstractmethod
    def get_pending_messages(
        self, hostname, types=None, from_host=None, unacked=False, include_broadcast=True
    ) -> list: ...

    @abstractmethod
    def get_message_thread(self, root_id: str) -> list: ...

    @abstractmethod
    def count_messages(self, status: str | None = None) -> int: ...

    # ── Ad-hoc query helpers (promoted from main.py) ───────────────────────

    @abstractmethod
    def get_unresolved_goals(self) -> list: ...

    @abstractmethod
    def get_stale_memories(self, days: int = 90) -> list: ...

    @abstractmethod
    def get_superseded_memories(self) -> list: ...

    @abstractmethod
    def get_eviction_summary(self) -> list: ...

    @abstractmethod
    def get_stale_canonical_memories(self) -> list: ...

    @abstractmethod
    def get_eviction_queue_summary(self) -> list: ...

    @abstractmethod
    def get_requirements(
        self, project: str = "", status: str = "", tag: str = "", limit: int = 50
    ) -> list: ...
