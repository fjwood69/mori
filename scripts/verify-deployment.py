#!/usr/bin/env python3
"""Deployment contract test — the single source of truth for "is this mori
instance serving correctly?"

Run against ANY mori-advisor instance (UAT or production). Asserts:
  - open routes (no auth) return 200
  - every auth-guarded feature route returns 401 without a key (auth enforced)
    AND a non-404 status with a valid key (route is actually registered)

This is deliberately shared by BOTH the UAT harness (pre-tag gate) and the CD
pipeline (post-deploy gate, run via `podman exec` inside the deployed container)
so the two assert IDENTICAL behavior. A deploy that passes /health but 404s on
feature routes — the failure mode that shipped broken for days — fails here.

Stdlib only (urllib) so it runs inside the slim container image with no extra
deps. Add new custom_route paths to ROUTES below — one place, both gates.

Usage:
    python3 verify-deployment.py <base_url> <api_key>
Exit 0 = contract satisfied, 1 = violation.
"""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

# Routes with no auth that must return 200.
OPEN_ROUTES = ["/health", "/ready", "/metrics", "/"]

# Auth-guarded routes: (method, path, body_or_None). Each probe is well-formed so
# a VALID key elicits 200. The contract asserts: 401 without a key (auth enforced)
# AND exactly 200 with a valid key (route registered + key accepted). Keep probes
# lightweight — never trigger heavy work (e.g. a real dream run) from a contract
# check. New custom routes go here; if a route can't return 200 for a safe probe,
# add it to a separate registration-only list rather than weakening this rule.
GUARDED_ROUTES = [
    ("GET", "/api/git/watermark?repo=verify&ref=main", None),
    ("POST", "/api/git/ingest", {"repo": "verify", "ref": "main", "commits": []}),
    ("GET", "/api/smoke", None),
    ("GET", "/api/memories?query=verify&limit=1", None),
    ("GET", "/api/export?format=standard&limit=1", None),
    ("GET", "/api/events?limit=1", None),
    # Write API (#14) — auth-gating probes only (mutating routes use safe strategies below)
    ("GET", "/api/pending", None),
]

# Write-API routes that require safe probes rather than a static body.
# These are checked for auth-gating (401 without key) and route registration
# (non-404 with key). We do NOT assert 200 because the operation may legitimately
# return 400/404/409 depending on store state.
WRITE_API_AUTH_ROUTES = [
    # POST /api/memories: safe probe uses a throwaway name + minimal body.
    # Probed separately below (see _probe_write_api).
    ("POST", "/api/memories/{name}/approve"),
    ("POST", "/api/memories/{name}/reject"),
    # Audit endpoint (#23 A) — returns 200 with empty list when no entries yet.
    ("GET", "/api/audit"),
    # NOTE: POST /api/memories/{name}/restore is NOT a static auth-gating entry —
    # restoring a non-existent name correctly returns 404 (route registered, key
    # accepted, target absent), indistinguishable from an unregistered route under
    # the non-404 rule. It is proven in the soft-delete+restore round-trip below.
    # DELETE /api/memories/{name} is NOT a static auth-gating entry: a keyed DELETE
    # of the non-existent sentinel correctly returns 404 (route registered, key
    # accepted, target absent), which is indistinguishable from an unregistered
    # route under the non-404 rule. DELETE is instead proven by the POST+DELETE
    # round-trip below (no-key 401 → keyed 200/404), which is unambiguous.
]


