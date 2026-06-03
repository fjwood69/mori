#!/bin/bash
# Mori-advisor GCE startup script (generic).
# Installs Podman + Tailscale, mounts the persistent data disk, ensures the
# mori user has subuid mappings for rootless Podman, starts Postgres, then runs
# mori-advisor / mori-ingestion / mori-msg as the mori user.
#
# Provider: this template runs mori-advisor in `direct` mode (an OpenAI-compatible
# provider via MORI_BASE_URL + MORI_API_KEY). To front it with an LLM gateway,
# point `startup_template_path` at your own customised script.

set -u

# ── Install dependencies ─────────────────────────────────────────────────
apt-get update -qq
apt-get install -y -qq podman sqlite3

if ! command -v podman &>/dev/null; then
  echo "FATAL: podman not installed"
  exit 1
fi

# ── Create mori user for rootless Podman ─────────────────────────────────
# NOTE: no -r flag — a regular user gets auto subuid mappings.
if ! id mori &>/dev/null; then
  useradd -u 10001 -m -s /bin/bash mori
  loginctl enable-linger mori
fi
chmod 755 /home/mori 2>/dev/null || true

# ── Mount persistent data disk ──────────────────────────────────────────
DATA_DEV=$(readlink -f /dev/disk/by-id/google-mori-data || echo "")
if [ -n "$DATA_DEV" ] && ! mountpoint -q /data; then
  mkdir -p /data
  blkid "$DATA_DEV" 2>&1 | grep -q "unrecognized" && mkfs.ext4 "$DATA_DEV" || true
  mount "$DATA_DEV" /data || echo "WARN: mount failed, disk may already be mounted"
  mkdir -p /data/mori-advisor
  chown mori:mori /data/mori-advisor
  chmod 755 /data/mori-advisor
  # Postgres pgdata — owned by the host UID that maps to postgres uid=999 inside
  # the mori user's rootless Podman namespace (mori uid=10001, subuid base 296608
  # → container uid=999 → host uid=297606).
  mkdir -p /data/postgres/pgdata
  chmod 700 /data/postgres/pgdata
  chown -R 297606:297606 /data/postgres/pgdata
  UUID=$(blkid -s UUID -o value "$DATA_DEV")
  if [ -n "$UUID" ] && ! grep -q "$UUID" /etc/fstab 2>/dev/null; then
    echo "UUID=$UUID /data ext4 defaults,nofail 0 2" >> /etc/fstab
  fi
fi

# ── Restore SSH host keys + Tailscale state from the persistent disk ─────────
if [ -d "/data/ssh" ]; then
  cp -rp /data/ssh/ssh_host_* /etc/ssh/
  chmod 600 /etc/ssh/ssh_host_*_key
  chmod 644 /etc/ssh/ssh_host_*_key.pub
  systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true
