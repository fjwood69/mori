#!/usr/bin/env bash
# Mori post-push hook — publishes GitPush event and ingests commit messages
# Install: cp scripts/post-push.sh .git/hooks/post-push && chmod +x .git/hooks/post-push
# Or use:  ./scripts/install-git-hooks.sh

MORI_URL="${MORI_URL:-http://localhost:8968}"
CLIENT="${MORI_CLIENT:-$(hostname 2>/dev/null || echo unknown)}"

# Resolve MORI_API_KEY: env var takes precedence; fall back to ~/.claude/.secrets
if [ -z "${MORI_API_KEY:-}" ]; then
  _SECRETS="${HOME}/.claude/.secrets"
  if [ -f "$_SECRETS" ]; then
    # Derive key name from hostname (e.g. uk-smr-nuc15pro → MORI_API_KEY_NUC15PRO)
    _HOST_UPPER=$(hostname 2>/dev/null | tr '[:lower:]-' '[:upper:]_' | sed 's/^[A-Z_]*_//')
    _KEY_NAME="MORI_API_KEY_${_HOST_UPPER}"
    MORI_API_KEY=$(grep "^${_KEY_NAME}=" "$_SECRETS" | cut -d= -f2- 2>/dev/null || echo "")
    # Fallback: try any MORI_API_KEY_ line if hostname-derived key not found
    if [ -z "$MORI_API_KEY" ]; then
      MORI_API_KEY=$(grep '^MORI_API_KEY_' "$_SECRETS" | head -1 | cut -d= -f2- 2>/dev/null || echo "")
    fi
  fi
fi

REPO=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null || echo "unknown")
BRANCH=$(git branch --show-current 2>/dev/null || echo "unknown")
SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
MESSAGE=$(git log -1 --pretty=%s 2>/dev/null || echo "")
REMOTE="${1:-origin}"

# Escape message for JSON (replace " and \)
MESSAGE_ESC=$(printf '%s' "$MESSAGE" | sed 's/\\/\\\\/g; s/"/\\"/g')

PAYLOAD=$(printf '{"hook_event_name":"GitPush","session_id":"%s","repo":"%s","branch":"%s","sha":"%s","message":"%s","remote":"%s","client":"%s"}' \
  "$SHA" "$REPO" "$BRANCH" "$SHA" "$MESSAGE_ESC" "$REMOTE" "$CLIENT")

AUTH_ARGS=()
if [ -n "$MORI_API_KEY" ]; then
  AUTH_ARGS+=(-H "X-Api-Key: ${MORI_API_KEY}")
fi

curl -sf -X POST "${MORI_URL}/api/events/raw?client=${CLIENT}" \
  -H "Content-Type: application/json" \
  "${AUTH_ARGS[@]}" \
  -d "$PAYLOAD" \
  >/dev/null 2>&1 || true

# ── Git commit ingestion ──────────────────────────────────────────────────────
# Sends commits since the last ingested SHA (per repo + branch) to Mori.
# Server-side dedup via ingestion_log makes this idempotent.
# Falls back to HEAD~20..HEAD on first push.

if [ -n "$MORI_API_KEY" ] && command -v python3 >/dev/null 2>&1; then

  # Fetch per-ref watermark
  WATERMARK=$(curl -sf \
    -H "X-Api-Key: ${MORI_API_KEY}" \
    "${MORI_URL}/api/ingest/git/watermark?repo=${REPO}&ref=${BRANCH}" \
    2>/dev/null \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('watermark') or '')" \
    2>/dev/null || echo "")

  if [ -n "$WATERMARK" ]; then
    RANGE="${WATERMARK}..HEAD"
  else
    RANGE="HEAD~20..HEAD"
  fi

  # Collect commits: \x1f separates fields, \x1e separates records.
  # maxsplit=3 in Python ensures body (which may contain \x1f) lands in parts[3].
  COMMITS_JSON=$(python3 - "$RANGE" <<'PYEOF'
import subprocess, json, sys

range_arg = sys.argv[1]
# Format: SHA\x1fshort\x1fsubject\x1fbody\x1e (record separator at end)
result = subprocess.run(
    ['git', 'log', '--reverse', range_arg,
     '--format=%H\x1f%h\x1f%s\x1f%b\x1e'],
    capture_output=True, text=True
)

commits = []
for entry in result.stdout.split('\x1e'):
    entry = entry.strip()
    if not entry:
        continue
    parts = entry.split('\x1f', 3)
    if len(parts) < 3:
        continue
    commits.append({
        'sha':       parts[0].strip(),
        'short_sha': parts[1].strip(),
        'subject':   parts[2].strip(),
        'body':      parts[3].strip() if len(parts) > 3 else '',
    })
print(json.dumps(commits))
PYEOF
)

  COMMIT_COUNT=$(python3 -c "import sys,json; print(len(json.loads(sys.argv[1])))" "$COMMITS_JSON" 2>/dev/null || echo "0")

  if [ "$COMMIT_COUNT" -gt 0 ]; then
    # Add author + timestamp via a second git log pass (cleaner than embedding in format above)
    COMMITS_JSON=$(python3 - "$COMMITS_JSON" "$RANGE" <<'PYEOF'
import subprocess, json, sys

commits = json.loads(sys.argv[1])
range_arg = sys.argv[2]

# Build SHA → (author, timestamp) map
meta_result = subprocess.run(
    ['git', 'log', '--reverse', range_arg, '--format=%H\x1f%an\x1f%aI'],
    capture_output=True, text=True
)
meta = {}
for line in meta_result.stdout.splitlines():
    parts = line.split('\x1f', 2)
    if len(parts) == 3:
        meta[parts[0].strip()] = {'author': parts[1].strip(), 'timestamp': parts[2].strip()}

for c in commits:
    m = meta.get(c['sha'], {})
    c['author'] = m.get('author', '')
    c['timestamp'] = m.get('timestamp', '')

print(json.dumps(commits))
PYEOF
)

    INGEST_PAYLOAD=$(python3 -c "
import json, sys
commits = json.loads(sys.argv[1])
print(json.dumps({'repo': sys.argv[2], 'ref': sys.argv[3], 'commits': commits, 'pusher': sys.argv[4]}))
" "$COMMITS_JSON" "$REPO" "$BRANCH" "$CLIENT" 2>/dev/null)

    RESULT=$(curl -sf -X POST \
      -H "X-Api-Key: ${MORI_API_KEY}" \
      -H "Content-Type: application/json" \
      -d "$INGEST_PAYLOAD" \
      "${MORI_URL}/api/ingest/git" \
      2>/dev/null || echo "")

    INGESTED=$(python3 -c "import sys,json; d=json.loads(sys.argv[1]); print(d.get('ingested',0))" "$RESULT" 2>/dev/null || echo "0")
    if [ "$INGESTED" -gt 0 ]; then
      echo "[mori] ingested ${INGESTED} commit(s) from ${REPO}/${BRANCH}" >&2
    fi
  fi
fi

exit 0
