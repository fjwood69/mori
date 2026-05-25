#!/usr/bin/env bash
# Mori event shipper for Claude/Cursor hooks (Linux/macOS).
# Reads hook event JSON from stdin, POSTs to Mori server.
# Usage: mori-ship-event.sh --url <url> --client <name> [--api-key <key>] [--mode raw|precompact]

MORI_URL="http://localhost:8968"
CLIENT="$(hostname 2>/dev/null || echo mori)"
API_KEY=""
MODE="raw"

while [[ $# -gt 0 ]]; do
  case $1 in
    --url)     MORI_URL="$2"; shift 2 ;;
    --client)  CLIENT="$2";   shift 2 ;;
    --api-key) API_KEY="$2";  shift 2 ;;
    --mode)    MODE="$2";     shift 2 ;;
    *)         shift ;;
  esac
done

body=$(cat)
[[ -z "$body" ]] && exit 0

endpoint="events/raw"
[[ "$MODE" == "precompact" ]] && endpoint="precompact"
uri="${MORI_URL%/}/api/${endpoint}?client=${CLIENT}"

auth_args=()
[[ -n "$API_KEY" ]] && auth_args+=(-H "X-Api-Key: ${API_KEY}")

LOG="${TMPDIR:-/tmp}/mori-hook.log"
if ! printf '%s' "$body" | curl -sf -X POST "$uri" \
    -H "Content-Type: application/json" \
    "${auth_args[@]}" \
    -d @- \
    >/dev/null 2>&1; then
  # Rotate log if > 100 KB
  if [ -f "$LOG" ] && [ "$(wc -c < "$LOG" 2>/dev/null || echo 0)" -gt 102400 ]; then
    mv "$LOG" "${LOG}.old" 2>/dev/null || true
  fi
  printf '%s [mori-ship] %s %s : failed\n' \
    "$(date '+%Y-%m-%d %H:%M:%S')" "$MODE" "$uri" \
    >> "$LOG" 2>/dev/null || true
fi

exit 0