fi
if [ -d "/data/tailscale" ]; then
  mkdir -p /var/lib/tailscale
  cp -r /data/tailscale/* /var/lib/tailscale/
  chmod 700 /var/lib/tailscale
  chmod 600 /var/lib/tailscale/tailscaled.state 2>/dev/null || true
fi

# ── Install + bring up Tailscale ─────────────────────────────────────────
if ! command -v tailscale &>/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
systemctl start tailscaled 2>/dev/null || true
if ! tailscale status &>/dev/null; then
  tailscale up --auth-key="${tailscale_auth_key}" --hostname=${tailscale_hostname}
else
  echo "Tailscale is already authenticated and running."
fi

# ── Fetch secrets as root (GCE service account) ─────────────────────────
MORI_API_KEY=$(gcloud secrets versions access latest --secret=MORI_API_KEY --project=${project_id} 2>/dev/null || echo "")
# Named per-client API keys (name:secret,...). Without this the server runs in
# open-auth mode (anyone on the tailnet). Strongly recommended.
MORI_API_KEYS=$(gcloud secrets versions access latest --secret=MORI_API_KEYS --project=${project_id} 2>/dev/null || echo "")
MORI_ADVISOR_API_KEY=$(gcloud secrets versions access latest --secret=MORI_ADVISOR_API_KEY --project=${project_id} 2>/dev/null || echo "")
MORI_BASE_URL=$(gcloud secrets versions access latest --secret=MORI_BASE_URL --project=${project_id} 2>/dev/null || echo "")
MORI_MODEL=$(gcloud secrets versions access latest --secret=MORI_MODEL --project=${project_id} 2>/dev/null || echo "")
MORI_DREAM_MODEL=$(gcloud secrets versions access latest --secret=MORI_DREAM_MODEL --project=${project_id} 2>/dev/null || echo "")
MORI_TRUSTED_DREAMERS=$(gcloud secrets versions access latest --secret=MORI_TRUSTED_DREAMERS --project=${project_id} 2>/dev/null || echo "")
MORI_NATS_URL=$(gcloud secrets versions access latest --secret=MORI_NATS_URL --project=${project_id} 2>/dev/null || echo "")
GHCR_TOKEN=$(gcloud secrets versions access latest --secret=GHCR_TOKEN --project=${project_id} 2>/dev/null || echo "")
MORI_PG_PASSWORD=$(gcloud secrets versions access latest --secret=MORI_PG_PASSWORD --project=${project_id} 2>/dev/null || echo "")
MORI_DATABASE_URL="postgresql://mori:$${MORI_PG_PASSWORD}@localhost:5432/mori"

if [ -z "$MORI_PG_PASSWORD" ]; then
  echo "FATAL: MORI_PG_PASSWORD is empty — cannot start postgres or mori-advisor"
  exit 1
fi

# ── Pull and run containers (rootless, as the mori user) ─────────────────
CONTAINER_IMAGE="${container_image}"
RUNTIME_DIR="/run/user/$(id -u mori)"
if [ ! -d "$RUNTIME_DIR" ]; then
  mkdir -p "$RUNTIME_DIR"; chown mori:mori "$RUNTIME_DIR"; chmod 700 "$RUNTIME_DIR"
fi

# Pull the image (with retries). Public images need no auth; for a private image,
# add a `podman login ghcr.io` step here using your GHCR_TOKEN secret.
for i in 1 2 3; do
  su - mori -c "XDG_RUNTIME_DIR=$RUNTIME_DIR podman pull '$CONTAINER_IMAGE'" && break
  echo "Pull attempt $i failed, retrying in 10s..."; sleep 10
done

for c in mori-advisor mori-ingestion mori-msg mori-pg; do
  su - mori -c "XDG_RUNTIME_DIR=$RUNTIME_DIR podman rm -f $c 2>/dev/null; true"
done

# ── Start Postgres (port 5432) ───────────────────────────────────────────
su - mori -c "XDG_RUNTIME_DIR=$RUNTIME_DIR podman run -d --name mori-pg \
  --restart=always --network=host \
  -e POSTGRES_USER=mori \
  -e POSTGRES_PASSWORD='$MORI_PG_PASSWORD' \
  -e POSTGRES_DB=mori \
  -e POSTGRES_INITDB_ARGS='--locale=en_US.utf8' \
  -v /data/postgres/pgdata:/var/lib/postgresql/data \
  docker.io/postgres:16 \
  postgres -c listen_addresses='*'"

echo "Waiting for Postgres to be ready..."
PG_READY=0
for i in $(seq 1 30); do
  if su - mori -c "XDG_RUNTIME_DIR=$RUNTIME_DIR podman exec mori-pg pg_isready -U mori" &>/dev/null; then
    PG_READY=1; break
  fi
  sleep 2
done
if [ $PG_READY -eq 0 ]; then
  echo "FATAL: Postgres did not become ready after 60s — aborting startup."; exit 1
fi

# ── Write the complete runtime env (single source of truth; CD reuses it) ─────
cat > /data/mori-advisor/.env <<ENVEOF
MORI_ADVISOR_DATA=/data/mori-advisor
MORI_DATABASE_URL=$MORI_DATABASE_URL
MORI_REQUIRE_POSTGRES=true
MORI_PROVIDER_MODE=direct
MORI_API_KEY=$MORI_API_KEY
MORI_BASE_URL=$MORI_BASE_URL
MORI_MODEL=$MORI_MODEL
MORI_DREAM_MODEL=$MORI_DREAM_MODEL
MORI_ADVISOR_API_KEY=$MORI_ADVISOR_API_KEY
MORI_API_KEYS=$MORI_API_KEYS
MORI_TRUSTED_DREAMERS=$MORI_TRUSTED_DREAMERS
MORI_NATS_URL=$MORI_NATS_URL
ENVEOF
chown mori:mori /data/mori-advisor/.env
chmod 600 /data/mori-advisor/.env

# ── Start mori-advisor (8968), mori-ingestion (8969), mori-msg ───────────────
su - mori -c "XDG_RUNTIME_DIR=$RUNTIME_DIR podman run -d --replace --name mori-advisor --restart=always --network=host \
  --user 0 -v /data/mori-advisor:/data/mori-advisor:Z --env-file /data/mori-advisor/.env '$CONTAINER_IMAGE'"

su - mori -c "XDG_RUNTIME_DIR=$RUNTIME_DIR podman run -d --replace --name mori-ingestion --restart=always --network=host \
  --user 0 -v /data/mori-advisor:/data/mori-advisor:Z --env-file /data/mori-advisor/.env \
  '$CONTAINER_IMAGE' python -m mori_advisor.ingestion_server"

su - mori -c "XDG_RUNTIME_DIR=$RUNTIME_DIR podman run -d --replace --name mori-msg --restart=always --network=host \
  --user 0 -v /data/mori-advisor:/data/mori-advisor:Z --env-file /data/mori-advisor/.env \
  -e MORI_MSG_HEADLESS_ENABLED=false '$CONTAINER_IMAGE' python -m mori_advisor.msg_daemon"

echo "Mori containers started."

# ── Dream cron (every 4 hours) ───────────────────────────────────────────
DREAM_CRON="0 */4 * * * XDG_RUNTIME_DIR=$RUNTIME_DIR podman exec mori-advisor python -m mori_advisor.dream_job 2>&1 | tee -a /data/mori-advisor/dream-cron.log | logger -t mori-dream"
(crontab -l 2>/dev/null | grep -v mori-advisor; echo "$DREAM_CRON") | crontab -

