"""One-time idempotent migration: add project:<name> and scope:global tags to existing memories.

Run once, then retire. Re-running is safe — tags already present are skipped.

Usage:
    python scripts/backfill_project_tags.py /path/to/memories.db [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# Map name prefix → project tag. Order matters: first match wins.
# These heuristics are only valid for the initial backfill — do not copy into runtime code.
PROJECT_RULES: list[tuple[str, str]] = [
    ("project-mori-", "project:mori"),
    ("project-bifrost-", "project:bifrost"),
    ("project-dotfiles-", "project:dotfiles"),
    ("project-ai-stack-", "project:ai-stack"),
    ("project-tfl-", "project:tfl"),
    ("project-monitoring-", "project:monitoring"),
]

# Names/types that are genuinely cross-project → scope:global
GLOBAL_TYPES: tuple[str, ...] = ("profile", "pattern")
GLOBAL_NAME_PREFIXES: tuple[str, ...] = (
    "hard-rules",
    "device-inventory",
    "nats-reference",
    "cc-share-reference",
    "standard-network",
    "session-",
    "nist-",
    "ssdf-",
)
GLOBAL_TAG_KEYWORDS: tuple[str, ...] = ("nist", "ssdf", "security-baseline")


def _load_tags(raw: str) -> list[str]:
    try:
        val = json.loads(raw or "[]")
        return val if isinstance(val, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _dump_tags(tags: list[str]) -> str:
    return json.dumps(tags)


def _infer_project_tag(name: str) -> str | None:
    for prefix, tag in PROJECT_RULES:
        if name.startswith(prefix):
            return tag
    return None


def _should_be_global(name: str, mem_type: str, existing_tags: list[str]) -> bool:
    if mem_type in GLOBAL_TYPES:
        return True
    for prefix in GLOBAL_NAME_PREFIXES:
        if name.startswith(prefix):
            return True
    for keyword in GLOBAL_TAG_KEYWORDS:
        if any(keyword in t for t in existing_tags):
            return True
    return False


def run(db_path: Path, dry_run: bool) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, name, type, tags FROM memories").fetchall()

    updates: list[tuple[str, int]] = []
    for row in rows:
        name: str = row["name"] or ""
        mem_type: str = row["type"] or ""
        tags = _load_tags(row["tags"])
        original = list(tags)
        changed = False

        project_tag = _infer_project_tag(name)
        if project_tag and project_tag not in tags:
            tags.append(project_tag)
            changed = True

        if _should_be_global(name, mem_type, original):
            if "scope:global" not in tags:
                tags.append("scope:global")
                changed = True

        if changed:
            added = [t for t in tags if t not in original]
            print(f"{'[DRY] ' if dry_run else ''}id={row['id']} {name}: +{added}")
            updates.append((_dump_tags(tags), row["id"]))

    print(f"\n{len(updates)} memories to update (of {len(rows)} total)")

    if not dry_run and updates:
        conn.executemany("UPDATE memories SET tags = ? WHERE id = ?", updates)
        conn.commit()
        print("Done.")
    elif dry_run:
        print("Dry run — no changes written.")

    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path", help="Path to memories.db (or config.db containing memories table)")
    parser.add_argument("--dry-run", action="store_true", help="Print changes without writing")
    args = parser.parse_args()

    db = Path(args.db_path)
    if not db.exists():
        print(f"Error: {db} not found", file=sys.stderr)
        sys.exit(1)

    run(db, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
