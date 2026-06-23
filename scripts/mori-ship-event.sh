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

# Auto-resolve key from secrets store when not passed explicitly.
# Derives MORI_API_KEY_<CLIENT_UPPER> (e.g. MORI_API_KEY_LAPTOP) so
# each device uses its own named key without putting the secret in settings.json.
if [[ -z "$API_KEY" ]] && [[ -x "$HOME/bin/get-secret.sh" ]]; then
  _key_name="MORI_API_KEY_$(echo "$CLIENT" | tr '[:lower:]-' '[:upper:]_')"
  API_KEY="$("$HOME/bin/get-secret.sh" "$_key_name" 2>/dev/null)" || true
fi

body=$(cat)
[[ -z "$body" ]] && exit 0

# ---- Stop-event enrichment ---------------------------------------------------
# On Stop, attach a bounded tail of the session transcript so the server can
# extract the turn's assistant reasoning (the highest-value memory signal, which
# hook payloads otherwise omit). The server does the JSONL parsing; the client
# just ships a bounded, base64-encoded tail.
#
# Pure bash + tail + base64 — no python/jq — so it works on bare macOS and Linux.
# base64 is JSON-safe, so the tail splices in without any escaping. Any failure
# falls through to shipping the original body unchanged.
if [[ "$MODE" == "raw" ]] && printf '%s' "$body" | grep -q '"hook_event_name"[[:space:]]*:[[:space:]]*"Stop"'; then
  tpath=$(printf '%s' "$body" | sed -n 's/.*"transcript_path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  if [[ -n "$tpath" && -r "$tpath" ]]; then
    tail_b64=$(tail -c 65536 "$tpath" 2>/dev/null | base64 2>/dev/null | tr -d '\n')
    if [[ -n "$tail_b64" && "$body" == *"}" ]]; then
      body="${body%\}},\"transcript_tail_b64\":\"${tail_b64}\"}"
    fi
  fi
fi

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
