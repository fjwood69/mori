"""Memory store — SQLite-backed CRUD with upsert semantics, versioning,
attribution, portability, and trusted dreamer protection.

Uses stdlib sqlite3 with WAL mode for concurrent reads. No extra deps.

Data Layout (runtime):
  /data/mori-advisor/
    memories.db          # SQLite database
    exports/             # Exported .md files (YAML frontmatter + body)
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def normalise_since(since: str) -> str:
    """Normalise a `since` boundary to the stored `updated_at` format (UTC).

    Accepts:
      - relative shorthand: "6h", "30m", "7d" (hours / minutes / days)
      - ISO-8601: "2026-06-04T12:34:56+00:00", "...Z", or already-spaced
        "2026-06-04 12:34:56"

    Returns a naive-UTC string "YYYY-MM-DD HH:MM:SS" matching SQLite's
    datetime('now'). This is critical for correctness: `updated_at` is stored
    space-separated, so a raw ISO string with a "T" separator would compare
    GREATER than every stored row (ord('T') > ord(' ')) and silently return
    nothing. Always route a `since` value through here before comparing.

    Raises ValueError on an unparseable value so callers can fall back.
    """
    from datetime import datetime, timedelta, timezone

    s = since.strip()
    if not s:
        raise ValueError("empty since")

    # Relative shorthand
    unit_secs = {"d": 86400, "h": 3600, "m": 60}
    if len(s) >= 2 and s[-1] in unit_secs and s[:-1].lstrip("-").isdigit():
        delta = timedelta(seconds=int(s[:-1]) * unit_secs[s[-1]])
        dt = datetime.now(timezone.utc) - delta
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    # Already in stored form (space-separated, no timezone)
    if "T" not in s and "+" not in s and not s.endswith("Z"):
        # Trust it as-is (e.g. "2026-06-04 12:34:56")
        return s[:19]

    # ISO-8601 — parse, convert to UTC, drop tz and fractional seconds
    iso = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


VALID_TYPES = {"project", "profile", "pattern", "decision", "standard", "requirement"}
VALID_TIERS = {"ephemeral", "working", "canonical"}
DEFAULT_EXPORT_DIR = "exports"
MAX_VERSIONS_PER_MEMORY = 20

FRESHNESS_CHECK_PROMPT = """You are checking if a piece of technical documentation is still accurate.

Memory:
Title: {title}
Tags: {tags}
Body:
{body}

Based on typical project evolution, is this memory still accurate?
Answer with exactly one word: YES, NO, or STALE.

