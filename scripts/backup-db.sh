#!/bin/bash
# Daily SQLite backup to GCS.
# Uses sqlite3 .backup for a consistent snapshot (safe under concurrent writes).
#
# Usage: bash scripts/backup-db.sh
# Or via systemd timer: systemctl start mori-backup

set -euo pipefail

DATA_DIR="${MORI_ADVISOR_DATA:-/data/mori-advisor}"
BACKUP_BUCKET="${MORI_BACKUP_BUCKET:-gs://mori-advisor-backups-mori-genai}"
DB_FILE="${DATA_DIR}/memories.db"
TIMESTAMP=$(date -u +%Y%m%d-%H%M%S)
BACKUP_PATH="${BACKUP_BUCKET}/memories-${TIMESTAMP}.db.gz"

if [ ! -f "$DB_FILE" ]; then
  echo "No database found at $DB_FILE — nothing to back up."
  exit 0
fi

# sqlite3 .backup uses the SQLite backup API — consistent even under writes
sqlite3 "${DB_FILE}" ".backup /tmp/memories-backup.db"
gzip /tmp/memories-backup.db
gsutil cp /tmp/memories-backup.db.gz "${BACKUP_PATH}"
rm -f /tmp/memories-backup.db.gz

echo "Backup written to ${BACKUP_PATH} ($(du -h "$DB_FILE" | cut -f1))"