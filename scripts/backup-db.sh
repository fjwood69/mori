#!/bin/bash
# Daily backup to GCS — supports SQLite (solo) and PostgreSQL (team) backends.
# Runs as root via cron. Auth: GCE metadata server (no credentials file needed).
#
# Usage (host-side, GCE):   sudo bash scripts/backup-db.sh
# Usage (in-container):     bash /app/scripts/backup-db.sh  (SQLite only)
#
# PostgreSQL path requires:
#   - MORI_DATABASE_URL set to postgresql://... in DATA_DIR/.env
#   - A `mori-pg` podman container running on the host (rootful)
#   - gsutil available on the host (/snap/bin/gsutil or in PATH)
#
# Retention: 14-day auto-delete via GCS lifecycle policy on the bucket.

set -euo pipefail

BUCKET="${MORI_BACKUP_BUCKET:?set MORI_BACKUP_BUCKET to your GCS backup bucket, e.g. mori-advisor-backups-<project>}"
DATA_DIR="${MORI_ADVISOR_DATA:-/data/mori-advisor}"
TIMESTAMP=$(date -u +%Y%m%d-%H%M%S)
TMPFILE="/tmp/mori-backup-${TIMESTAMP}"
trap 'rm -f "${TMPFILE}" "${TMPFILE}.gz" 2>/dev/null || true' EXIT

# GCE metadata server access token
TOKEN=$(curl -sf \
  "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" \
  -H "Metadata-Flavor: Google" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

if [ -z "$TOKEN" ]; then
  echo "ERROR: could not get GCE access token" >&2
  exit 1
fi

gcs_upload() {
  local local_file="$1"
  local remote_name="$2"
  curl -sf -X PUT \
    --data-binary @"${local_file}" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/octet-stream" \
    "https://storage.googleapis.com/${BUCKET}/${remote_name}"
}

# Detect backend from .env
MORI_DATABASE_URL="${MORI_DATABASE_URL:-}"
if [ -z "$MORI_DATABASE_URL" ] && [ -f "${DATA_DIR}/.env" ]; then
  MORI_DATABASE_URL=$(grep '^MORI_DATABASE_URL=' "${DATA_DIR}/.env" 2>/dev/null | cut -d= -f2- || true)
fi

if echo "${MORI_DATABASE_URL}" | grep -q '^postgresql'; then
  # ── PostgreSQL backup ──────────────────────────────────────────────────
  REMOTE_NAME="mori-pg-${TIMESTAMP}.sql.gz"
  echo "Backing up Postgres → ${REMOTE_NAME}"
  podman exec mori-pg pg_dump -U mori mori > "${TMPFILE}"
  gzip -9 "${TMPFILE}"
  gcs_upload "${TMPFILE}.gz" "${REMOTE_NAME}"
  echo "OK: ${REMOTE_NAME} uploaded ($(du -h "${TMPFILE}.gz" | cut -f1))"

  # Retention handled by GCS lifecycle policy (14-day auto-delete on bucket)

else
  # ── SQLite backup (solo deployments) ──────────────────────────────────
  DB_FILE="${DATA_DIR}/memories.db"
  if [ ! -f "${DB_FILE}" ]; then
    echo "No SQLite database at ${DB_FILE} — nothing to back up."
    exit 0
  fi
  REMOTE_NAME="memories-${TIMESTAMP}.db.gz"
  echo "Backing up SQLite → ${REMOTE_NAME}"
  sqlite3 "${DB_FILE}" ".backup ${TMPFILE}"
  gzip -9 "${TMPFILE}"
  gcs_upload "${TMPFILE}.gz" "${REMOTE_NAME}"
  echo "OK: ${REMOTE_NAME} uploaded ($(du -h "${DB_FILE}" | cut -f1))"
fi

echo "Backup complete: ${TIMESTAMP}"