def _request(method, url, key=None, body=None):
    """Return HTTP status code (or 0 on connection error)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("X-Api-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


SOFTDEL_PROBE = "verify-deployment-softdel-probe"


def _store_dsn() -> str:
    import os

    dsn = os.environ.get("MORI_DATABASE_URL", "")
    return dsn if dsn.startswith(("postgres://", "postgresql://")) else ""


def _store_query(sql: str, *args):
    """One row from the store (Postgres, inside the container). Raises on any failure."""
    import asyncio

    import asyncpg

    async def _q():
        conn = await asyncpg.connect(_store_dsn(), ssl=False)
        try:
            return await conn.fetchrow(sql, *args)
        finally:
            await conn.close()

    return asyncio.run(_q())


def _probe_row_canary(name: str, written_after) -> bool:
    """True iff THIS run's write landed on the probe row and left protected_domains exactly '[]'.

    ``written_after`` is the store's clock read just before the contract's own write: the row's
    ``updated_at`` must be at or after it, so a write that silently did not happen cannot pass on
    the row's previous (post-migration) state. Postgres only; SQLite never had the bug.
    """
    if not _store_dsn():
        print("  --  probe-row canary SKIP (not a Postgres backend)")
        return True
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


def _softdel_probe_and_canary(base: str, key: str) -> int:
    """Write the probe row (an UPDATE on every deploy after the first), soft-delete and restore
    it, then run the canary. Returns 1 on failure.

    The probe WRITE must succeed (v2.3.10): the update path is exactly what this contract tests —
    the 2026-10-02 incident was an UPDATE re-encoding JSONB — so a non-2xx write FAILS rather than
    skipping, which would leave the canary to pass on the row's untouched state.
    """
    written_after = None
    if _store_dsn():
        try:
            written_after = _store_query("SELECT now() AS t")["t"]
        except Exception as e:
            print(f"  XX  probe-row canary could not read the store clock: {type(e).__name__}: {e}")
            return 1

    post = _request(
        "POST",
        base + "/api/memories",
        key=key,
        body={"name": SOFTDEL_PROBE, "title": "Softdel probe", "body": "probe"},
    )
    if post not in (200, 201):
        print(f"  XX  soft-delete + restore probe: the probe WRITE returned {post} (need 200/201)")
        return 1
    sd_noauth = _request("DELETE", f"{base}/api/memories/{SOFTDEL_PROBE}")
    sd_del = _request("DELETE", f"{base}/api/memories/{SOFTDEL_PROBE}", key=key)
    restore_noauth = _request("POST", f"{base}/api/memories/{SOFTDEL_PROBE}/restore")
    restore = _request("POST", f"{base}/api/memories/{SOFTDEL_PROBE}/restore", key=key)
    detail = (
        f"(post={post} del_noauth={sd_noauth} del={sd_del} "
        f"restore_noauth={restore_noauth} restore={restore})"
    )
    fail = 0
    if (
        sd_noauth == 401
        and sd_del in (200, 404)
        and restore_noauth == 401
        and restore in (200, 404)
    ):
        print(f"  OK  soft-delete + restore probe {detail}")
    else:
        print(f"  XX  soft-delete + restore probe {detail}")
        fail = 1
    if not _probe_row_canary(SOFTDEL_PROBE, written_after):
        fail = 1
    return fail


def main():
    if len(sys.argv) != 3:
        print("usage: verify-deployment.py <base_url> <api_key>", file=sys.stderr)
        return 2
    base = sys.argv[1].rstrip("/")
    key = sys.argv[2]
    fail = 0

    for path in OPEN_ROUTES:
        code = _request("GET", base + path)
        if code == 200:
            print(f"  OK  GET {path} -> 200")
        else:
            print(f"  XX  GET {path} -> {code} (expected 200)")
            fail = 1

    for method, path, body in GUARDED_ROUTES:
        short = path.split("?")[0]
        noauth = _request(method, base + path, key=None, body=body)
        auth = _request(method, base + path, key=key, body=body)
        if noauth == 401 and auth == 200:
            print(f"  OK  {method} {short} (noauth={noauth} auth={auth})")
        else:
            print(
                f"  XX  {method} {short} (noauth={noauth} auth={auth}) "
                f"-- expected noauth=401, auth=200"
            )
            fail = 1

    # Write API auth-gating probes — assert 401 without key (auth enforced), non-404 with key.
    # Use a sentinel name so the path is valid but the write won't pollute the store.
    _verify_name = "verify-deployment-probe"
    for method, tmpl in WRITE_API_AUTH_ROUTES:
        path = tmpl.replace("{name}", _verify_name)
        short = path.split("?")[0]
        noauth = _request(method, base + path, key=None, body=None)
        auth_code = _request(method, base + path, key=key, body=None)
        if noauth == 401 and auth_code != 404:
            print(f"  OK  {method} {short} (noauth={noauth} auth={auth_code})")
        else:
            print(
                f"  XX  {method} {short} (noauth={noauth} auth={auth_code}) "
                f"-- expected noauth=401, auth≠404"
            )
            fail = 1

    # POST /api/memories safe write-then-delete probe:
    # Propose a throwaway memory, then delete it. Both must succeed (or 202 for pending).
    _probe_name = "verify-deployment-write-probe"
    post_code = _request(
        "POST",
        base + "/api/memories",
        key=key,
        body={"name": _probe_name, "title": "Verify probe", "body": "deployment check"},
    )
    if post_code in (200, 201, 202):
        # Auth-gating: a no-key DELETE must be rejected (401) before the real delete.
        del_noauth = _request("DELETE", f"{base}/api/memories/{_probe_name}")
        del_code = _request("DELETE", f"{base}/api/memories/{_probe_name}", key=key)
        if del_noauth == 401 and del_code in (200, 404):
            print(
                f"  OK  POST /api/memories + DELETE probe "
                f"(post={post_code} del_noauth={del_noauth} del={del_code})"
            )
        elif del_noauth != 401:
            print(
                f"  XX  DELETE /api/memories/{_probe_name} no-key returned "
                f"{del_noauth} (expected 401)"
            )
            fail = 1
        else:
            print(
                f"  XX  DELETE /api/memories/{_probe_name} returned {del_code} (expected 200/404)"
            )
            fail = 1
    else:
        print(f"  XX  POST /api/memories probe returned {post_code} (expected 200/201/202)")
        fail = 1

    # Dynamic detail probe: GET /api/memories/{name} — can't use a static path because
    # ApiKeyMiddleware 401s any /api/* route it doesn't recognise, so a static empty-store
    # probe would false-fail. Fetch the first memory from the list; skip if store is empty.
    list_url = base + "/api/memories?limit=1"
    list_req = urllib.request.Request(list_url, method="GET")
    list_req.add_header("X-Api-Key", key)
    mem_name = None
    try:
        with urllib.request.urlopen(list_req, timeout=15) as resp:
            payload = json.loads(resp.read().decode())
            memories = payload.get("memories") or []
            if memories:
                mem_name = memories[0].get("name")
    except Exception:
        pass  # connection errors already caught by GUARDED_ROUTES above

    if mem_name:
        safe = urllib.parse.quote(mem_name, safe="")
        noauth = _request("GET", f"{base}/api/memories/{safe}")
        auth = _request("GET", f"{base}/api/memories/{safe}", key=key)
        if noauth == 401 and auth == 200:
            print(f"  OK  GET /api/memories/{{name}} (noauth={noauth} auth={auth})")
        else:
            print(
                f"  XX  GET /api/memories/{{name}} (noauth={noauth} auth={auth})"
                f" -- expected noauth=401, auth=200"
            )
            fail = 1
    else:
        print("  --  GET /api/memories/{name} SKIP (store empty, cannot probe)")

    # Soft-delete + restore round-trip (#23 B) + the v2.3.10 probe-row canary.
    if _softdel_probe_and_canary(base, key):
        fail = 1

    print("PASS" if fail == 0 else "FAIL")
    return fail


if __name__ == "__main__":
    sys.exit(main())
