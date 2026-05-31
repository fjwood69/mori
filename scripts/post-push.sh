#!/usr/bin/env bash
# Mori post-push hook — publishes GitPush event to Mori server
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

exit 0
