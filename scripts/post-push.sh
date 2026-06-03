#!/usr/bin/env bash
# Mori post-push hook — publishes GitPush event and ingests commit messages
# Install: cp scripts/post-push.sh .git/hooks/post-push && chmod +x .git/hooks/post-push
# Or use:  ./scripts/install-git-hooks.sh

MORI_URL="${MORI_URL:-http://localhost:8968}"
MORI_API_KEY="${MORI_API_KEY:-}"
CLIENT="${MORI_CLIENT:-$(hostname 2>/dev/null || echo unknown)}"

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
# Sends new commits since the last ingested SHA to Mori for memory distillation.
# Server-side dedup (ingestion_log.source_hash) makes this idempotent.

if [ -n "$MORI_API_KEY" ] && command -v python3 >/dev/null 2>&1; then
  # Fetch the git watermark (last ingested commit SHA for this repo)
  WATERMARK=$(curl -sf \
    -H "X-Api-Key: ${MORI_API_KEY}" \
    "${MORI_URL}/api/dream/state?key=git_watermark_${REPO}" \
    2>/dev/null \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('value') or '')" \
    2>/dev/null || echo "")

  if [ -n "$WATERMARK" ]; then
    RANGE="${WATERMARK}..HEAD"
  else
    # First push — cap at 20 commits to avoid overwhelming a new server
    RANGE="HEAD~20..HEAD"
  fi

  # Build a JSON array of commits using \x1f as field separator (safe inside --format)
  COMMITS_JSON=$(python3 -c "
import subprocess, json, sys
range_arg = sys.argv[1]
result = subprocess.run(
    ['git', 'log', '--reverse', range_arg,
     '--format=%H\x1f%h\x1f%s\x1f%an\x1f%aI'],
    capture_output=True, text=True
)
commits = []
for line in result.stdout.splitlines():
    parts = line.split('\x1f')
    if len(parts) < 5:
        continue
    commits.append({
        'sha':       parts[0].strip(),
        'short_sha': parts[1].strip(),
        'subject':   parts[2].strip(),
        'author':    parts[3].strip(),
        'timestamp': parts[4].strip(),
    })
print(json.dumps(commits))
" "$RANGE" 2>/dev/null || echo "[]")

  COMMIT_COUNT=$(python3 -c "import sys,json; print(len(json.loads(sys.argv[1])))" "$COMMITS_JSON" 2>/dev/null || echo "0")

  if [ "$COMMIT_COUNT" -gt 0 ]; then
    INGEST_PAYLOAD=$(python3 -c "
import json, sys
commits = json.loads(sys.argv[1])
print(json.dumps({'repo': sys.argv[2], 'branch': sys.argv[3], 'commits': commits, 'pusher': sys.argv[4]}))
" "$COMMITS_JSON" "$REPO" "$BRANCH" "$CLIENT" 2>/dev/null)

    RESULT=$(curl -sf -X POST \
      -H "X-Api-Key: ${MORI_API_KEY}" \
      -H "Content-Type: application/json" \
      -d "$INGEST_PAYLOAD" \
      "${MORI_URL}/api/ingest/git" \
      2>/dev/null || echo "")

    INGESTED=$(python3 -c "import sys,json; d=json.loads(sys.argv[1]); print(d.get('ingested',0))" "$RESULT" 2>/dev/null || echo "0")
    if [ "$INGESTED" -gt 0 ]; then
      echo "[mori] ingested ${INGESTED} commit(s) from ${REPO}" >&2
    fi
  fi
fi

exit 0
