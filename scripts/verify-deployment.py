#!/usr/bin/env python3
"""Deployment contract test — the single source of truth for "is this mori
instance serving correctly, with the canon lock ON?"

Run against ANY mori-advisor instance (UAT or production) with a key that has the ``write`` role
and nothing more. Asserts:
  - open routes (no auth) return 200;
  - every auth-guarded feature route returns 401 without a key AND 200 with the key;
  - dreamer-only routes (approve, reject, audit) return EXACTLY 403 to the write key;
  - the write key's soft delete and restore of a WORKING row apply (200);
  - v2.3.17 — the lock, on every deploy, via the two CD canaries:
      * DELETE of the live canary ``cd-canary-live`` → 202, status ``pending``, an integer
        ``pending_id`` (the one already open is allowed), operation ``delete``, and the proposer is
        THIS key. Approve and reject of that pending id with this key → exactly 403.
      * the live canary reads back canonical, protected and tagged ``cd-canary``;
      * restore of the tombstoned canary ``cd-canary-tombstone`` → 409, status ``refused``,
        reason ``canon-authority``.
    A 403 from the route counts as neither, and fails. So does a 404 (a canary is gone — approved
    by mistake, restored or deleted), a 200 (the lock is off), and a 409 on the delete (another
    identity holds an open delete proposal on the canary — the anomaly this probe exists to catch).

The store-side checks (the probe row's protected_domains, the canaries' rows) are
``verify-store.py``, run on the host inside the container by the deploy's forced command.

Shared by the UAT harness (pre-tag gate) and CD (post-deploy, from the runner over the tailnet with
CD's own key — v2.3.17; it no longer runs inside the container with a borrowed key).

Stdlib only (urllib). Add new custom_route paths to the lists below — one place, both gates.

Usage:
    python3 verify-deployment.py <base_url> <write_api_key> <key_name>
Exit 0 = contract satisfied, 1 = violation, 2 = usage.
"""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

# Routes with no auth that must return 200.
OPEN_ROUTES = ["/health", "/ready", "/metrics", "/"]

# Auth-guarded routes a WRITE key can use: (method, path, body_or_None). Each probe is well-formed
# so the key elicits 200. The contract asserts 401 without a key AND exactly 200 with it. Keep
# probes lightweight — never trigger heavy work (e.g. a real dream run) from a contract check.
GUARDED_ROUTES = [
    ("GET", "/api/git/watermark?repo=verify&ref=main", None),
    ("POST", "/api/git/ingest", {"repo": "verify", "ref": "main", "commits": []}),
    ("GET", "/api/smoke", None),
    ("GET", "/api/memories?query=verify&limit=1", None),
    ("GET", "/api/export?format=standard&limit=1", None),
    ("GET", "/api/events?limit=1", None),
    ("GET", "/api/pending", None),
]

# Dreamer-only routes: 401 without a key, EXACTLY 403 with the write key (v2.3.17, ruling P1-1 —
# "non-404" would also pass a 200, i.e. a route opened to write). Approve and reject are probed
# again below with the live canary's real pending id.
DREAMER_ROUTES = [
    ("POST", "/api/memories/verify-deployment-probe/approve"),
    ("POST", "/api/memories/verify-deployment-probe/reject"),
    ("GET", "/api/audit"),
]

# v2.3.17: the contract's own working rows. A REST write to a working row another key owns is
# queued (202), not applied — the pre-v2.3.17 probe names were written by whichever key the old
# contract borrowed, so CD's own key gets fresh names that it owns from their first write.
SOFTDEL_PROBE = "cd-contract-softdel-probe"
WRITE_PROBE = "cd-contract-write-probe"
CANARY_LIVE = "cd-canary-live"
CANARY_TOMB = "cd-canary-tombstone"
CANARY_TAG = "cd-canary"


