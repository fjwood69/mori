#!/usr/bin/env bash
# Create (or check) the two CD deploy canaries (v2.3.17, design §2.2, ruling Q3) — run by Fred,
# with his review key, BEFORE the v2.3.17 tag:
#
#   deploy/gcp/cd/mori-cd-canaries.sh <base_url> setup    # create, protect (MCP), tombstone
#   deploy/gcp/cd/mori-cd-canaries.sh <base_url> verify   # check them as the contract needs
#
# Works on v2.3.16 and v2.3.17, so the first v2.3.17 deploy's contract finds the canaries already
# protected. The review key is read with `read -s` from this terminal and handed to the core
# (mori-cd-canaries.py) on stdin; it is never printed, stored or put on a command line. Refuses under
# Claude Code: an agent must never hold the review key (R9).
set -euo pipefail
if [[ -n "${CLAUDECODE:-}" || ! -t 0 ]]; then
  echo "Refusing: run this in your own interactive terminal, not through Claude Code." >&2; exit 1
fi
BASE="${1:?usage: mori-cd-canaries.sh <base_url> setup|verify}"
MODE="${2:?usage: mori-cd-canaries.sh <base_url> setup|verify}"
read -r -s -p "Review key: " KEY; echo
[[ -n "$KEY" ]] || { echo "no key" >&2; exit 1; }
printf '%s\n' "$KEY" | python3 "$(dirname "$0")/mori-cd-canaries.py" "$BASE" "$MODE"
unset KEY