YES = still completely accurate and relevant
NO = no longer accurate, should be ignored or archived
STALE = partially outdated, needs human review before use"""


def _slugify(title: str) -> str:
    """Derive a kebab-case slug from a title string."""
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9-]", "-", slug)  # non-alphanumeric → hyphens
    slug = re.sub(r"-+", "-", slug)  # collapse consecutive hyphens
    slug = slug.strip("-")
    if not slug:
        slug = f"memory-{int(time.time())}"
    return slug


def _make_diff(a: str, b: str) -> str:
    """Generate a unified diff between two strings."""
    a_lines = a.splitlines(keepends=True)
    b_lines = b.splitlines(keepends=True)
    return "".join(difflib.unified_diff(a_lines, b_lines, fromfile="before", tofile="after"))


def _merge_json_arrays(existing: str, new_items: list[str]) -> str:
    """Merge new unique string items into a JSON array string."""
    try:
        existing_list = json.loads(existing) if existing else []
    except (json.JSONDecodeError, TypeError):
        existing_list = []
    if not isinstance(existing_list, list):
        existing_list = []
    merged = list(dict.fromkeys(existing_list + new_items))  # preserve order, dedupe
    return json.dumps(merged)


class MemoryStore:
    """SQLite-backed persistent memory store with WAL mode.

    Uses short-lived per-method connections for safe concurrent access
    in the async FastMCP server context. WAL mode makes open/close fast
    (~1-2ms overhead).
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._init_db()

    @staticmethod
    def bootstrap_schema(db_path: str | Path) -> None:
        """Create all tables, indexes, and config rows.

        Must be called exactly once at process startup, before any
        MemoryStore or SessionLog instances are created. Uses a
        private connection that is closed after the schema is applied.
        """
        import sqlite3

        p = Path(db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")

        conn.execute(
            "CREATE TABLE IF NOT EXISTS memories ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  name TEXT UNIQUE NOT NULL,"
            "  title TEXT NOT NULL,"
            "  description TEXT NOT NULL DEFAULT '',"
            "  type TEXT NOT NULL DEFAULT 'project',"
            "  body TEXT NOT NULL DEFAULT '',"
            "  tags TEXT NOT NULL DEFAULT '[]',"
            "  origin_session_id TEXT,"
            "  created_at TEXT NOT NULL DEFAULT (datetime('now')),"
            "  updated_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
        for col_def in [
            "origin_session_ids TEXT NOT NULL DEFAULT '[]'",
            "origin_clients TEXT NOT NULL DEFAULT '[]'",
            "protected INTEGER NOT NULL DEFAULT 0",
            "protected_domains TEXT NOT NULL DEFAULT '[]'",
            "tier TEXT NOT NULL DEFAULT 'working'",
            "last_retrieved_at TEXT",
            "retrieval_count INTEGER NOT NULL DEFAULT 0",
            "freshness_status TEXT NOT NULL DEFAULT 'unknown'",
            "freshness_checked_at TEXT",
            "superseded_by TEXT",
        ]:
            try:
                conn.execute(f"ALTER TABLE memories ADD COLUMN {col_def}")
            except sqlite3.OperationalError:
                pass

        conn.execute(
            "CREATE TABLE IF NOT EXISTS memory_versions ("
            "  version_id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  memory_name TEXT NOT NULL,"
            "  title TEXT NOT NULL,"
            "  description TEXT NOT NULL DEFAULT '',"
            "  type TEXT NOT NULL DEFAULT 'project',"
            "  body TEXT NOT NULL DEFAULT '',"
            "  tags TEXT NOT NULL DEFAULT '[]',"
            "  origin_session_ids TEXT NOT NULL DEFAULT '[]',"
            "  origin_clients TEXT NOT NULL DEFAULT '[]',"
            "  version_note TEXT NOT NULL DEFAULT '',"
            "  created_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mem_versions_name ON memory_versions(memory_name)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mem_versions_time ON memory_versions(created_at)"
        )
        # Supports the post-compact delta brief (get_memories_changed_since) —
        # turns the updated_at range scan into an index lookup.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_updated_at ON memories(updated_at)")

        conn.execute(
            "CREATE TABLE IF NOT EXISTS pending_writes ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  memory_name TEXT NOT NULL,"
            "  title TEXT NOT NULL,"
            "  description TEXT NOT NULL DEFAULT '',"
            "  type TEXT NOT NULL DEFAULT 'project',"
            "  body TEXT NOT NULL DEFAULT '',"
            "  tags TEXT NOT NULL DEFAULT '[]',"
            "  origin_session_ids TEXT NOT NULL DEFAULT '[]',"
            "  origin_clients TEXT NOT NULL DEFAULT '[]',"
            "  proposed_at TEXT NOT NULL DEFAULT (datetime('now')),"
            "  proposed_by TEXT NOT NULL,"
            "  status TEXT NOT NULL DEFAULT 'pending',"
            "  reviewed_at TEXT,"
            "  reviewed_by TEXT,"
            "  review_note TEXT NOT NULL DEFAULT ''"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_writes(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_memory ON pending_writes(memory_name)")

        conn.execute(
            "CREATE TABLE IF NOT EXISTS dreamer_config ("
            "  key TEXT PRIMARY KEY,"
            "  value TEXT NOT NULL"
            ")"
        )

        conn.execute(
            "CREATE TABLE IF NOT EXISTS eviction_queue ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  memory_name TEXT NOT NULL,"
            "  reason TEXT NOT NULL,"
            "  detail TEXT NOT NULL DEFAULT '',"
            "  detected_at TEXT NOT NULL DEFAULT (datetime('now')),"
            "  resolved INTEGER NOT NULL DEFAULT 0,"
            "  resolved_at TEXT,"
            "  note TEXT NOT NULL DEFAULT ''"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_evict_memory ON eviction_queue(memory_name)")

        conn.execute(
            "CREATE TABLE IF NOT EXISTS ingestion_log ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  source_path TEXT NOT NULL,"
            "  source_hash TEXT NOT NULL,"
            "  ingested_at TEXT NOT NULL DEFAULT (datetime('now')),"
            "  memories_written INTEGER NOT NULL DEFAULT 0,"
            "  model TEXT NOT NULL DEFAULT '',"
            "  focus TEXT NOT NULL DEFAULT 'all',"
            "  tier TEXT NOT NULL DEFAULT 'working',"
            "  tags TEXT NOT NULL DEFAULT '[]',"
            "  dry_run INTEGER NOT NULL DEFAULT 0,"
            "  error_count INTEGER NOT NULL DEFAULT 0,"
            "  status TEXT NOT NULL DEFAULT 'committed'"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ingestion_hash ON ingestion_log(source_hash)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ingestion_path ON ingestion_log(source_path)")

        # Migrate: add status column if it doesn't exist (v0.1.3 -> v0.1.4)
        try:
            conn.execute(
                "ALTER TABLE ingestion_log ADD COLUMN status TEXT NOT NULL DEFAULT 'committed'"
            )
        except sqlite3.OperationalError:
            pass  # Column already exists

        conn.commit()
        conn.close()

    # ── connection management ──────────────────────────────────────────

    def _get_conn(self):
        """Open a short-lived connection with WAL and busy timeout.

        Close after use. WAL mode makes repeated open/close fast
        and avoids single-connection contention in the async server.
        """
        import sqlite3

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self):
        """Just ensure the parent dir exists. Connections are per-method."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    # ── helpers ────────────────────────────────────────────────────────

    def _ensure_type(self, type_val: str) -> str:
        return type_val if type_val in VALID_TYPES else "project"

    def _ensure_tier(self, tier_val: str | None) -> str:
        return tier_val if tier_val in VALID_TIERS else "working"

    def _bump_retrieval(self, name: str) -> None:
        """Increment retrieval_count and update last_retrieved_at for a memory.

        Fire-and-forget — errors are logged, never propagated.
        """
        import sqlite3

        try:
            conn = self._get_conn()
            try:
                conn.execute(
                    """
                    UPDATE memories
                    SET retrieval_count = COALESCE(retrieval_count, 0) + 1,
                        last_retrieved_at = datetime('now')
                    WHERE name = ?
                    """,
                    (name,),
                )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error:
            logger.debug("Failed to bump retrieval for '%s'", name, exc_info=True)

    def _parse_tags(self, raw: str) -> list[str]:
        try:
            return json.loads(raw) if raw else []
        except (json.JSONDecodeError, TypeError):
            logger.warning("Failed to parse tags: %s", raw)
            return []

    def _format_tags(self, tags: list[str]) -> str:
        return json.dumps(tags or [])

    # ── Count methods (for observability) ────────────────────────────────

    def count(self, tier: str | None = None, protected: bool | None = None) -> int:
        """Total memory count, optionally filtered by tier and/or protected status."""
        import sqlite3

        q = "SELECT COUNT(*) FROM memories WHERE 1=1"
        params = []
        if tier is not None:
            q += " AND tier = ?"
            params.append(tier)
        if protected is not None:
            q += " AND protected = ?"
            params.append(1 if protected else 0)

        try:
            conn = self._get_conn()
            try:
                cur = conn.execute(q, params)
                return cur.fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error:
            return 0

    def pending_count(self, status: str | None = None) -> int:
        """Number of pending writes, optionally filtered by status."""
        import sqlite3

        q = "SELECT COUNT(*) FROM pending_writes"
        params = []
        if status is not None:
            q += " WHERE status = ?"
            params.append(status)
        else:
            q += " WHERE status = 'pending'"

        try:
            conn = self._get_conn()
            try:
                cur = conn.execute(q, params)
                return cur.fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error:
            return 0

    def eviction_count(self) -> int:
        """Number of unresolved eviction queue entries."""
        import sqlite3

        try:
            conn = self._get_conn()
            try:
                cur = conn.execute("SELECT COUNT(*) FROM eviction_queue WHERE resolved = 0")
                return cur.fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error:
            return 0

    def count_ingestion(self) -> int:
        """Total ingestion runs logged."""
        import sqlite3

        try:
            conn = self._get_conn()
            try:
                cur = conn.execute("SELECT COUNT(*) FROM ingestion_log")
                return cur.fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error:
            return 0

    def _row_to_dict(self, row) -> dict:
        """Convert a tuple row from SELECT * on memories to a dict.

        Column order (after ALTER TABLE additions at end):
          0:id  1:name  2:title  3:description  4:type  5:body  6:tags
          7:origin_session_id  8:created_at  9:updated_at
          10:origin_session_ids  11:origin_clients  12:protected  13:protected_domains
          14:tier  15:last_retrieved_at  16:retrieval_count
          17:freshness_status  18:freshness_checked_at  19:superseded_by
        """
        return {
            "id": row[0],
            "name": row[1],
            "title": row[2],
            "description": row[3],
            "type": row[4],
            "body": row[5],
            "tags": self._parse_tags(row[6]),
            "origin_session_id": row[7],
            "created_at": row[8],
            "updated_at": row[9],
            "origin_session_ids": self._parse_tags(row[10]) if len(row) > 10 else [],
            "origin_clients": self._parse_tags(row[11]) if len(row) > 11 else [],
            "protected": bool(row[12]) if len(row) > 12 else False,
            "protected_domains": self._parse_tags(row[13]) if len(row) > 13 else [],
            "tier": row[14] if len(row) > 14 else "working",
            "last_retrieved_at": row[15] if len(row) > 15 else None,
            "retrieval_count": row[16] if len(row) > 16 else 0,
            "freshness_status": row[17] if len(row) > 17 else "unknown",
            "freshness_checked_at": row[18] if len(row) > 18 else None,
            "superseded_by": row[19] if len(row) > 19 else None,
        }

    def _memory_not_found(self, name: str) -> str:
        return f"Memory '{name}' not found."

    def _get_config(self, key: str, default: str = "") -> str:
        """Read a config value from dreamer_config table."""
        import sqlite3

        try:
            conn = self._get_conn()
            try:
                cur = conn.execute("SELECT value FROM dreamer_config WHERE key = ?", (key,))
                row = cur.fetchone()
                return row[0] if row else default
            finally:
                conn.close()
        except sqlite3.Error:
            return default

    def _is_trusted_client(self, client: str | None) -> bool:
        """Check if a client hostname is in the trusted_dreamers list."""
        if not client:
            return False
        raw = self._get_config("trusted_clients", "[]")
        try:
            trusted = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return False
        return client in trusted

    def _is_protected(self, name: str, tags: list[str], existing_row) -> bool:
        """Check if a memory write should be treated as protected.

        Returns True if:
          - The existing memory has protected=1, OR
          - Any tag matches a protected_tag_prefix
        """
        import sqlite3

        # Check existing memory flags
        if existing_row:
            try:
                existing_protected = bool(existing_row[12]) if len(existing_row) > 12 else False
                if existing_protected:
                    return True
            except (IndexError, sqlite3.OperationalError):
                pass

        # Check tag prefixes
        raw_prefixes = self._get_config("protected_tag_prefixes", "[]")
        try:
            prefixes = json.loads(raw_prefixes)
        except (json.JSONDecodeError, TypeError):
            prefixes = []

        for tag in tags:
            for prefix in prefixes:
                if tag.startswith(prefix):
                    return True
        return False

    def _snapshot_to_versions(
        self, name: str, version_note: str = "", *, _conn: sqlite3.Connection
    ):
        """Snapshot current memory state into memory_versions before upsert.

        Requires a connection (caller always provides one from write()).
        """
        import sqlite3

        conn = _conn

        try:
            cur = conn.execute(
                "SELECT title, description, type, body, tags, origin_session_ids, origin_clients "
                "FROM memories WHERE name = ?",
                (name,),
            )
            row = cur.fetchone()
        except sqlite3.Error:
            return

        if not row:
            return

        conn.execute(
            """
            INSERT INTO memory_versions
                (memory_name, title, description, type, body, tags,
                 origin_session_ids, origin_clients, version_note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name, row[0], row[1], row[2], row[3], row[4], row[5], row[6], version_note),
        )

        # Prune oldest versions if over limit
        conn.execute(
            """
            DELETE FROM memory_versions
            WHERE version_id IN (
                SELECT version_id FROM memory_versions
                WHERE memory_name = ?
                ORDER BY created_at DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (name, MAX_VERSIONS_PER_MEMORY),
        )

    def _version_has_origin(self, version_id: int) -> bool:
        """Check if a version_id exists in memory_versions."""
        import sqlite3

        conn = self._get_conn()
        try:
            cur = conn.execute("SELECT 1 FROM memory_versions WHERE version_id = ?", (version_id,))
            return cur.fetchone() is not None
        except sqlite3.Error:
            return False
        finally:
            conn.close()

    # ── CRUD methods ───────────────────────────────────────────────────

    def write(
        self,
        name: str | None = None,
        title: str = "",
        description: str = "",
        type: str = "project",
        tier: str = "working",
        body: str = "",
        tags: list[str] | None = None,
        origin_session_id: str | None = None,
        origin_session_ids: list[str] | None = None,
        origin_clients: list[str] | None = None,
        client: str | None = None,
        _skip_protection: bool = False,
        _conn: sqlite3.Connection | None = None,
    ) -> str:
        """Create or update a memory entry (upsert by name).

        If name is omitted, auto-derive from title.

        If the memory is protected and the client is not a trusted dreamer,
        the write is queued as a pending write instead.

        Args:
            _skip_protection: Internal flag to bypass protection checks
                              (used by standards import).
            _conn: Optional connection for transaction-wrapped writes
                   (used by dream pipeline).
        """
        import sqlite3

        close_conn = False
        if _conn is None:
            conn = self._get_conn()
            close_conn = True
        else:
            conn = _conn

        try:
            effective_name = name if name else _slugify(title)
            if not effective_name:
                effective_name = f"memory-{int(time.time())}"

            effective_type = self._ensure_type(type)
            effective_tier = self._ensure_tier(tier)
            tags_list = tags or []
            tags_json = self._format_tags(tags_list)

            # Check if existing memory exists and is protected
            try:
                existing_cur = conn.execute(
                    "SELECT * FROM memories WHERE name = ?", (effective_name,)
                )
                existing_row = existing_cur.fetchone()
            except sqlite3.Error:
                existing_row = None

            if not _skip_protection and self._is_protected(effective_name, tags_list, existing_row):
                if not self._is_trusted_client(client):
                    # Queue as pending write instead
                    conn.execute(
                        """
                        INSERT INTO pending_writes
                            (memory_name, title, description, type, body, tags,
                             origin_session_ids, origin_clients, proposed_by)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            effective_name,
                            title,
                            description,
                            effective_type,
                            body,
                            tags_json,
                            json.dumps(origin_session_ids or []),
                            json.dumps(origin_clients or []),
                            client or "unknown",
                        ),
                    )
                    if close_conn:
                        conn.commit()
                    return (
                        f"Memory '{effective_name}' is protected — "
                        f"change queued as pending write (trusted dreamer review required)."
                    )

            # Snapshot current state before upsert
            self._snapshot_to_versions(effective_name, version_note="updated", _conn=conn)

            # Merge origin arrays for attribution
            if existing_row:
                existing_origin_ids = json.dumps(origin_session_ids or [])
                existing_origin_clients = json.dumps(origin_clients or [])
                if len(existing_row) > 10:
                    existing_origin_ids = _merge_json_arrays(
                        existing_row[10], origin_session_ids or []
                    )
                if len(existing_row) > 11:
                    existing_origin_clients = _merge_json_arrays(
                        existing_row[11], origin_clients or []
                    )
            else:
                existing_origin_ids = json.dumps(origin_session_ids or [])
                existing_origin_clients = json.dumps(origin_clients or [])

            # Check if existing row has the new columns
            if existing_row and len(existing_row) > 10:
                merged_ids = existing_origin_ids
                merged_clients = existing_origin_clients
            else:
                merged_ids = json.dumps(origin_session_ids or [])
                merged_clients = json.dumps(origin_clients or [])

            # Preserve existing protection flags and tier
            protected_val = 0
            protected_domains_val = "[]"
            if existing_row and len(existing_row) > 12:
                protected_val = existing_row[12]
            if existing_row and len(existing_row) > 13:
                protected_domains_val = existing_row[13]

            # Don't downgrade canonical to working
            existing_tier = effective_tier
            if existing_row and len(existing_row) > 14:
                existing_tier_val = existing_row[14]
                if existing_tier_val == "canonical":
                    existing_tier = "canonical"

            try:
                conn.execute(
                    """
                    INSERT INTO memories
                        (name, title, description, type, tier, body, tags, origin_session_id,
                         origin_session_ids, origin_clients, protected, protected_domains)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        title               = excluded.title,
                        description         = excluded.description,
                        type                = excluded.type,
                        tier                = excluded.tier,
                        body                = excluded.body,
                        tags                = excluded.tags,
                        origin_session_id   = COALESCE(excluded.origin_session_id, memories.origin_session_id),
                        origin_session_ids  = excluded.origin_session_ids,
                        origin_clients      = excluded.origin_clients,
                        protected           = excluded.protected,
                        protected_domains   = excluded.protected_domains,
                        updated_at          = datetime('now')
                    """,
                    (
                        effective_name,
                        title,
                        description,
                        effective_type,
                        existing_tier,
                        body,
                        tags_json,
                        origin_session_id,
                        merged_ids,
                        merged_clients,
                        protected_val,
                        protected_domains_val,
                    ),
                )
                if close_conn:
                    conn.commit()
                return f"Memory '{effective_name}' written."
            except sqlite3.Error as e:
                return f"Database error writing memory: {e}"
        finally:
            if close_conn:
                conn.close()

    def read(self, name: str) -> str:
        """Read a memory entry and return a formatted block."""
        import sqlite3

        conn = self._get_conn()
        try:
            cur = conn.execute("SELECT * FROM memories WHERE name = ?", (name,))
            row = cur.fetchone()
        except sqlite3.Error as e:
            return f"Database error: {e}"
        finally:
            conn.close()

        if not row:
            return self._memory_not_found(name)

        m = self._row_to_dict(row)
        self._bump_retrieval(m["name"])
        tags_str = ", ".join(m["tags"]) if m["tags"] else "(none)"
        protected_str = "🔒 Protected" if m.get("protected") else ""
        parts: list[str] = [
            f"# Memory: {m['name']}",
            f"**Title**: {m['title']}",
            f"**Type**: {m['type']}  |  **Tags**: {tags_str} {protected_str}",
            f"**Created**: {m['created_at']}  |  **Updated**: {m['updated_at']}",
            "",
            m["body"] or "(no content)",
        ]
        return "\n".join(parts)

    def list(
        self,
        type_filter: str | None = None,
        tag: str | None = None,
        session: str | None = None,
        client: str | None = None,
        limit: int = 50,
    ) -> str:
        """List memories, optionally filtered by type, tag, session, or client."""
        import sqlite3

        query = "SELECT name, title, type, tags, updated_at FROM memories WHERE 1=1"
        params: list = []

        if type_filter:
            query += " AND type = ?"
            params.append(type_filter)

        if tag:
            query += " AND tags LIKE ?"
            params.append(f'%"{tag}"%')

        if session:
            query += " AND (origin_session_id = ? OR origin_session_ids LIKE ?)"
            params.extend([session, f'%"{session}"%'])

        if client:
            query += " AND origin_clients LIKE ?"
            params.append(f'%"{client}"%')

        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        conn = self._get_conn()
        try:
            cur = conn.execute(query, params)
            rows = cur.fetchall()
        except sqlite3.Error as e:
            return f"Database error: {e}"
        finally:
            conn.close()

        if not rows:
            return "No memories found."

        # Track retrievals
        for row in rows:
            self._bump_retrieval(row[0])

        lines = ["# Memories\n"]
        for row in rows:
            _name, _title, _type, tags_raw, _updated = row
            tags = self._parse_tags(tags_raw)
            tags_str = f"[{', '.join(tags)}]" if tags else ""
            lines.append(f"- **{_name}**: {_title} ({_type}) {tags_str}")
        lines.append(f"\nTotal: {len(rows)} memories")
        return "\n".join(lines)

    def search(
        self,
        query: str | None = None,
        type_filter: str | None = None,
        tag: str | None = None,
        client: str | None = None,
        since: str | None = None,
        limit: int = 10,
    ) -> str:
        """Search memories by keyword across name, title, description, and body.

        Supports additional filtering by type, tag, client, and time frame.
        """
        import sqlite3
        from datetime import datetime, timedelta, timezone

        sql = """
            SELECT name, title, type, tags, updated_at, description, body
            FROM memories WHERE 1=1
        """
        params: list = []

        if query:
            sql += " AND (name LIKE ? OR title LIKE ? OR description LIKE ? OR body LIKE ?)"
            like = f"%{query}%"
            params.extend([like, like, like, like])

        if type_filter:
            sql += " AND type = ?"
            params.append(type_filter)

        if tag:
            sql += " AND tags LIKE ?"
            params.append(f'%"{tag}"%')

        if client:
            sql += " AND origin_clients LIKE ?"
            params.append(f'%"{client}"%')

        if since:
            # Parse "7d" / "30d" shorthand or ISO date
            try:
                if since.endswith("d"):
                    days = int(since[:-1])
                    dt = datetime.now(timezone.utc) - timedelta(days=days)
                    since_iso = dt.strftime("%Y-%m-%d %H:%M:%S")
                else:
                    since_iso = since
                sql += " AND updated_at >= ?"
                params.append(since_iso)
            except (ValueError, TypeError):
                pass  # ignore unparseable since values

        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        conn = self._get_conn()
        try:
            cur = conn.execute(sql, params)
            rows = cur.fetchall()
        except sqlite3.Error as e:
            return f"Database error: {e}"
        finally:
            conn.close()

        if not rows:
            return "No memories found matching your search."

        # Track retrievals
        for row in rows:
            self._bump_retrieval(row[0])

        lines = [
            "| Memory | Category | Updated | Preview |\n|--------|----------|---------|---------|\n"
        ]
        for row in rows:
            _name, _title, _type, tags_raw, _updated, _desc, _body = row
            tags = self._parse_tags(tags_raw)

            # Category: pick the first meaningful tag, else fall back to type
            # "dream-phase", "project", "create", "merge", "delete" are noise — skip them
            category = _type
            for t in tags:
                if t not in (
                    "dream-phase",
                    "project",
                    "create",
                    "merge",
                    "delete",
                    "internal",
                    "create",
                ):
                    category = t
                    break

            body_preview = (_body or _desc or "").strip().split("\n")[0][:80].replace("|", "\\|")
            lines.append(f"| **{_name}** | {category} | {_updated[:10]} | {body_preview} |")

        lines.append(f"\n*{len(rows)} results*")
        return "\n".join(lines)

    def delete(self, name: str) -> str:
        """Delete a memory entry by name."""
        import sqlite3

        conn = self._get_conn()
        try:
            cur = conn.execute("DELETE FROM memories WHERE name = ?", (name,))
            conn.commit()
            if cur.rowcount == 0:
                return self._memory_not_found(name)
            return f"Deleted memory '{name}'."
        except sqlite3.Error as e:
            return f"Database error: {e}"
        finally:
            conn.close()

    def export(self, name: str, output_path: str | None = None) -> str:
        """Export a memory to a .md file with YAML frontmatter.

        Default output: <datadir>/exports/<name>.md
        """
        import sqlite3

        conn = self._get_conn()
        try:
            cur = conn.execute("SELECT * FROM memories WHERE name = ?", (name,))
            row = cur.fetchone()
        except sqlite3.Error as e:
            return f"Database error: {e}"
        finally:
            conn.close()

        if not row:
            return self._memory_not_found(name)

        m = self._row_to_dict(row)

        content = self._memory_to_frontmatter_md(m)

        if output_path:
            out = Path(output_path)
            if not out.is_absolute():
                return f"Export path must be absolute: {output_path}"
        else:
            export_dir = self.db_path.parent / DEFAULT_EXPORT_DIR
            export_dir.mkdir(parents=True, exist_ok=True)
            out = export_dir / f"{m['name']}.md"

        try:
            out.write_text(content, encoding="utf-8")
            return f"Exported to {out}"
        except OSError as e:
            return f"Error writing export file: {e}"

    def _memory_to_frontmatter_md(self, m: dict) -> str:
        """Build a .md string with YAML frontmatter from a memory dict."""
        tags_yaml = json.dumps(m["tags"])
        frontmatter = (
            "---\n"
            f"name: {m['name']}\n"
            f"title: {m['title']}\n"
            f"description: {m['description']}\n"
            f"type: {m['type']}\n"
        )
        if m["tags"]:
            frontmatter += f"tags: {tags_yaml}\n"
        if m.get("origin_session_id"):
            frontmatter += f"originSessionId: {m['origin_session_id']}\n"
        frontmatter += f"created_at: {m['created_at']}\nupdated_at: {m['updated_at']}\n---\n\n"
        return frontmatter + (m["body"] or "")

    # ── Versioning ─────────────────────────────────────────────────────

    def history(self, name: str, limit: int = 10) -> str:
        """List version history for a memory."""
        import sqlite3

        conn = self._get_conn()
        try:
            cur = conn.execute(
                """
                SELECT version_id, version_note, created_at
                FROM memory_versions
                WHERE memory_name = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (name, limit),
            )
            rows = cur.fetchall()
        except sqlite3.Error as e:
            return f"Database error: {e}"
        finally:
            conn.close()

        if not rows:
            return f"No version history for '{name}'."

        lines = [f"# Version History: {name}\n"]
        for vid, note, ts in rows:
            note_str = f" — {note}" if note else ""
            lines.append(f"  {vid}. {ts}{note_str}")
        return "\n".join(lines)

    def diff(self, name: str, from_version: int, to_version: int) -> str:
        """Show unified diff of body between two versions."""
        import sqlite3

        conn = self._get_conn()
        try:
            cur = conn.execute(
                """
                SELECT version_id, body, created_at
                FROM memory_versions
                WHERE version_id IN (?, ?) AND memory_name = ?
                ORDER BY version_id ASC
                """,
                (from_version, to_version, name),
            )
            rows = cur.fetchall()
        except sqlite3.Error as e:
            return f"Database error: {e}"
        finally:
            conn.close()

        if len(rows) < 2:
            return f"Could not find both versions ({from_version}, {to_version}) for '{name}'."

        body_a, body_b = rows[0][1], rows[1][1]
        return _make_diff(body_a, body_b)

    def rollback(self, name: str, version_id: int) -> str:
        """Restore a memory to a previous version.

        Creates a new version entry (rollbacks are themselves versioned).
        """
        import sqlite3

        conn = self._get_conn()
        try:
            cur = conn.execute(
                """
                SELECT title, description, type, body, tags, origin_session_ids, origin_clients
                FROM memory_versions WHERE version_id = ? AND memory_name = ?
                """,
                (version_id, name),
            )
            version_row = cur.fetchone()
        except sqlite3.Error as e:
            return f"Database error: {e}"
        finally:
            conn.close()

        if not version_row:
            return f"Version {version_id} not found for '{name}'."

        # Snapshot current state before rollback
        self._snapshot_to_versions(name, version_note=f"before rollback to v{version_id}")

        conn2 = self._get_conn()
        try:
            conn2.execute(
                """
                UPDATE memories
                SET title = ?, description = ?, type = ?, body = ?, tags = ?,
                    origin_session_ids = ?, origin_clients = ?,
                    updated_at = datetime('now')
                WHERE name = ?
                """,
                (
                    version_row[0],
                    version_row[1],
                    version_row[2],
                    version_row[3],
                    version_row[4],
                    version_row[5],
                    version_row[6],
                    name,
                ),
            )
            conn2.commit()
            return f"Memory '{name}' rolled back to version {version_id}."
        except sqlite3.Error as e:
            return f"Database error during rollback: {e}"
        finally:
            conn2.close()

    # ── Attribution ────────────────────────────────────────────────────

    def get_memories_by_project(
        self,
        project: str,
        include_global: bool = True,
    ) -> dict:
        """Return project-scoped memory sets for brief() tiered loading.

        Filters at the database layer — never loads a superset and filters
        in Python. LIKE on JSON arrays does a full table scan, which is
        acceptable at the current scale (~200 memories); migrate to a
        normalised tags junction table if the store exceeds ~2000 entries.

        Returns:
            project_memories: list of memory dicts tagged project:<name>
                              (canonical + working, non-superseded)
            global_memories:  list of memory dicts tagged scope:global or
                              scope:cross-project, OR type in (profile, pattern)
            other_projects:   list of (project_name, count) tuples for index
        """
        import sqlite3

        tag_value = f"project:{project}"
        conn = self._get_conn()
        try:
            # Project memories — canonical first, then working, most-recent first
            cur = conn.execute(
                """
                SELECT * FROM memories
                WHERE tags LIKE ?
                  AND tier IN ('canonical', 'working')
                  AND (superseded_by IS NULL OR superseded_by = '')
                ORDER BY
                  CASE tier WHEN 'canonical' THEN 0 ELSE 1 END ASC,
                  updated_at DESC
                """,
                (f'%"{tag_value}"%',),
            )
            project_rows = cur.fetchall()

            # Global memories — always loaded regardless of project filter
            global_rows: list = []
            if include_global:
                cur = conn.execute(
                    """
                    SELECT * FROM memories
                    WHERE (
                        tags LIKE '%"scope:global"%'
                        OR tags LIKE '%"scope:cross-project"%'
                        OR type IN ('profile', 'pattern')
                    )
                    AND (superseded_by IS NULL OR superseded_by = '')
                    AND tags NOT LIKE ?
                    ORDER BY tier DESC, updated_at DESC
                    """,
                    (f'%"{tag_value}"%',),  # exclude already-included project memories
                )
                global_rows = cur.fetchall()

            # Other-project index — distinct project: tags from non-matching memories
            cur = conn.execute(
                """
                SELECT tags FROM memories
                WHERE tags LIKE '%"project:%"'
                  AND tags NOT LIKE ?
                  AND (superseded_by IS NULL OR superseded_by = '')
                """,
                (f'%"{tag_value}"%',),
            )
            other_rows = cur.fetchall()
        except sqlite3.Error as e:
            logger.warning("get_memories_by_project failed: %s", e)
            return {"project_memories": [], "global_memories": [], "other_projects": []}
        finally:
            conn.close()

        project_memories = [self._row_to_dict(r) for r in project_rows]
        global_memories = [self._row_to_dict(r) for r in global_rows]

        # Parse other-project tag counts
        other_counts: dict[str, int] = {}
        for (tags_raw,) in other_rows:
            for tag in self._parse_tags(tags_raw):
                if tag.startswith("project:") and tag != tag_value:
                    proj_name = tag[len("project:") :]
                    other_counts[proj_name] = other_counts.get(proj_name, 0) + 1
        other_projects = sorted(other_counts.items(), key=lambda x: x[1], reverse=True)

        # Bump retrievals for loaded memories
        for m in project_memories + global_memories:
            self._bump_retrieval(m["name"])

        return {
            "project_memories": project_memories,
            "global_memories": global_memories,
            "other_projects": other_projects,
        }

    def get_memories_changed_since(
        self,
        since: str,
        project: str | None = None,
        include_global: bool = True,
        limit: int = 30,
    ) -> list[dict]:
        """Return memories updated after `since`, scoped for the post-compact delta.

        Used by `brief(post_compact=True)`. `since` accepts relative shorthand
        ("6h"/"7d") or ISO-8601 and is normalised to the stored UTC format via
        `normalise_since`; the comparison is an exclusive bound
        (`updated_at > since`). See that helper for why normalisation matters.

        Scope mirrors `get_memories_by_project`:
          - project given: project-tagged memories + (when include_global) global
            memories (scope:global / scope:cross-project / profile / pattern)
          - project None: all non-superseded memories changed in the window

        Returns memory dicts ordered most-recent-first, capped at `limit`.
        Best-effort: returns [] on parse/DB error rather than raising.
        """
        try:
            since_norm = normalise_since(since)
        except (ValueError, TypeError):
            return []

        params: list = [since_norm]
        if project:
            tag_value = f"project:{project}"
            scope = "tags LIKE ?"
            params.append(f'%"{tag_value}"%')
            if include_global:
                scope = (
                    "(tags LIKE ? "
                    "OR tags LIKE '%\"scope:global\"%' "
                    "OR tags LIKE '%\"scope:cross-project\"%' "
                    "OR type IN ('profile', 'pattern'))"
                )
            sql = f"""
                SELECT * FROM memories
                WHERE updated_at > ?
                  AND (superseded_by IS NULL OR superseded_by = '')
                  AND {scope}
                ORDER BY updated_at DESC
                LIMIT ?
            """
        else:
            sql = """
                SELECT * FROM memories
                WHERE updated_at > ?
                  AND (superseded_by IS NULL OR superseded_by = '')
                ORDER BY updated_at DESC
                LIMIT ?
            """
        params.append(limit)

        conn = self._get_conn()
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error as e:
            logger.warning("get_memories_changed_since failed: %s", e)
            return []
        finally:
            conn.close()

        return [self._row_to_dict(r) for r in rows]

    def session_summary(self, session_id: str) -> str:
        """Show all memories attributed to a given session."""
        return self.list(session=session_id)

    # ── Portability ────────────────────────────────────────────────────

    def export_all(self, output_dir: str) -> str:
        """Export all memories to .md files in the given directory.

        Also writes a MEMORY.md index file.
        """
        import sqlite3

        conn = self._get_conn()
        try:
            cur = conn.execute("SELECT * FROM memories ORDER BY name")
            rows = cur.fetchall()
        except sqlite3.Error as e:
            return f"Database error: {e}"
        finally:
            conn.close()

        if not rows:
            return "No memories to export."

        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        exported = []
        for row in rows:
            m = self._row_to_dict(row)
            file_path = out / f"{m['name']}.md"
            content = self._memory_to_frontmatter_md(m)
            try:
                file_path.write_text(content, encoding="utf-8")
                exported.append(m["name"])
            except OSError as e:
                logger.warning("Failed to write %s: %s", file_path, e)

        # Write MEMORY.md index
        index_lines = ["# Memory Index\n"]
        for row in rows:
            m = self._row_to_dict(row)
            desc = m.get("description", "") or m["title"]
            index_lines.append(f"- [{m['name']}]({m['name']}.md) — {desc}")
        try:
            (out / "MEMORY.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")
        except OSError as e:
            logger.warning("Failed to write MEMORY.md: %s", e)

        return f"Exported {len(exported)} memories to {output_dir}"

    def import_memories(self, source_dir: str) -> str:
        """Import .md files with YAML frontmatter from a directory.

        Upserts into memories table. Only processes files with valid YAML
        frontmatter blocks (--- delimited).
        """
        src = Path(source_dir)
        if not src.is_dir():
            return f"Directory not found: {source_dir}"

        imported = 0
        errors = 0
        for file_path in sorted(src.glob("*.md")):
            if file_path.name == "MEMORY.md":
                continue
            try:
                content = file_path.read_text(encoding="utf-8")
                parsed = self._parse_frontmatter_md(content)
                if not parsed:
                    errors += 1
                    continue
                result = self.write(**parsed)
                if "written" in result:
                    imported += 1
                else:
                    errors += 1
                    logger.warning("Import failed for %s: %s", file_path.name, result)
            except Exception as e:
                errors += 1
                logger.warning("Error importing %s: %s", file_path.name, e)

        return f"Imported {imported} memories from {source_dir} ({errors} errors)"

    def _parse_frontmatter_md(self, content: str) -> dict | None:
        """Parse a .md file with YAML frontmatter into write() kwargs.

        Handles the CC auto-memory format with camelCase originSessionId.
        """
        # Must start with ---
        if not content.startswith("---"):
            return None
        parts = content.split("---", 2)
        if len(parts) < 3:
            return None

        raw = parts[1].strip()
        body = parts[2].strip()

        # Parse simple key: value pairs (supports quoted values)
        kwargs: dict = {}
        tags_list: list[str] = []
        for line in raw.split("\n"):
            line = line.strip()
            if not line or ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip().strip('"').strip("'")

            if key == "name":
                kwargs["name"] = val
            elif key == "title":
                kwargs["title"] = val
            elif key == "description":
                kwargs["description"] = val
            elif key == "type":
                kwargs["type"] = val
            elif key == "tags":
                try:
                    tags_list = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    tags_list = [t.strip() for t in val.strip("[]").split(",") if t.strip()]
            elif key in ("originSessionId", "origin_session_id"):
                kwargs["origin_session_id"] = val
            elif key == "tier":
                kwargs["tier"] = val

        if not kwargs.get("name"):
            return None

        kwargs["body"] = body
        if tags_list:
            kwargs["tags"] = tags_list
        return kwargs

    # ── Trusted Dreamers ───────────────────────────────────────────────

    def pending_list(self, status: str = "pending") -> str:
        """List pending writes awaiting approval."""
        import sqlite3

        conn = self._get_conn()
        try:
            cur = conn.execute(
                """
                SELECT id, memory_name, title, type, proposed_at, proposed_by, status
                FROM pending_writes
                WHERE status = ?
                ORDER BY proposed_at ASC
                """,
                (status,),
            )
            rows = cur.fetchall()
        except sqlite3.Error as e:
            return f"Database error: {e}"
        finally:
            conn.close()

        if not rows:
            return "No pending writes."

        lines = [f"# Pending Writes ({status})"]
        for row in rows:
            wid, mname, title, mtype, proposed_at, proposed_by, _status = row
            lines.append(
                f"  {wid}. **{mname}**: {title} ({mtype}) — "
                f"proposed by {proposed_by} at {proposed_at}"
            )
        lines.append(f"\nTotal: {len(rows)} pending")
        return "\n".join(lines)

    def approve(self, write_id: int, note: str = "", reviewer: str = "") -> str:
        """Approve a pending write. Applies the change and records reviewer."""
        import sqlite3

        conn = self._get_conn()
        try:
            cur = conn.execute(
                "SELECT * FROM pending_writes WHERE id = ? AND status = 'pending'",
                (write_id,),
            )
            row = cur.fetchone()
        except sqlite3.Error as e:
            return f"Database error: {e}"
        finally:
            conn.close()

        if not row:
            return f"Pending write #{write_id} not found or already processed."

        # Apply the write
        pw = {
            "memory_name": row[1],
            "title": row[2],
            "description": row[3],
            "type": row[4],
            "body": row[5],
            "tags": self._parse_tags(row[6]),
            "origin_session_ids": self._parse_tags(row[7]) if row[7] else [],
            "origin_clients": self._parse_tags(row[8]) if row[8] else [],
        }

        result = self.write(
            name=pw["memory_name"],
            title=pw["title"],
            description=pw["description"],
            type=pw["type"],
            body=pw["body"],
            tags=pw["tags"],
            origin_session_ids=pw["origin_session_ids"],
            origin_clients=pw["origin_clients"],
            client=reviewer or "trusted-dreamer",
        )

        # Update pending write status
        conn2 = self._get_conn()
        try:
            conn2.execute(
                """
                UPDATE pending_writes
                SET status = 'approved', reviewed_at = datetime('now'),
                    reviewed_by = ?, review_note = ?
                WHERE id = ?
                """,
                (reviewer or "trusted-dreamer", note, write_id),
            )
            conn2.commit()
        except sqlite3.Error as e:
            return f"Approved write but failed to update status: {e}"
        finally:
            conn2.close()

        return f"Pending write #{write_id} approved. {result}"

    def reject(self, write_id: int, note: str = "", reviewer: str = "") -> str:
        """Reject a pending write without applying."""
        import sqlite3

        conn = self._get_conn()
        try:
            conn.execute(
                """
                UPDATE pending_writes
                SET status = 'rejected', reviewed_at = datetime('now'),
                    reviewed_by = ?, review_note = ?
                WHERE id = ? AND status = 'pending'
                """,
                (reviewer or "trusted-dreamer", note, write_id),
            )
            if conn.total_changes == 0:
                conn.commit()
                return f"Pending write #{write_id} not found or already processed."
            conn.commit()
            return f"Pending write #{write_id} rejected."
        except sqlite3.Error as e:
            return f"Database error: {e}"
        finally:
            conn.close()

    def protect(self, name: str, domains: list[str] | None = None) -> str:
        """Toggle protection on a memory. Trusted dreamers only."""
        import sqlite3

        conn = self._get_conn()
        try:
            cur = conn.execute(
                "SELECT protected, protected_domains FROM memories WHERE name = ?",
                (name,),
            )
            row = cur.fetchone()
        except sqlite3.Error as e:
            return f"Database error: {e}"
        finally:
            conn.close()

        if not row:
            return self._memory_not_found(name)

        current_protected = bool(row[0]) if row else False
        new_protected = 0 if current_protected else 1
        new_domains = json.dumps(domains or []) if domains else (row[1] if row else "[]")

        conn2 = self._get_conn()
        try:
            conn2.execute(
                "UPDATE memories SET protected = ?, protected_domains = ?, updated_at = datetime('now') WHERE name = ?",
                (new_protected, new_domains, name),
            )
            conn2.commit()
            status = "protected" if new_protected else "unprotected"
            return f"Memory '{name}' is now {status}."
        except sqlite3.Error as e:
            return f"Database error: {e}"
        finally:
            conn2.close()

    # ── Freshness and eviction ─────────────────────────────────────────

    def check_freshness(
        self,
        llm_consult: callable,
        limit: int = 20,
    ) -> dict:
        """Run freshness validation on canonical memories tagged with
        infrastructure/dependency/tooling/config tags.

        Uses the provided llm_consult(system, user) callable (e.g.
        BifrostClient.consult) to validate each candidate.

        Returns {"checked": int, "fresh": int, "stale": int, "no": int, "errors": int}.
        """
        import sqlite3

        cand_tag_patterns = ["infrastructure", "dependency", "tooling", "config"]
        like_clauses = " OR ".join(["tags LIKE ?" for _ in cand_tag_patterns])
        params = [f'%"{t}"%' for t in cand_tag_patterns]

        conn = self._get_conn()
        try:
            cur = conn.execute(
                f"""
                SELECT * FROM memories
                WHERE tier = 'canonical'
                  AND freshness_status IN ('unknown', 'fresh')
                  AND ({like_clauses})
                ORDER BY freshness_checked_at IS NULL DESC, freshness_checked_at ASC
                LIMIT ?
                """,
                params + [limit],
            )
            rows = cur.fetchall()
        except sqlite3.Error as e:
            logger.warning("Freshness check query failed: %s", e)
            return {"checked": 0, "fresh": 0, "stale": 0, "no": 0, "errors": 1}
        finally:
            conn.close()

        results = {"checked": 0, "fresh": 0, "stale": 0, "no": 0, "errors": 0}

        for row in rows:
            m = self._row_to_dict(row)
            try:
                prompt = FRESHNESS_CHECK_PROMPT.format(
                    title=m["title"],
                    tags=", ".join(m["tags"]),
                    body=m["body"][:2000],
                )
                response = llm_consult(
                    system=prompt,
                    user=m["name"],
                    vk="fast",
                    max_tokens=10,
                    temperature=0.0,
                )
                status = (response or "").strip().upper()
                normalized = "fresh"
                if status == "NO":
                    normalized = "no"
                elif status == "STALE":
                    normalized = "stale"

                write_conn = self._get_conn()
                try:
                    write_conn.execute(
                        "UPDATE memories SET freshness_status = ?, freshness_checked_at = datetime('now') WHERE name = ?",
                        (normalized, m["name"]),
                    )
                    write_conn.commit()
                finally:
                    write_conn.close()

                results["checked"] += 1
                if normalized == "fresh":
                    results["fresh"] += 1
                elif normalized == "stale":
                    results["stale"] += 1
                elif normalized == "no":
                    results["no"] += 1
            except Exception as e:
                logger.warning("Freshness check failed for '%s': %s", m["name"], e)
                results["errors"] += 1

        return results

    def scan_orphans(self, days: int = 30, dry_run: bool = True) -> str:
        """Find working-tier memories not retrieved in `days` days.

        In non-dry-run mode, adds entries to eviction_queue.
        Returns a formatted dashboard string.
        """
        import sqlite3

        conn = self._get_conn()
        try:
            cur = conn.execute(
                """
                SELECT name, title, type, last_retrieved_at, retrieval_count
                FROM memories
                WHERE (tier IS NULL OR tier != 'canonical')
                  AND last_retrieved_at IS NOT NULL
                  AND last_retrieved_at < datetime('now', ?)
                  AND protected = 0
                ORDER BY last_retrieved_at ASC
                """,
                (f"-{days} days",),
            )
            rows = cur.fetchall()
        except sqlite3.Error as e:
            return f"Orphan scan failed: {e}"
        finally:
            conn.close()

        if not rows:
            return f"# Orphan Scan ({days}d window)\n\nNo orphans found."

        parts = [f"# Orphan Scan ({days}d window)\n\n## Flagged for review\n"]
        for name, title, mtype, last_retrieved, count in rows:
            parts.append(
                f"- **{name}**: {title} ({mtype}) — "
                f"last retrieved {last_retrieved}, {count} retrievals"
            )
            if not dry_run:
                write_conn = self._get_conn()
                try:
                    write_conn.execute(
                        "INSERT INTO eviction_queue (memory_name, reason, detail) VALUES (?, 'orphan', ?)",
                        (name, f"Not retrieved in {days} days. Last: {last_retrieved}"),
                    )
                    write_conn.commit()
                finally:
                    write_conn.close()

        parts.append(f"\nTotal: {len(rows)} orphan{'s' if len(rows) != 1 else ''} flagged")
        return "\n".join(parts)
