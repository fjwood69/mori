"""mori export — dump SQLite memories.db to JSONL for Postgres import.

Usage:
    python -m mori_advisor.cli.export [--db PATH] [--output PATH] [--since ISO_DATE] [--dry-run]

Default db:     $MORI_ADVISOR_DATA/memories.db (or /data/mori-advisor/memories.db)
Default output: /tmp/mori-export.jsonl

Export order (dependency-safe):
    1. dreamer_config
    2. dream_state
    3. memories
    4. memory_versions, pending_writes, eviction_queue
    5. ingestion_log
    6. session_events  (last 90 days unless --all or --since)
    7. msg_log         (if msg.db exists alongside memories.db)
    8. delegate_tasks  (if table exists)

Runs PRAGMA wal_checkpoint(TRUNCATE) before reading to flush the WAL.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _export_table(
    conn: sqlite3.Connection,
    table: str,
    out,
    where: str = "",
    params: list | None = None,
) -> int:
    if not _table_exists(conn, table):
        return 0
    q = f"SELECT * FROM {table}"
    if where:
        q += f" WHERE {where}"
    rows = conn.execute(q, params or []).fetchall()
    for row in rows:
        record = dict(row)
        record["_table"] = table
        out.write(json.dumps(record, default=str) + "\n")
    return len(rows)


def run_export(
    db_path: Path, output_path: Path, since: str | None, include_all: bool, dry_run: bool
) -> None:
    if not db_path.exists():
        print(f"ERROR: database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = _connect(db_path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # Determine session_events cutoff
    if include_all or not since:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    else:
        cutoff = since

    counts: dict[str, int] = {}

    if dry_run:
        print("DRY RUN — counting rows only, no file written")
        for table in (
            "dreamer_config",
            "dream_state",
            "memories",
            "memory_versions",
            "pending_writes",
            "eviction_queue",
            "ingestion_log",
        ):
            if _table_exists(conn, table):
                n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                counts[table] = n
                print(f"  {table}: {n}")
        if _table_exists(conn, "session_events"):
            n = conn.execute(
                "SELECT COUNT(*) FROM session_events WHERE timestamp >= ?", (cutoff,)
            ).fetchone()[0]
            counts["session_events"] = n
            print(f"  session_events (since {cutoff[:10]}): {n}")
        msg_db = db_path.parent / "msg.db"
        if msg_db.exists():
            mc = _connect(msg_db)
            if _table_exists(mc, "msg_log"):
                n = mc.execute("SELECT COUNT(*) FROM msg_log").fetchone()[0]
                counts["msg_log"] = n
                print(f"  msg_log: {n}")
            mc.close()
        conn.close()
        print(f"\nTotal rows: {sum(counts.values())}")
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as out:
        for table in ("dreamer_config", "dream_state"):
            n = _export_table(conn, table, out)
            counts[table] = n

        n = _export_table(conn, "memories", out)
        counts["memories"] = n

        for table in ("memory_versions", "pending_writes", "eviction_queue", "ingestion_log"):
            n = _export_table(conn, table, out)
            counts[table] = n

        # session_events — time-filtered
        if _table_exists(conn, "session_events"):
            if include_all:
                n = _export_table(conn, "session_events", out)
            else:
                n = _export_table(conn, "session_events", out, "timestamp >= ?", [cutoff])
            counts["session_events"] = n

        if _table_exists(conn, "delegate_tasks"):
            n = _export_table(conn, "delegate_tasks", out)
            counts["delegate_tasks"] = n

    conn.close()

    # msg_log — separate db file
    msg_db = db_path.parent / "msg.db"
    if msg_db.exists():
        mc = _connect(msg_db)
        if _table_exists(mc, "msg_log"):
            with open(output_path, "a") as out:
                n = _export_table(mc, "msg_log", out)
            counts["msg_log"] = n
        mc.close()

    total = sum(counts.values())
    print(f"Exported {total} rows to {output_path}")
    for table, n in counts.items():
        print(f"  {table}: {n}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export mori SQLite DB to JSONL")
    parser.add_argument("--db", help="Path to memories.db", default=None)
    parser.add_argument("--output", default="/tmp/mori-export.jsonl", help="Output JSONL path")
    parser.add_argument(
        "--since", help="ISO date cutoff for session_events (default: 90 days ago)", default=None
    )
    parser.add_argument(
        "--all", dest="include_all", action="store_true", help="Include all session_events"
    )
    parser.add_argument("--dry-run", action="store_true", help="Count rows only, no file written")
    args = parser.parse_args()

    if args.db:
        db_path = Path(args.db)
    else:
        data_dir = os.environ.get("MORI_ADVISOR_DATA", "/data/mori-advisor")
        db_path = Path(data_dir) / "memories.db"

    run_export(
        db_path=db_path,
        output_path=Path(args.output),
        since=args.since,
        include_all=args.include_all,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
