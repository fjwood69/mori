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
    ("GET", "/api/events?limit=1", None),
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

    print("PASS" if fail == 0 else "FAIL")
    return fail


if __name__ == "__main__":
    sys.exit(main())