# ── Backup cron (daily 06:00 UTC): pg_dump → GCS via metadata-server auth ─────
BACKUP_SCRIPT="/usr/local/bin/mori-backup.sh"
cat > "$BACKUP_SCRIPT" << 'BACKUPEOF'
#!/bin/bash
set -u
BUCKET="${backup_bucket}"
DATE=$(date +%Y%m%d)
RUNTIME_DIR="/run/user/$(id -u mori)"

TOKEN=$(curl -s "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" \
  -H "Metadata-Flavor: Google" | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null || echo "")
[ -z "$TOKEN" ] && { echo "ERROR: no GCE access token"; exit 1; }

TMPFILE=$(mktemp /tmp/mori-pg-backup-XXXXXX)
REMOTE_NAME="mori-pg-$${DATE}.sql.gz"
XDG_RUNTIME_DIR=$RUNTIME_DIR podman exec mori-pg pg_dump -U mori mori | gzip -9 > "$${TMPFILE}.gz"
curl -sf -X PUT --data-binary @"$${TMPFILE}.gz" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/octet-stream" \
  "https://storage.googleapis.com/$BUCKET/$REMOTE_NAME" && echo "OK: $REMOTE_NAME" || echo "FAIL: $REMOTE_NAME"
rm -f "$${TMPFILE}" "$${TMPFILE}.gz"
BACKUPEOF
chmod 755 "$BACKUP_SCRIPT"
BACKUP_CRON="0 6 * * * $BACKUP_SCRIPT >/data/mori-advisor/backup-cron.log 2>&1"
(crontab -l 2>/dev/null | grep -v mori-backup; echo "$BACKUP_CRON") | crontab -
echo "Startup complete."
