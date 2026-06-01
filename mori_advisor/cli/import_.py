"""mori import — load JSONL export into a BaseStore (SQLite or Postgres).

Usage:
    # SQLite (default, no MORI_DATABASE_URL)
    python -m mori_advisor.cli.import_ /tmp/mori-export.jsonl

    # Postgres
    MORI_DATABASE_URL=postgresql://mori:pw@localhost:5433/mori \\
        python -m mori_advisor.cli.import_ /tmp/mori-export.jsonl

    # Dry run (validate schema compatibility only)
    python -m mori_advisor.cli.import_ /tmp/mori-export.jsonl --dry-run

All inserts use INSERT … ON CONFLICT DO NOTHING (idempotent).
Import order matches export order — foreign keys are satisfied by design.

Postgres note: asyncpg is required. Install with:
    pip install asyncpg>=0.29.0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Columns that are TIMESTAMPTZ in Postgres — must be datetime objects, not strings
_TIMESTAMP_COLS: set[str] = {
    "created_at", "updated_at", "last_retrieved_at", "freshness_checked_at",
    "changed_at", "proposed_at", "reviewed_at", "reviewed_at", "detected_at",
    "ingested_at", "resolved_at", "timestamp", "ts",
}

# Columns that are BOOLEAN in Postgres — SQLite stores 0/1 as int
_BOOL_COLS: set[str] = {"protected", "dry_run", "resolved"}

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")


def _coerce_ts(val: str) -> datetime:
    """Parse an ISO-ish timestamp string → timezone-aware datetime."""
    # Normalise space separator and handle optional fractional seconds
    s = val.replace(" ", "T")
    if not s.endswith("Z") and "+" not in s[10:] and "-" not in s[10:]:
        s += "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        # Fallback: strip timezone, assume UTC
        return datetime.fromisoformat(s[:19]).replace(tzinfo=timezone.utc)


def _coerce_record(record: dict) -> dict:
    """Convert SQLite values to Python types asyncpg requires."""
    out = {}
    for k, v in record.items():
        if k in _TIMESTAMP_COLS and isinstance(v, str) and v and _TS_RE.match(v):
            try:
                out[k] = _coerce_ts(v)
            except Exception:
                out[k] = v
        elif k in _BOOL_COLS and isinstance(v, int):
            out[k] = bool(v)
        else:
            out[k] = v
    return out

KNOWN_TABLES = {
    "dreamer_config", "dream_state", "memories", "memory_versions",
    "pending_writes", "eviction_queue", "ingestion_log", "session_events",
    "msg_log", "delegate_tasks",
}


def _parse_args():
    parser = argparse.ArgumentParser(description="Import mori JSONL export into a store")
    parser.add_argument("source", help="Path to JSONL export file")
    parser.add_argument("--dry-run", action="store_true", help="Validate only, no writes")
    return parser.parse_args()


# ── SQLite import ─────────────────────────────────────────────────────────────

def _sqlite_import(jsonl_path: Path, db_path: Path, dry_run: bool) -> None:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=OFF")  # allow import in any order

    counts: dict[str, int] = {}
    errors: list[str] = []

    with open(jsonl_path) as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as e:
                errors.append(f"line {lineno}: JSON parse error — {e}")
                continue

            table = record.pop("_table", None)
            if table not in KNOWN_TABLES:
                errors.append(f"line {lineno}: unknown table '{table}' — skipped")
                continue

            if dry_run:
                counts[table] = counts.get(table, 0) + 1
                continue

            cols = list(record.keys())
            placeholders = ",".join("?" * len(cols))
            col_list = ",".join(cols)
            try:
                conn.execute(
                    f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({placeholders})",
                    [record[c] for c in cols],
                )
                counts[table] = counts.get(table, 0) + 1
            except sqlite3.Error as e:
                errors.append(f"line {lineno} {table}: {e}")

    if not dry_run:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.commit()
    conn.close()

    if dry_run:
        print("DRY RUN — validation only")
    print(f"Imported {sum(counts.values())} rows:")
    for table, n in sorted(counts.items()):
        print(f"  {table}: {n}")
    if errors:
        print(f"\n{len(errors)} error(s):")
        for e in errors[:20]:
            print(f"  {e}")
        if len(errors) > 20:
            print(f"  ... and {len(errors) - 20} more")


# ── Postgres import ───────────────────────────────────────────────────────────

async def _pg_import(jsonl_path: Path, dsn: str, dry_run: bool) -> None:
    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3, statement_cache_size=0)

    counts: dict[str, int] = {}
    errors: list[str] = []

    with open(jsonl_path) as f:
        lines = f.readlines()

    async with pool.acquire() as conn:
        for lineno, line in enumerate(lines, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as e:
                errors.append(f"line {lineno}: JSON parse error — {e}")
                continue

            table = record.pop("_table", None)
            if table not in KNOWN_TABLES:
                errors.append(f"line {lineno}: unknown table '{table}' — skipped")
                continue

            if dry_run:
                counts[table] = counts.get(table, 0) + 1
                continue

            record = _coerce_record(record)
            cols = list(record.keys())
            placeholders = ",".join(f"${i+1}" for i in range(len(cols)))
            col_list = ",".join(f'"{c}"' for c in cols)
            try:
                await conn.execute(
                    f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
                    *[record[c] for c in cols],
                )
                counts[table] = counts.get(table, 0) + 1
            except Exception as e:
                errors.append(f"line {lineno} {table}: {e}")

    await pool.close()

    if dry_run:
        print("DRY RUN — validation only")
    print(f"Imported {sum(counts.values())} rows:")
    for table, n in sorted(counts.items()):
        print(f"  {table}: {n}")
    if errors:
        print(f"\n{len(errors)} error(s):")
        for e in errors[:20]:
            print(f"  {e}")
        if len(errors) > 20:
            print(f"  ... and {len(errors) - 20} more")


def main() -> None:
    args = _parse_args()
    source = Path(args.source)
    if not source.exists():
        print(f"ERROR: source file not found: {source}", file=sys.stderr)
        sys.exit(1)

    dsn = os.environ.get("MORI_DATABASE_URL", "").strip()
    if dsn.startswith(("postgresql://", "postgres://")):
        asyncio.run(_pg_import(source, dsn, dry_run=args.dry_run))
    else:
        data_dir = os.environ.get("MORI_ADVISOR_DATA", "/data/mori-advisor")
        db_path = Path(data_dir) / "memories.db"
        _sqlite_import(source, db_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
