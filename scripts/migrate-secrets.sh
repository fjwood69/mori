#!/bin/bash
# One-time migration: copy secrets from ~/.claude/.secrets to GCP Secret Manager.
# Run this ONCE from the NUC after Terraform creates the secret resources.
#
# Usage: bash scripts/migrate-secrets.sh [--dry-run]

set -euo pipefail

PROJECT="${PROJECT_ID:-mori-genai}"
DRY_RUN=false
if [ "${1:-}" = "--dry-run" ]; then
  DRY_RUN=true
fi

SECRETS_FILE="${HOME}/dotfiles/.claude/.secrets"
if [ ! -f "$SECRETS_FILE" ]; then
  echo "ERROR: Secrets file not found at $SECRETS_FILE"
  echo "Run from the NUC where dotfiles are checked out."
  exit 1
fi

get_secret() {
  grep "^${1}=" "$SECRETS_FILE" | head -1 | cut -d= -f2-
}

MIGRATE=(
  "MORI_API_KEY"
  "MORI_ADVISOR_API_KEY"
  "MORI_BASE_URL"
  "MORI_MODEL"
  "MORI_DREAM_MODEL"
  "MORI_TRUSTED_DREAMERS"
  "MORI_NATS_URL"
)

for secret_id in "${MIGRATE[@]}"; do
  value=$(get_secret "$secret_id")
  if [ -z "$value" ]; then
    echo "WARNING: $secret_id not found in .secrets, skipping"
    continue
  fi

  if [ "$DRY_RUN" = true ]; then
    echo "[DRY RUN] Would add version to $secret_id (${#value} chars)"
  else
    echo "Adding version to $secret_id (${#value} chars)..."
    echo -n "$value" | gcloud secrets versions add "$secret_id" \
      --project="$PROJECT" --data-file=-
    echo "  OK"
  fi
done

echo "Done."
if [ "$DRY_RUN" = true ]; then
  echo "Run without --dry-run to apply."
fi