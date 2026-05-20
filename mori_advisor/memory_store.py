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
import time
from pathlib import Path

logger = logging.getLogger(__name__)

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

    Each instance owns a single connection. All methods return string
    responses suitable for MCP tool output.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._conn = None
        self._conn = self._connect()

    # ── connection management ──────────────────────────────────────────

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

        # Main memories table
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                name              TEXT UNIQUE NOT NULL,
                title             TEXT NOT NULL,
                description       TEXT NOT NULL DEFAULT '',
                type              TEXT NOT NULL DEFAULT 'project',
                body              TEXT NOT NULL DEFAULT '',
                tags              TEXT NOT NULL DEFAULT '[]',
                origin_session_id TEXT,
                created_at        TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )

        # Add new columns for attribution, protection, eviction (safe to run if exist)
        new_columns = [
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
        ]
        for col_def in new_columns:
            col_name = col_def.split()[0]
            try:
                conn.execute(f"ALTER TABLE memories ADD COLUMN {col_def}")
            except sqlite3.OperationalError:
                pass  # column already exists

        # Memory versions table
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_versions (
                version_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_name        TEXT NOT NULL,
                title              TEXT NOT NULL,
                description        TEXT NOT NULL DEFAULT '',
                type               TEXT NOT NULL DEFAULT 'project',
                body               TEXT NOT NULL DEFAULT '',
                tags               TEXT NOT NULL DEFAULT '[]',
                origin_session_ids TEXT NOT NULL DEFAULT '[]',
                origin_clients     TEXT NOT NULL DEFAULT '[]',
                version_note       TEXT NOT NULL DEFAULT '',
                created_at         TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mem_versions_name ON memory_versions(memory_name)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mem_versions_time ON memory_versions(created_at)"
        )

        # Pending writes table (trusted dreamer approvals)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_writes (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_name        TEXT NOT NULL,
                title              TEXT NOT NULL,
                description        TEXT NOT NULL DEFAULT '',
                type               TEXT NOT NULL DEFAULT 'project',
                body               TEXT NOT NULL DEFAULT '',
                tags               TEXT NOT NULL DEFAULT '[]',
                origin_session_ids TEXT NOT NULL DEFAULT '[]',
                origin_clients     TEXT NOT NULL DEFAULT '[]',
                proposed_at        TEXT NOT NULL DEFAULT (datetime('now')),
                proposed_by        TEXT NOT NULL,
                status             TEXT NOT NULL DEFAULT 'pending',
                reviewed_at        TEXT,
                reviewed_by        TEXT,
                review_note        TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_writes(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_memory ON pending_writes(memory_name)"
        )

        # Dreamer config table
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dreamer_config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )

        # Eviction queue table
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS eviction_queue (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_name TEXT NOT NULL,
                reason      TEXT NOT NULL,
                detail      TEXT NOT NULL DEFAULT '',
                detected_at TEXT NOT NULL DEFAULT (datetime('now')),
                resolved    INTEGER NOT NULL DEFAULT 0,
                resolved_at TEXT,
                note        TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_evict_memory ON eviction_queue(memory_name)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_evict_status ON eviction_queue(resolved)"
        )

        # Seed default trusted dreamer config (overridable via env)
        import os
        default_trusted = os.environ.get(
            "MORI_TRUSTED_DREAMERS",
            "[]",
        )
        conn.execute(
            "INSERT OR IGNORE INTO dreamer_config (key, value) VALUES (?, ?)",
            ("trusted_clients", default_trusted),
        )
        conn.execute(
            "INSERT OR IGNORE INTO dreamer_config (key, value) VALUES (?, ?)",
            ("protected_tag_prefixes", '["infra", "reference", "standard"]'),
        )

        conn.commit()
        conn.close()

    def _connect(self):
        import sqlite3

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _initialize(self):
        """Open the connection. Schema is bootstrapped by bootstrap_schema()."""
        import sqlite3

        self._conn = self._connect()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

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
            self._conn.execute(
                """
                UPDATE memories
                SET retrieval_count = COALESCE(retrieval_count, 0) + 1,
                    last_retrieved_at = datetime('now')
                WHERE name = ?
                """,
                (name,),
            )
            self._conn.commit()
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

    def count(self) -> int:
        """Total memory count."""
        import sqlite3
        try:
            cur = self._conn.execute("SELECT COUNT(*) FROM memories")
            return cur.fetchone()[0]
        except sqlite3.Error:
            return 0

    def pending_count(self) -> int:
        """Number of pending writes awaiting approval."""
        import sqlite3
        try:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM pending_writes WHERE status = 'pending'"
            )
            return cur.fetchone()[0]
        except sqlite3.Error:
            return 0

    def eviction_count(self) -> int:
        """Number of unresolved eviction queue entries."""
        import sqlite3
        try:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM eviction_queue WHERE resolved = 0"
            )
            return cur.fetchone()[0]
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
            cur = self._conn.execute(
                "SELECT value FROM dreamer_config WHERE key = ?", (key,)
            )
            row = cur.fetchone()
            return row[0] if row else default
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

    def _snapshot_to_versions(self, name: str, version_note: str = "", *, _conn: sqlite3.Connection | None = None):
        """Snapshot current memory state into memory_versions before upsert."""
        import sqlite3

        conn = _conn or self._conn

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

        try:
            cur = self._conn.execute(
                "SELECT 1 FROM memory_versions WHERE version_id = ?", (version_id,)
            )
            return cur.fetchone() is not None
        except sqlite3.Error:
            return False

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

        conn = _conn or self._conn

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
                if not _conn:
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
                    effective_name, title, description, effective_type, existing_tier, body,
                    tags_json, origin_session_id,
                    merged_ids, merged_clients,
                    protected_val, protected_domains_val,
                ),
            )
            if not _conn:
                conn.commit()
            return f"Memory '{effective_name}' written."
        except sqlite3.Error as e:
            return f"Database error writing memory: {e}"

    def read(self, name: str) -> str:
        """Read a memory entry and return a formatted block."""
        import sqlite3

        try:
            cur = self._conn.execute(
                "SELECT * FROM memories WHERE name = ?", (name,)
            )
            row = cur.fetchone()
        except sqlite3.Error as e:
            return f"Database error: {e}"

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

        try:
            cur = self._conn.execute(query, params)
            rows = cur.fetchall()
        except sqlite3.Error as e:
            return f"Database error: {e}"

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
        from datetime import datetime, timezone, timedelta

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

        try:
            cur = self._conn.execute(sql, params)
            rows = cur.fetchall()
        except sqlite3.Error as e:
            return f"Database error: {e}"

        if not rows:
            return "No memories found matching your search."

        # Track retrievals
        for row in rows:
            self._bump_retrieval(row[0])

        lines = ["| Memory | Category | Updated | Preview |\n|--------|----------|---------|---------|\n"]
        for row in rows:
            _name, _title, _type, tags_raw, _updated, _desc, _body = row
            tags = self._parse_tags(tags_raw)

            # Category: pick the first meaningful tag, else fall back to type
            # "dream-phase", "project", "create", "merge", "delete" are noise — skip them
            category = _type
            for t in tags:
                if t not in ("dream-phase", "project", "create", "merge", "delete", "internal", "create"):
                    category = t
                    break

            body_preview = (_body or _desc or "").strip().split("\n")[0][:80].replace("|", "\\|")
            title_clean = _title[:50].replace("|", "\\|")
            lines.append(f"| **{_name}** | {category} | {_updated[:10]} | {body_preview} |")

        lines.append(f"\n*{len(rows)} results*")
        return "\n".join(lines)

    def delete(self, name: str) -> str:
        """Delete a memory entry by name."""
        import sqlite3

        try:
            cur = self._conn.execute(
                "DELETE FROM memories WHERE name = ?", (name,)
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return self._memory_not_found(name)
            return f"Deleted memory '{name}'."
        except sqlite3.Error as e:
            return f"Database error: {e}"

    def export(self, name: str, output_path: str | None = None) -> str:
        """Export a memory to a .md file with YAML frontmatter.

        Default output: <datadir>/exports/<name>.md
        """
        import sqlite3

        try:
            cur = self._conn.execute(
                "SELECT * FROM memories WHERE name = ?", (name,)
            )
            row = cur.fetchone()
        except sqlite3.Error as e:
            return f"Database error: {e}"

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
        frontmatter += (
            f"created_at: {m['created_at']}\n"
            f"updated_at: {m['updated_at']}\n"
            "---\n\n"
        )
        return frontmatter + (m["body"] or "")

    # ── Versioning ─────────────────────────────────────────────────────

    def history(self, name: str, limit: int = 10) -> str:
        """List version history for a memory."""
        import sqlite3

        try:
            cur = self._conn.execute(
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

        try:
            cur = self._conn.execute(
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

        if len(rows) < 2:
            return f"Could not find both versions ({from_version}, {to_version}) for '{name}'."

        body_a, body_b = rows[0][1], rows[1][1]
        return _make_diff(body_a, body_b)

    def rollback(self, name: str, version_id: int) -> str:
        """Restore a memory to a previous version.

        Creates a new version entry (rollbacks are themselves versioned).
        """
        import sqlite3

        try:
            cur = self._conn.execute(
                """
                SELECT title, description, type, body, tags, origin_session_ids, origin_clients
                FROM memory_versions WHERE version_id = ? AND memory_name = ?
                """,
                (version_id, name),
            )
            version_row = cur.fetchone()
        except sqlite3.Error as e:
            return f"Database error: {e}"

        if not version_row:
            return f"Version {version_id} not found for '{name}'."

        # Snapshot current state before rollback
        self._snapshot_to_versions(name, version_note=f"before rollback to v{version_id}")

        try:
            self._conn.execute(
                """
                UPDATE memories
                SET title = ?, description = ?, type = ?, body = ?, tags = ?,
                    origin_session_ids = ?, origin_clients = ?,
                    updated_at = datetime('now')
                WHERE name = ?
                """,
                (
                    version_row[0], version_row[1], version_row[2], version_row[3],
                    version_row[4], version_row[5], version_row[6], name,
                ),
            )
            self._conn.commit()
            return f"Memory '{name}' rolled back to version {version_id}."
        except sqlite3.Error as e:
            return f"Database error during rollback: {e}"

    # ── Attribution ────────────────────────────────────────────────────

    def session_summary(self, session_id: str) -> str:
        """Show all memories attributed to a given session."""
        return self.list(session=session_id)

    # ── Portability ────────────────────────────────────────────────────

    def export_all(self, output_dir: str) -> str:
        """Export all memories to .md files in the given directory.

        Also writes a MEMORY.md index file.
        """
        import sqlite3

        try:
            cur = self._conn.execute("SELECT * FROM memories ORDER BY name")
            rows = cur.fetchall()
        except sqlite3.Error as e:
            return f"Database error: {e}"

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

        try:
            cur = self._conn.execute(
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

        try:
            cur = self._conn.execute(
                "SELECT * FROM pending_writes WHERE id = ? AND status = 'pending'",
                (write_id,),
            )
            row = cur.fetchone()
        except sqlite3.Error as e:
            return f"Database error: {e}"

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
        try:
            self._conn.execute(
                """
                UPDATE pending_writes
                SET status = 'approved', reviewed_at = datetime('now'),
                    reviewed_by = ?, review_note = ?
                WHERE id = ?
                """,
                (reviewer or "trusted-dreamer", note, write_id),
            )
            self._conn.commit()
        except sqlite3.Error as e:
            return f"Approved write but failed to update status: {e}"

        return f"Pending write #{write_id} approved. {result}"

    def reject(self, write_id: int, note: str = "", reviewer: str = "") -> str:
        """Reject a pending write without applying."""
        import sqlite3

        try:
            self._conn.execute(
                """
                UPDATE pending_writes
                SET status = 'rejected', reviewed_at = datetime('now'),
                    reviewed_by = ?, review_note = ?
                WHERE id = ? AND status = 'pending'
                """,
                (reviewer or "trusted-dreamer", note, write_id),
            )
            if self._conn.total_changes == 0:
                self._conn.commit()
                return f"Pending write #{write_id} not found or already processed."
            self._conn.commit()
            return f"Pending write #{write_id} rejected."
        except sqlite3.Error as e:
            return f"Database error: {e}"

    def protect(self, name: str, domains: list[str] | None = None) -> str:
        """Toggle protection on a memory. Trusted dreamers only.

        Args:
            name: Memory name to protect/unprotect.
            domains: Tag prefixes that trigger protection. None = no change.
        """
        import sqlite3

        try:
            cur = self._conn.execute(
                "SELECT protected, protected_domains FROM memories WHERE name = ?",
                (name,),
            )
            row = cur.fetchone()
        except sqlite3.Error as e:
            return f"Database error: {e}"

        if not row:
            return self._memory_not_found(name)

        current_protected = bool(row[0]) if row else False
        new_protected = 0 if current_protected else 1
        new_domains = json.dumps(domains or []) if domains else (row[1] if row else "[]")

        try:
            self._conn.execute(
                "UPDATE memories SET protected = ?, protected_domains = ?, updated_at = datetime('now') WHERE name = ?",
                (new_protected, new_domains, name),
            )
            self._conn.commit()
            status = "protected" if new_protected else "unprotected"
            return f"Memory '{name}' is now {status}."
        except sqlite3.Error as e:
            return f"Database error: {e}"

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

        try:
            cur = self._conn.execute(
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
                    max_tokens=10,
                    temperature=0.0,
                )
                status = (response or "").strip().upper()
                normalized = "fresh"
                if status == "NO":
                    normalized = "no"
                elif status == "STALE":
                    normalized = "stale"

                self._conn.execute(
                    "UPDATE memories SET freshness_status = ?, freshness_checked_at = datetime('now') WHERE name = ?",
                    (normalized, m["name"]),
                )
                self._conn.commit()
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

        try:
            cur = self._conn.execute(
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

        if not rows:
            return f"# Orphan Scan ({days}d window)\n\nNo orphans found."

        parts = [f"# Orphan Scan ({days}d window)\n\n## Flagged for review\n"]
        for name, title, mtype, last_retrieved, count in rows:
            parts.append(
                f"- **{name}**: {title} ({mtype}) — "
                f"last retrieved {last_retrieved}, {count} retrievals"
            )
            if not dry_run:
                self._conn.execute(
                    "INSERT INTO eviction_queue (memory_name, reason, detail) VALUES (?, 'orphan', ?)",
                    (name, f"Not retrieved in {days} days. Last: {last_retrieved}"),
                )
        if not dry_run:
            self._conn.commit()

        parts.append(f"\nTotal: {len(rows)} orphan{'s' if len(rows) != 1 else ''} flagged")
        return "\n".join(parts)
