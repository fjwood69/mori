#!/usr/bin/env bash
# Sync repo skills/ into plugins/mori/skills/ before release or plugin install.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/skills"
DEST="$ROOT/plugins/mori/skills"
if [ ! -d "$SRC" ]; then
  echo "Error: skills source not found: $SRC" >&2
  exit 1
fi
rm -rf "$DEST"
cp -a "$SRC" "$DEST"
echo "Synced $SRC → $DEST"
ls "$DEST"
