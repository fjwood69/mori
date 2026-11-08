#!/usr/bin/env python3
"""Deployment store check — v2.3.17 (ruling Q2). Run INSIDE the deployed container, on the host,
straight after the restart and health gate (``mori-cd-deploy-as-mori``):

    podman exec mori-advisor python3 /app/scripts/verify-store.py

It uses the store the server uses (``MORI_DATABASE_URL`` in the container) — never the deploy
secret — and FAILS CLOSED: no Postgres URL is a failure (``--allow-sqlite`` exists for UAT's SQLite
port only and is never passed by CD). It checks:

1. **The probe row** (the v2.3.10 protected_domains guard). It writes the working probe row through
   the app's own store code (the UPDATE path after the first deploy — the 2026-10-02 incident
   re-encoded JSONB there), then asserts this run's write landed and ``protected_domains`` is
   exactly the empty JSON array.
2. **The live CD canary** ``cd-canary-live``: active, canonical, protected, tagged ``cd-canary``.
3. **The tombstoned canary** ``cd-canary-tombstone``: deleted (``deleted_at`` set, no active row of
   that name), canonical, protected, tagged ``cd-canary``.

The HTTP half (the lock itself, with CD's write key) is ``verify-deployment.py``, run from the CD
runner. Exit 0 = all checks pass; 1 = a failure; 2 = usage.
"""

from __future__ import annotations

import asyncio
import json
import sys

# Its own row (v2.3.17): the store check writes as an internal identity, so sharing a row with the
# HTTP contract would make the contract's REST write a queued proposal (another owner).
PROBE = "verify-store-probe"
LIVE = "cd-canary-live"
TOMB = "cd-canary-tombstone"
TAG = "cd-canary"


