#!/usr/bin/env python3
"""Create and check the two CD deploy canaries (v2.3.17, design §2.2, ruling Q3) — the core of
``mori-cd-canaries.sh``. Reads the REVIEW (dreamer) key from stdin; prints no secret.

    mori-cd-canaries.py <base_url> setup    # create, protect, tombstone (on v2.3.16 or v2.3.17)
    mori-cd-canaries.py <base_url> verify   # both canaries as the contract needs them

``setup`` works BEFORE the v2.3.17 tag, so the first v2.3.17 deploy's contract finds the canaries
already protected: the rows are created over REST; protection is set through the MCP tool
``memory_protect`` (present on v2.3.16 — the REST protect route arrives with v2.3.17); the restore
target is then soft-deleted. Idempotent: an existing canary is protected again, not duplicated.
Standard library only.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

LIVE, TOMB, TAG = "cd-canary-live", "cd-canary-tombstone", "cd-canary"


def _http(method: str, url: str, key: str, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Api-Key", key)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def _json(text: str):
    try:
        return json.loads(text)
    except ValueError:  # an SSE frame: take the last data: line
        lines = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
        return json.loads(lines[-1]) if lines else None


def _mcp_protect(base: str, key: str, name: str) -> str:
    h = {"Accept": "application/json, text/event-stream"}
    code, text, hdrs = _http(
        "POST",
        base + "/mcp",
        key,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "mori-cd-canaries", "version": "1"},
            },
        },
        h,
    )
    sid = next((v for k, v in hdrs.items() if k.lower() == "mcp-session-id"), None)
    if code != 200 or not sid:
        raise SystemExit(f"MCP initialize failed ({code})")
    h["mcp-session-id"] = sid
    _http("POST", base + "/mcp", key, {"jsonrpc": "2.0", "method": "notifications/initialized"}, h)
    code, text, _ = _http(
        "POST",
        base + "/mcp",
        key,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "memory_protect", "arguments": {"name": name, "protected": True}},
        },
        h,
    )
    msg = _json(text) or {}
    content = ((msg.get("result") or {}).get("content") or [{}])[0].get("text", "")
    if code != 200 or "required" in content.lower() or "not found" in content.lower():
        raise SystemExit(f"memory_protect {name} failed ({code}): {content[:200]}")
    return content


def _canary_body(name: str, role: str) -> dict:
    return {
        "name": name,
        "tier": "canonical",
        "type": "decision",
        "tags": [TAG],
        "title": f"CD DEPLOY CANARY ({role}) — approving a proposal on this row DELETES or restores it "
        "and FAILS the next deploy",
        "description": "The deploy contract's probe (v2.3.17). Never approve a proposal on it.",
        "body": "This row exists so every deploy can prove the canon lock is on. Leave it alone.",
    }


def _step(ok: bool, label: str) -> None:
    print(f"  {'OK' if ok else 'XX'}  {label}")
    if not ok:
        raise SystemExit(1)


def setup(base: str, key: str) -> None:
    # The tombstone first: if it is currently deleted, restore it so it can be protected.
    code, _, _ = _http("POST", f"{base}/api/memories/{TOMB}/restore", key, {})
    for name, role in ((LIVE, "live"), (TOMB, "tombstone")):
        code, text, _ = _http("GET", f"{base}/api/memories/{name}", key)
        if code == 404:
            code, text, _ = _http("POST", f"{base}/api/memories", key, _canary_body(name, role))
            _step(code == 201, f"create {name} ({code})")
        else:
            row = _json(text) or {}
            _step(code == 200 and row.get("tier") == "canonical", f"{name} exists, canonical")
        _mcp_protect(base, key, name)
        print(f"  OK  protect {name} (MCP memory_protect)")
    code, _, _ = _http("DELETE", f"{base}/api/memories/{TOMB}", key)
    _step(code == 200, f"tombstone {TOMB} ({code})")
    verify(base, key)


def verify(base: str, key: str) -> None:
    code, text, _ = _http("GET", f"{base}/api/memories/{LIVE}", key)
    row = _json(text) or {}
    tags = (
        row.get("tags")
        if isinstance(row.get("tags"), list)
        else json.loads(row.get("tags") or "[]")
    )
    _step(
        code == 200 and row.get("tier") == "canonical" and TAG in tags,
        f"{LIVE}: active, canonical, tagged ({code})",
    )
    if "protected" in row:  # v2.3.17 returns it; v2.3.16 doesn't
        _step(bool(row["protected"]), f"{LIVE}: protected")
    code, _, _ = _http("GET", f"{base}/api/memories/{TOMB}", key)
    _step(code == 404, f"{TOMB}: not active ({code})")


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[2] not in ("setup", "verify"):
        print(
            "usage: mori-cd-canaries.py <base_url> setup|verify  (review key on stdin)",
            file=sys.stderr,
        )
        return 2
    key = sys.stdin.readline().strip()
    if not key:
        print("no key on stdin", file=sys.stderr)
        return 2
    base = argv[1].rstrip("/")
    (setup if argv[2] == "setup" else verify)(base, key)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
