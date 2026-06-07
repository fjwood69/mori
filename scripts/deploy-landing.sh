#!/usr/bin/env bash
# deploy-landing.sh — sync web/landing/ to fjwood69/moriapp.dev via GitHub API
#
# Usage: deploy-landing.sh [--dry-run] [--message "custom commit message"]
#
# Reads GITHUB_TOKEN from ~/bin/get-secret.sh (falls back to env var).
# Syncs index.html and assets/*.svg from the mori repo landing dir to the
# moriapp.dev deploy target repo. Cloudflare Pages auto-deploys on push.
set -euo pipefail

# ── Config ──────────────────────────────────────────────────────────────────
DEPLOY_REPO="fjwood69/moriapp.dev"
DEPLOY_BRANCH="master"
SCRIPT_DIR="$(cd "$(dirname "$(realpath "$0")")" && pwd)"
LANDING_DIR="$(cd "${SCRIPT_DIR}/../web/landing" && pwd)"
GH_API="https://api.github.com"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; RESET='\033[0m'
ok()   { printf "${GREEN}✓${RESET}  %s\n" "$*"; }
info() { printf "${BOLD}→${RESET}  %s\n" "$*"; }
skip() { printf "${YELLOW}–${RESET}  %s\n" "$*"; }

# ── Args ─────────────────────────────────────────────────────────────────────
DRY_RUN=false
COMMIT_MSG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true ;;
    --message|-m) COMMIT_MSG="$2"; shift ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
  shift
done

[[ -z "$COMMIT_MSG" ]] && COMMIT_MSG="deploy: sync landing page from mori"

# ── Credentials ──────────────────────────────────────────────────────────────
if [[ -x "$HOME/bin/get-secret.sh" ]]; then
  GH_TOKEN="$("$HOME/bin/get-secret.sh" GITHUB_TOKEN 2>/dev/null)"
else
  GH_TOKEN="${GITHUB_TOKEN:-}"
fi
if [[ -z "$GH_TOKEN" ]]; then
  echo "ERROR: GITHUB_TOKEN not found" >&2; exit 1
fi

# ── Helpers ──────────────────────────────────────────────────────────────────
gh_get_sha() {
  local path="$1"
  curl -sf "${GH_API}/repos/${DEPLOY_REPO}/contents/${path}?ref=${DEPLOY_BRANCH}" \
    -H "Authorization: Bearer ${GH_TOKEN}" \
    2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('sha',''))" 2>/dev/null || echo ""
}

gh_put_file() {
  local path="$1" local_file="$2" msg="$3" sha="$4"
  local content
  content=$(base64 -w 0 "$local_file")
  local body
  if [[ -n "$sha" ]]; then
    body=$(python3 -c "import json; print(json.dumps({'message': '$msg', 'content': '$content', 'sha': '$sha', 'branch': '$DEPLOY_BRANCH'}))")
  else
    body=$(python3 -c "import json; print(json.dumps({'message': '$msg', 'content': '$content', 'branch': '$DEPLOY_BRANCH'}))")
  fi
  curl -sf -X PUT "${GH_API}/repos/${DEPLOY_REPO}/contents/${path}" \
    -H "Authorization: Bearer ${GH_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['commit']['sha'][:12])"
}

content_sha() {
  # Git blob SHA: sha1("blob <size>\0<content>")
  local file="$1"
  local size
  size=$(wc -c < "$file")
  (printf "blob %d\0" "$size"; cat "$file") | sha1sum | cut -d' ' -f1
}

# ── Files to sync ─────────────────────────────────────────────────────────────
declare -A FILES
FILES["index.html"]="${LANDING_DIR}/index.html"
for svg in "${LANDING_DIR}/assets/"*.svg; do
  [[ -f "$svg" ]] && FILES["assets/$(basename "$svg")"]="$svg"
done

# ── Sync ─────────────────────────────────────────────────────────────────────
echo ""
printf "${BOLD}deploy-landing → ${DEPLOY_REPO}:${DEPLOY_BRANCH}${RESET}\n"
[[ "$DRY_RUN" == true ]] && printf "${YELLOW}(dry run — no writes)${RESET}\n"
echo ""

CHANGED=0
SKIPPED=0

for repo_path in "${!FILES[@]}"; do
  local_file="${FILES[$repo_path]}"
  info "$repo_path"

  remote_sha=$(gh_get_sha "$repo_path")
  local_sha=$(content_sha "$local_file")

  if [[ "$remote_sha" == "$local_sha" ]]; then
    skip "unchanged"
    ((SKIPPED++)) || true
    continue
  fi

  if [[ "$DRY_RUN" == true ]]; then
    skip "would push (remote ${remote_sha:0:8} → local ${local_sha:0:8})"
    ((CHANGED++)) || true
    continue
  fi

  commit_sha=$(gh_put_file "$repo_path" "$local_file" "$COMMIT_MSG" "$remote_sha")
  ok "pushed (commit ${commit_sha})"
  ((CHANGED++)) || true
done

echo ""
if [[ "$DRY_RUN" == true ]]; then
  printf "${BOLD}Dry run complete — ${CHANGED} would change, ${SKIPPED} unchanged.${RESET}\n"
else
  if [[ $CHANGED -gt 0 ]]; then
    ok "Deployed ${CHANGED} file(s) — Cloudflare Pages will auto-deploy in ~30s"
    printf "   Live at: https://moriapp.dev\n"
  else
    printf "${BOLD}Nothing to deploy — all files up to date.${RESET}\n"
  fi
fi
echo ""
