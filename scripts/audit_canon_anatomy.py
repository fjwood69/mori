"""Quantify how much existing canon would fail the completeness (anatomy) gate.

Read-only measurement. The completeness gate (mori_advisor.completeness.validate_anatomy)
was wired at the store.write chokepoint in AUDIT mode (logs, never blocks) — this script
answers the question that justifies the audit window: *how much of the corpus already on
disk entered ungated and would fail anatomy if the gate ever flips to enforce?*

Field mapping mirrors the chokepoint: candidate = {"body": body, "reason": description}.
memory_type is deliberately NOT derived (only universal rules fire) — same as the live seam.

Usage:
    # SQLite (local / UAT)
    python scripts/audit_canon_anatomy.py /path/to/memories.db
    # Postgres (prod — read-only)
    python scripts/audit_canon_anatomy.py --database-url postgresql://...
    # restrict to a tier (default: all)
    python scripts/audit_canon_anatomy.py memories.db --tier canonical
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from mori_advisor.completeness import validate_anatomy


def _verdict(body: str, description: str) -> dict:
    return validate_anatomy({"body": body or "", "reason": description or ""})


def _summarise(rows: list[tuple[str, str, str, str]], tier_filter: str | None) -> int:
    """rows = [(name, tier, body, description)]. Returns process exit code."""
    by_reason: Counter[str] = Counter()
    by_tier_fail: Counter[str] = Counter()
    by_tier_total: Counter[str] = Counter()
    failed_examples: list[tuple[str, str]] = []

    considered = 0
    for name, tier, body, description in rows:
        if tier_filter and tier != tier_filter:
            continue
        considered += 1
        by_tier_total[tier] += 1
        v = _verdict(body, description)
        by_reason[v["reason"]] += 1
        if not v["valid"]:
            by_tier_fail[tier] += 1
            if len(failed_examples) < 15:
                failed_examples.append((name, v["reason"]))

    if considered == 0:
        print("No memories matched.")
        return 0

    failed = sum(c for r, c in by_reason.items() if r != "ok")
    pct = 100.0 * failed / considered

    print(
        f"\n=== Canon anatomy audit ({considered} memories"
        f"{f', tier={tier_filter}' if tier_filter else ', all tiers'}) ==="
    )
    print(f"WOULD FAIL anatomy if the gate enforced: {failed}/{considered}  ({pct:.1f}%)\n")
    print("By reason code:")
    for reason, count in by_reason.most_common():
        print(f"  {reason:24s} {count:5d}  ({100.0 * count / considered:.1f}%)")
    print("\nBy tier (fail / total):")
    for tier in sorted(by_tier_total):
        print(f"  {tier:12s} {by_tier_fail[tier]:5d} / {by_tier_total[tier]:<5d}")
    if failed_examples:
        print("\nSample failures (name → reason):")
        for name, reason in failed_examples:
            print(f"  {name:50s} {reason}")
    return 0


def _load_sqlite(db_path: Path) -> list[tuple[str, str, str, str]]:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT name, tier, body, description FROM memories WHERE deleted_at IS NULL"
        )
        return [(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]
    finally:
        conn.close()


async def _load_postgres(url: str) -> list[tuple[str, str, str, str]]:
    import asyncpg

    conn = await asyncpg.connect(url)
    try:
        recs = await conn.fetch(
            "SELECT name, tier, body, description FROM memories WHERE deleted_at IS NULL"
        )
        return [(r["name"], r["tier"], r["body"], r["description"]) for r in recs]
    finally:
        await conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("db_path", nargs="?", help="Path to a SQLite memories.db")
    ap.add_argument("--database-url", help="Postgres URL (read-only); overrides db_path")
    ap.add_argument("--tier", help="Restrict to a single tier (e.g. canonical)")
    args = ap.parse_args()

    if args.database_url:
        rows = asyncio.run(_load_postgres(args.database_url))
    elif args.db_path:
        p = Path(args.db_path)
        if not p.exists():
            print(f"No such file: {p}", file=sys.stderr)
            return 2
        rows = _load_sqlite(p)
    else:
        ap.error("provide a SQLite db_path or --database-url")
        return 2

    return _summarise(rows, args.tier)


if __name__ == "__main__":
    raise SystemExit(main())