def _tags(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return [t.strip() for t in raw.split(",") if t.strip()]
    return [str(t) for t in raw] if isinstance(raw, list) else []


def _canary_ok(label: str, row, *, want_deleted: bool) -> bool:
    if row is None:
        print(f"  XX  {label}: missing")
        return False
    tier, protected, tags, deleted = row
    ok = (
        tier == "canonical"
        and bool(protected)
        and TAG in _tags(tags)
        and (deleted is not None) == want_deleted
    )
    print(
        f"  {'OK' if ok else 'XX'}  {label} (tier={tier} protected={bool(protected)} "
        f"tagged={TAG in _tags(tags)} deleted={deleted is not None})"
    )
    return ok


async def _probe_write(store) -> None:
    from mori_advisor.provenance import Provenance

    prov = Provenance(actor="system", actor_detail="verify-store", source="scripts/verify-store.py")
    r = store._write(
        name=PROBE,
        title="Softdel probe",
        body="probe",
        tier="working",
        provenance=prov,
    )
    if asyncio.iscoroutine(r):
        r = await r
    if not getattr(r, "accepted", False):
        raise RuntimeError(f"probe write not accepted: {getattr(r, 'reason', r)}")


def _store_dsn() -> str:
    import os

    dsn = os.environ.get("MORI_DATABASE_URL", "")
    return dsn if dsn.startswith(("postgres://", "postgresql://")) else ""


def _store_query(sql: str, *args):
    """One row from the store (Postgres, inside the container). Raises on any failure."""
    import asyncpg

    async def _q():
        conn = await asyncpg.connect(_store_dsn(), ssl=False)
        try:
            return await conn.fetchrow(sql, *args)
        finally:
            await conn.close()

    return asyncio.run(_q())


def _probe_row_canary(name: str, written_after) -> bool:
    """True iff THIS run's write landed on the probe row and left protected_domains exactly '[]'
    (the v2.3.10 guard, moved here from verify-deployment.py in v2.3.17).

    ``written_after`` is the store's clock read just before the probe write: the row's
    ``updated_at`` must be at or after it, so a write that silently did not happen cannot pass on
    the row's previous state. FAILS CLOSED: no Postgres URL, no clock read, or a query error is a
    failure, never a skip.
    """
    if not _store_dsn():
        print("  XX  probe-row canary: no Postgres MORI_DATABASE_URL — fails closed")
        return False
    if written_after is None:
        print("  XX  probe-row canary: could not read the store clock before the write")
        return False
    try:
        row = _store_query(
            "SELECT jsonb_typeof(protected_domains) AS t, "
            "protected_domains = '[]'::jsonb AS empty, "
            "pg_column_size(protected_domains) AS size, "
            "updated_at >= $2 AS written_now "
            "FROM memories WHERE name = $1 AND deleted_at IS NULL",
            name,
            written_after,
        )
    except Exception as e:  # the canary must never pass silently
        print(f"  XX  probe-row canary could not query the store: {type(e).__name__}: {e}")
        return False
    if row is None:
        print(f"  XX  probe-row canary: no active row '{name}'")
        return False
    ok = bool(row["written_now"]) and row["t"] == "array" and bool(row["empty"])
    print(
        f"  {'OK' if ok else 'XX'}  probe-row canary (written this run={row['written_now']} "
        f"protected_domains type={row['t']} empty={row['empty']} stored_bytes={row['size']})"
    )
    return ok


async def _check_pg(store) -> int:
    fail = 0
    async with store.pool.acquire() as conn:
        written_after = await conn.fetchval("SELECT now()")
    try:
        await _probe_write(store)
    except Exception as e:
        # Condition 2 (v2.3.10): a failed probe WRITE fails outright; the canary never runs on the
        # row's untouched state.
        print(f"  XX  probe write failed: {e}")
        return 1
    if not await asyncio.to_thread(_probe_row_canary, PROBE, written_after):
        fail = 1
    async with store.pool.acquire() as conn:
        live = await conn.fetchrow(
            "SELECT tier, protected, tags, deleted_at FROM memories "
            "WHERE name = $1 AND deleted_at IS NULL",
            LIVE,
        )
        tomb_active = await conn.fetchval(
            "SELECT 1 FROM memories WHERE name = $1 AND deleted_at IS NULL", TOMB
        )
        tomb = await conn.fetchrow(
            "SELECT tier, protected, tags, deleted_at FROM memories "
            "WHERE name = $1 AND deleted_at IS NOT NULL ORDER BY deleted_at DESC LIMIT 1",
            TOMB,
        )
    fail |= 0 if _canary_ok(LIVE, tuple(live) if live else None, want_deleted=False) else 1
    if tomb_active:
        print(f"  XX  {TOMB}: an ACTIVE row holds the name (restored?)")
        fail = 1
    fail |= 0 if _canary_ok(TOMB, tuple(tomb) if tomb else None, want_deleted=True) else 1
    return fail


async def _check_sqlite(store) -> int:
    fail = 0
    try:
        await _probe_write(store)
    except Exception as e:
        print(f"  XX  probe write failed: {e}")
        return 1
    conn = store._mem._get_conn()
    try:
        probe = conn.execute(
            "SELECT protected_domains FROM memories WHERE name = ? AND deleted_at IS NULL", (PROBE,)
        ).fetchone()
        live = conn.execute(
            "SELECT tier, protected, tags, deleted_at FROM memories "
            "WHERE name = ? AND deleted_at IS NULL",
            (LIVE,),
        ).fetchone()
        tomb_active = conn.execute(
            "SELECT 1 FROM memories WHERE name = ? AND deleted_at IS NULL", (TOMB,)
        ).fetchone()
        tomb = conn.execute(
            "SELECT tier, protected, tags, deleted_at FROM memories "
            "WHERE name = ? AND deleted_at IS NOT NULL ORDER BY deleted_at DESC LIMIT 1",
            (TOMB,),
        ).fetchone()
    finally:
        conn.close()
    print(
        f"  {'OK' if probe else 'XX'}  probe row written (SQLite; the JSONB guard is Postgres-only)"
    )
    fail |= 0 if probe else 1
    fail |= 0 if _canary_ok(LIVE, live, want_deleted=False) else 1
    if tomb_active:
        print(f"  XX  {TOMB}: an ACTIVE row holds the name (restored?)")
        fail = 1
    fail |= 0 if _canary_ok(TOMB, tomb, want_deleted=True) else 1
    return fail


async def _main(allow_sqlite: bool) -> int:
    from mori_advisor.store import get_store

    store = get_store()
    if hasattr(store, "connect") and type(store).__name__ == "PostgresStore":
        await store.connect()
        try:
            return await _check_pg(store)
        finally:
            await store.pool.close()
    if not allow_sqlite:
        print(
            "  XX  no Postgres MORI_DATABASE_URL in this container — the store check fails closed"
        )
        return 1
    return await _check_sqlite(store)


def main(argv: list[str]) -> int:
    args = argv[1:]
    if args not in ([], ["--allow-sqlite"]):
        print("usage: verify-store.py [--allow-sqlite]", file=sys.stderr)
        return 2
    try:
        fail = asyncio.run(_main("--allow-sqlite" in args))
    except Exception as e:  # the store check must never pass silently
        print(f"  XX  store check failed: {type(e).__name__}: {e}")
        fail = 1
    print("STORE PASS" if fail == 0 else "STORE FAIL")
    return fail


if __name__ == "__main__":
    sys.exit(main(sys.argv))
