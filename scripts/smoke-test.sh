#!/usr/bin/env bash
# Mori smoke test — verifies a Mori instance is healthy before tagging a release.
#
# Usage:
#   ./scripts/smoke-test.sh [--strict] [URL]
#   MORI_URL=http://localhost:8968 MORI_API_KEY=xxx ./scripts/smoke-test.sh
#   ./scripts/smoke-test.sh --strict http://<host>:8968
#
# --strict: treat 'degraded' as failure (use for GCE post-deploy confirmation)
# Exit 0 = ok (or degraded without --strict); exit 1 = failed

set -euo pipefail

STRICT=0
MORI_URL="${MORI_URL:-http://localhost:8968}"
MORI_API_KEY="${MORI_API_KEY:-}"

for arg in "$@"; do
  case "$arg" in
    --strict) STRICT=1 ;;
    http://*|https://*) MORI_URL="$arg" ;;
  esac
done

# Colour helpers
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RESET='\033[0m'
BOLD='\033[1m'

pass() { printf "${GREEN}✓${RESET} %-20s %s\n" "$1" "$2"; }
fail() { printf "${RED}✗${RESET} %-20s %s\n" "$1" "$2"; }
warn() { printf "${YELLOW}⚠${RESET} %-20s %s\n" "$1" "$2"; }

# Check for JSON parser
PARSER=""
if command -v python3 >/dev/null 2>&1; then
  PARSER="python3"
elif command -v jq >/dev/null 2>&1; then
  PARSER="jq"
else
  echo "Error: requires python3 or jq to parse JSON output" >&2
  exit 1
fi

# Auth header
AUTH_ARGS=()
if [ -n "$MORI_API_KEY" ]; then
  AUTH_ARGS+=(-H "X-Api-Key: $MORI_API_KEY")
fi

echo ""
echo -e "${BOLD}Mori smoke test → $MORI_URL${RESET}"
echo "──────────────────────────────────────────"

# Call /api/smoke
RESPONSE=$(curl -sf --max-time 30 "${AUTH_ARGS[@]}" "$MORI_URL/api/smoke" 2>&1) || {
  echo -e "${RED}✗${RESET} Could not reach $MORI_URL/api/smoke"
  echo "  Is Mori running? Is MORI_API_KEY set?"
  exit 1
}

# Parse and display results
if [ "$PARSER" = "python3" ]; then
  export RESPONSE
  export STRICT
  python3 - <<'EOF'
import os, json, sys
sys.stdout.reconfigure(encoding='utf-8')

response_text = os.environ.get("RESPONSE", "")
try:
    data = json.loads(response_text)
except Exception as e:
    print(f"\033[0;31m✗\033[0m Invalid JSON response from server: {e}")
    print("Response received:")
    print(response_text)
    sys.exit(1)

checks = data.get("checks", {})
overall = data.get("status", "unknown")

RED = "\033[0;31m"; GREEN = "\033[0;32m"; YELLOW = "\033[1;33m"; RESET = "\033[0m"

detail = {
    "db_read":         lambda c: f"{c.get('memory_count','')} memories",
    "event_log":       lambda c: f"{c.get('total_events','')} events",
    "event_roundtrip": lambda c: f"{c.get('before','')} → {c.get('after','')}",
    "dream_watermark": lambda c: f"watermark={c.get('watermark','')}, undreamed={c.get('undreamed','')}",
    "msg_daemon":      lambda c: f"{c.get('msg_count','')} messages",
}

for key in sorted(checks.keys()):
    check = checks[key]
    status = check.get("status", "missing")
    extra = detail.get(key, lambda c: "")(check)
    err = check.get("error", "")
    if status == "ok":
        print(f"{GREEN}✓{RESET} {key:<20} {extra}")
    elif status == "skipped":
        print(f"  {key:<20} (skipped)")
    else:
        print(f"{RED}✗{RESET} {key:<20} {err or 'failed'}")

print("──────────────────────────────────────────")

strict_val = os.environ.get("STRICT", "0")
if overall == "ok":
    print(f"{GREEN}Status: OK{RESET} — mori is healthy")
    sys.exit(0)
elif overall == "degraded":
    print(f"{YELLOW}Status: DEGRADED{RESET} — NATS/ingestion failed (non-critical)")
    sys.exit(int(strict_val))
else:
    print(f"{RED}Status: FAILED{RESET} — critical checks failed")
    sys.exit(1)
EOF
else
  # jq fallback
  OVERALL=$(echo "$RESPONSE" | jq -r '.status')
  echo "$RESPONSE" | jq -r '.checks | to_entries[] | "\(.key): \(.value.status)"'
  echo "──────────────────────────────────────────"
  echo "Status: $OVERALL"
  if [ "$OVERALL" = "failed" ]; then exit 1; fi
  if [ "$OVERALL" = "degraded" ] && [ "$STRICT" = "1" ]; then exit 1; fi
fi