def _call(method, url, key=None, body=None):
    """(HTTP status, parsed JSON body or None). Status 0 on a connection error."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("X-Api-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except Exception:
        return 0, None
    try:
        return status, json.loads(raw.decode() or "null")
    except ValueError:
        return status, None


def _request(method, url, key=None, body=None):
    return _call(method, url, key=key, body=body)[0]


def _tags(raw):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return [t.strip() for t in raw.split(",") if t.strip()]
    return [str(t) for t in raw] if isinstance(raw, list) else []


def _ok(cond, msg):
    print(f"  {'OK' if cond else 'XX'}  {msg}")
    return 0 if cond else 1


def check_canaries(base: str, key: str, key_name: str) -> int:
    """The lock, proved on every deploy (v2.3.17). Returns 1 on any failure."""
    fail = 0
    live = f"{base}/api/memories/{CANARY_LIVE}"
    tomb = f"{base}/api/memories/{CANARY_TOMB}/restore"
    fail |= _ok(_request("DELETE", live) == 401, "DELETE live canary without a key -> 401")
    fail |= _ok(_request("POST", tomb) == 401, "restore tombstone without a key -> 401")

    code, body = _call("DELETE", live, key=key)
    body = body if isinstance(body, dict) else {}
    pid = body.get("pending_id")
    delete_ok = (
        code == 202
        and body.get("status") == "pending"
        and isinstance(pid, int)
        and not isinstance(pid, bool)
        and body.get("operation") == "delete"
        and body.get("proposed_by") == key_name
    )
    fail |= _ok(
        delete_ok,
        f"DELETE live canary -> {code} status={body.get('status')} pending_id={pid} "
        f"operation={body.get('operation')} proposer_is_this_key={body.get('proposed_by') == key_name}"
        + ("" if delete_ok else "  (need 202 pending, int id, delete, proposer = this key)"),
    )
    if delete_ok:
        for action in ("approve", "reject"):
            c = _request(
                "POST",
                f"{base}/api/memories/{CANARY_LIVE}/{action}",
                key=key,
                body={"write_id": pid, "note": "deploy contract probe"},
            )
            fail |= _ok(
                c == 403, f"{action} the canary's proposal with the write key -> {c} (need 403)"
            )

    code, row = _call("GET", live, key=key)
    row = row if isinstance(row, dict) else {}
    intact = (
        code == 200
        and row.get("tier") == "canonical"
        and bool(row.get("protected"))
        and CANARY_TAG in _tags(row.get("tags"))
    )
    fail |= _ok(
        intact,
        f"live canary reads back -> {code} tier={row.get('tier')} protected={bool(row.get('protected'))} "
        f"tagged={CANARY_TAG in _tags(row.get('tags'))}",
    )

    code, body = _call("POST", tomb, key=key, body={})
    body = body if isinstance(body, dict) else {}
    restore_ok = (
        code == 409 and body.get("status") == "refused" and body.get("reason") == "canon-authority"
    )
    fail |= _ok(
        restore_ok,
        f"restore tombstone -> {code} status={body.get('status')} reason={body.get('reason')}"
        + ("" if restore_ok else "  (need 409 refused canon-authority)"),
    )
    return fail


def _softdel_round_trip(base: str, key: str) -> int:
    """A WORKING row: the write key's soft delete and restore apply directly (200 each)."""
    post = _request(
        "POST",
        base + "/api/memories",
        key=key,
        body={"name": SOFTDEL_PROBE, "title": "Softdel probe", "body": "probe"},
    )
    sd_noauth = _request("DELETE", f"{base}/api/memories/{SOFTDEL_PROBE}")
    sd_del = _request("DELETE", f"{base}/api/memories/{SOFTDEL_PROBE}", key=key)
    restore_noauth = _request("POST", f"{base}/api/memories/{SOFTDEL_PROBE}/restore")
    restore = _request("POST", f"{base}/api/memories/{SOFTDEL_PROBE}/restore", key=key)
    ok = (
        post in (200, 201)
        and sd_noauth == 401
        and sd_del == 200
        and restore_noauth == 401
        and restore == 200
    )
    return _ok(
        ok,
        f"soft-delete + restore round trip (post={post} del_noauth={sd_noauth} del={sd_del} "
        f"restore_noauth={restore_noauth} restore={restore}; need 200s and 401s)",
    )


def main(argv=None):
    argv = sys.argv if argv is None else argv
    if len(argv) != 4:
        print("usage: verify-deployment.py <base_url> <write_api_key> <key_name>", file=sys.stderr)
        return 2
    base, key, key_name = argv[1].rstrip("/"), argv[2], argv[3]
    fail = 0

    for path in OPEN_ROUTES:
        code = _request("GET", base + path)
        fail |= _ok(code == 200, f"GET {path} -> {code} (expected 200)")

    for method, path, body in GUARDED_ROUTES:
        short = path.split("?")[0]
        noauth = _request(method, base + path, key=None, body=body)
        auth = _request(method, base + path, key=key, body=body)
        fail |= _ok(
            noauth == 401 and auth == 200,
            f"{method} {short} (noauth={noauth} auth={auth}; expected 401 / 200)",
        )

    for method, path in DREAMER_ROUTES:
        noauth = _request(method, base + path, key=None, body=None)
        auth = _request(method, base + path, key=key, body=None)
        fail |= _ok(
            noauth == 401 and auth == 403,
            f"{method} {path} dreamer-only (noauth={noauth} auth={auth}; expected 401 / 403)",
        )

    # A working row: write then delete, both applied.
    post = _request(
        "POST",
        base + "/api/memories",
        key=key,
        body={"name": WRITE_PROBE, "title": "Verify probe", "body": "deployment check"},
    )
    del_noauth = _request("DELETE", f"{base}/api/memories/{WRITE_PROBE}")
    del_code = _request("DELETE", f"{base}/api/memories/{WRITE_PROBE}", key=key)
    fail |= _ok(
        post in (200, 201) and del_noauth == 401 and del_code == 200,
        f"POST /api/memories + DELETE (post={post} del_noauth={del_noauth} del={del_code}; "
        "need 200/201, 401, 200)",
    )

    # GET /api/memories/{name}: the live canary is a known name, so the probe never skips.
    safe = urllib.parse.quote(CANARY_LIVE, safe="")
    noauth = _request("GET", f"{base}/api/memories/{safe}")
    fail |= _ok(noauth == 401, f"GET /api/memories/{{name}} without a key -> {noauth} (need 401)")

    fail |= _softdel_round_trip(base, key)
    fail |= check_canaries(base, key, key_name)

    print("PASS" if fail == 0 else "FAIL")
    return fail


if __name__ == "__main__":
    sys.exit(main())
