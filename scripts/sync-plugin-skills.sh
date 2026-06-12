#!/usr/bin/env bash
# Sync repo skills/ into plugins/mori/skills/ before release or plugin install.
#
# plugins/mori/skills/ is a MANUAL MIRROR of skills/ (a committed copy, not generated),
# so a canonical-only edit silently leaves the plugin copy stale — exactly how the v2.2.21
# PostCompact→SessionStart line shipped fixed in skills/ but stale in the plugin mirror.
#
#   (no args)  sync:  rm -rf plugins/mori/skills && cp -a skills plugins/mori/skills
#   --check    verify the mirror matches skills/; exit 1 on drift (used by CI). No mutation.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/skills"
DEST="$ROOT/plugins/mori/skills"
if [ ! -d "$SRC" ]; then
  echo "Error: skills source not found: $SRC" >&2
  exit 1
fi

if [ "${1:-}" = "--check" ]; then
  if drift="$(diff -r "$SRC" "$DEST" 2>&1)"; then
    echo "✓ plugin skills mirror is in sync with skills/"
    exit 0
  fi
  echo "✗ plugin skills mirror has DRIFTED from skills/." >&2
  echo "  Fix: run 'bash scripts/sync-plugin-skills.sh' and commit the result." >&2
  echo "$drift" >&2
  exit 1
fi

rm -rf "$DEST"
cp -a "$SRC" "$DEST"
echo "Synced $SRC → $DEST"
ls "$DEST"
