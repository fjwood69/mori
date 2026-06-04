#!/usr/bin/env bash
# Render the GCE startup script from Terraform and (optionally) push it to the
# instance's startup-script metadata — correctly and safely.
#
# WHY THIS EXISTS
#   The instance's metadata_startup_script is force-replacement in Terraform and
#   is marked `ignore_changes`, so the script is updated out-of-band rather than
#   via `terraform apply`. The obvious way to extract the rendered script —
#   `terraform console` — wraps multi-line string output in a `<<EOT … EOT`
#   heredoc. Naively parsing that (or running an escape-decode over it) corrupts
#   the script: lines collapse and commands break, while `bash -n` may still pass.
#   That corruption once took the instance down on reboot. The CORRECT extraction
#   is simply: drop the first (`<<EOT`) and last (`EOT`) lines and keep the bytes
#   between verbatim — no decoding. This script does exactly that, then validates
#   the result before it will push anything.
#
# USAGE
#   ./render-metadata.sh                 # render to ./startup-rendered.sh + validate
#   ./render-metadata.sh --push          # also back up live metadata, then push
#   INSTANCE=mori-advisor ZONE=… PROJECT=… ./render-metadata.sh --push
#
# The rendered file and metadata backups contain secrets and are gitignored.
set -euo pipefail

cd "$(dirname "$0")"

OUT="${OUT:-./startup-rendered.sh}"
INSTANCE="${INSTANCE:-mori-advisor}"
ZONE="${ZONE:-$(echo 'var.zone' | terraform console 2>/dev/null | tr -d '"')}"
PROJECT="${PROJECT:-$(echo 'var.project_id' | terraform console 2>/dev/null | tr -d '"')}"

echo "→ rendering local.startup_script (heredoc-safe extraction)…"
# nonsensitive() is required because the script embeds a sensitive var; sed strips
# the <<EOT / EOT wrapper lines, leaving the rendered bytes untouched.
echo 'nonsensitive(local.startup_script)' | terraform console | sed '1d;$d' > "$OUT"

# ── Validate before trusting it ──────────────────────────────────────────────
fail=0
[ "$(head -1 "$OUT")" = '#!/bin/bash' ] || { echo "  ✗ first line is not the shebang"; fail=1; }
grep -qE '^(<<EOT|EOT)$' "$OUT"        && { echo "  ✗ stray heredoc markers — extraction broke"; fail=1; }
grep -q '\\n' "$OUT"                   && { echo "  ✗ literal backslash-n — content was escape-mangled"; fail=1; }
bash -n "$OUT"                         || { echo "  ✗ bash syntax error"; fail=1; }
if [ "$fail" -ne 0 ]; then
  echo "✗ validation FAILED — refusing to use $OUT"; exit 1
fi
echo "✓ rendered + validated: $OUT ($(wc -l < "$OUT") lines)"

# ── Optional push ────────────────────────────────────────────────────────────
if [ "${1:-}" = "--push" ]; then
  if [ -z "$ZONE" ] || [ -z "$PROJECT" ]; then
    echo "✗ set ZONE and PROJECT (env vars) to push"; exit 1
  fi
  BK="./metadata-backup-$(date +%Y%m%d-%H%M%S).sh"
  echo "→ backing up current live metadata → $BK"
  gcloud compute instances describe "$INSTANCE" --project="$PROJECT" --zone="$ZONE" \
    --format="value(metadata.items.filter(key:startup-script).extract(value))" > "$BK" 2>/dev/null || \
    echo "  (warning: could not back up current metadata)"
  echo "→ pushing startup-script to $INSTANCE ($PROJECT/$ZONE)…"
  gcloud compute instances add-metadata "$INSTANCE" --project="$PROJECT" --zone="$ZONE" \
    --metadata-from-file "startup-script=$OUT"
  echo "✓ pushed — runs on next boot only (reboot to apply)."
fi
