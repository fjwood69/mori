#!/bin/bash
# Moku-advisor GCE startup script
# Installs Podman + Tailscale, mounts the persistent data disk,
# ensures the mori user has subuid mappings for rootless Podman,
# then starts the container as the mori user.

set -u

# ── Install dependencies ─────────────────────────────────────────────────
apt-get update -qq
apt-get install -y -qq podman sqlite3

# Verify podman is available
if ! command -v podman &>/dev/null; then
  echo "FATAL: podman not installed"
  exit 1
fi

# ── Create mori user for rootless Podman ─────────────────────────────────
# NOTE: no -r flag — a regular user (GID=10001) gets auto subuid mappings.
if ! id mori &>/dev/null; then
  useradd -u 10001 -m -s /bin/bash mori
  loginctl enable-linger mori
fi

# Ensure home dir is accessible (su - mori needs this)
chmod 755 /home/mori 2>/dev/null || true

# ── Mount persistent data disk ──────────────────────────────────────────
DATA_DEV=$(readlink -f /dev/disk/by-id/google-mori-data || echo "")
if [ -n "$DATA_DEV" ] && ! mountpoint -q /data; then
  mkdir -p /data
  # Only format if blkid explicitly says "no filesystem" (exit 2)
  blkid "$DATA_DEV" 2>&1 | grep -q "unrecognized" && mkfs.ext4 "$DATA_DEV" || true
  mount "$DATA_DEV" /data || echo "WARN: mount failed, disk may already be mounted"
  mkdir -p /data/mori-advisor
  chown mori:mori /data/mori-advisor
  chmod 755 /data/mori-advisor
  UUID=$(blkid -s UUID -o value "$DATA_DEV")
  if [ -n "$UUID" ] && ! grep -q "$UUID" /etc/fstab 2>/dev/null; then
    echo "UUID=$UUID /data ext4 defaults,nofail 0 2" >> /etc/fstab
  fi
fi

# ── Install Tailscale ────────────────────────────────────────────────────
if ! command -v tailscale &>/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
  tailscale up --auth-key="${tailscale_auth_key}" --hostname=ca-gcp-mori-advisor
fi

# ── Fetch secrets as root (GCE service account) ─────────────────────────
# The mori user can't call gcloud secrets — it runs under GCE's service
# account identity which is only available to root or via metadata server.
MORI_API_KEY=$(gcloud secrets versions access latest --secret=MORI_API_KEY --project=${project_id} 2>/dev/null || echo "")
MORI_ADVISOR_API_KEY=$(gcloud secrets versions access latest --secret=MORI_ADVISOR_API_KEY --project=${project_id} 2>/dev/null || echo "")
MORI_BASE_URL=$(gcloud secrets versions access latest --secret=MORI_BASE_URL --project=${project_id} 2>/dev/null || echo "")
MORI_MODEL=$(gcloud secrets versions access latest --secret=MORI_MODEL --project=${project_id} 2>/dev/null || echo "")
MORI_DREAM_MODEL=$(gcloud secrets versions access latest --secret=MORI_DREAM_MODEL --project=${project_id} 2>/dev/null || echo "")
MORI_TRUSTED_DREAMERS=$(gcloud secrets versions access latest --secret=MORI_TRUSTED_DREAMERS --project=${project_id} 2>/dev/null || echo "")
MORI_NATS_URL=$(gcloud secrets versions access latest --secret=MORI_NATS_URL --project=${project_id} 2>/dev/null || echo "")
GHCR_TOKEN=$(gcloud secrets versions access latest --secret=GHCR_TOKEN --project=${project_id} 2>/dev/null || echo "")
BIFROST_ADMIN_PASSWORD=$(gcloud secrets versions access latest --secret=BIFROST_ADMIN_PASSWORD --project=${project_id} 2>/dev/null || echo "")
DEEPINFRA_API_KEY=$(gcloud secrets versions access latest --secret=DEEPINFRA_API_KEY --project=${project_id} 2>/dev/null || echo "")
NOVITA_API_KEY=$(gcloud secrets versions access latest --secret=NOVITA_API_KEY --project=${project_id} 2>/dev/null || echo "")
PARASAIL_API_KEY=$(gcloud secrets versions access latest --secret=PARASAIL_API_KEY --project=${project_id} 2>/dev/null || echo "")
VERTEX_API_KEY=$(gcloud secrets versions access latest --secret=VERTEX_API_KEY --project=${project_id} 2>/dev/null || echo "")
CLOUDFLARE_API_KEY=$(gcloud secrets versions access latest --secret=CLOUDFLARE_API_KEY --project=${project_id} 2>/dev/null || echo "")
FIREWORKS_API_KEY=$(gcloud secrets versions access latest --secret=FIREWORKS_API_KEY --project=${project_id} 2>/dev/null || echo "")

# Validate critical secrets
if [ -z "$MORI_API_KEY" ]; then
  echo "WARN: MORI_API_KEY is empty — container will start without provider access"
fi

# ── Pull and run containers (rootless) ──────────────────────────────────
# Uses su - mori -c so podman finds ~/.local/share/containers.
# XDG_RUNTIME_DIR is required for rootless podman to talk to the session.
# --user 0 maps container root → host UID 10001 (mori), so the container's
# appuser can write to the bind-mounted /data directory.

CONTAINER_IMAGE="${container_image}"
BIFROST_IMAGE="ghcr.io/fjwood69/bifrost:claude-code-compat"
RUNTIME_DIR="/run/user/10001"

# Safety net: ensure runtime dir exists (loginctl enable-linger may not
# have created it yet on first boot)
if [ ! -d "$RUNTIME_DIR" ]; then
  mkdir -p "$RUNTIME_DIR"
  chown mori:mori "$RUNTIME_DIR"
  chmod 700 "$RUNTIME_DIR"
fi

# Ensure Bifrost data dir exists
mkdir -p /data/bifrost
chown mori:mori /data/bifrost
chmod 755 /data/bifrost

# Authenticate to GHCR
if [ -n "$GHCR_TOKEN" ]; then
  echo "$GHCR_TOKEN" | su - mori -c "XDG_RUNTIME_DIR=$RUNTIME_DIR podman login ghcr.io -u fjwood69 --password-stdin"
fi

# Pull images with retries
for img in "$CONTAINER_IMAGE" "$BIFROST_IMAGE"; do
  for i in 1 2 3; do
    su - mori -c "XDG_RUNTIME_DIR=$RUNTIME_DIR podman pull '$img'" && break
    echo "Pull attempt $i for $img failed, retrying in 10s..."
    sleep 10
  done
done

# Remove old containers if they exist
su - mori -c "XDG_RUNTIME_DIR=$RUNTIME_DIR podman rm -f mori-advisor 2>/dev/null; true"
su - mori -c "XDG_RUNTIME_DIR=$RUNTIME_DIR podman rm -f bifrost 2>/dev/null; true"

# Start Bifrost container (port 8787)
su - mori -c "XDG_RUNTIME_DIR=$RUNTIME_DIR podman run -d --name bifrost --restart=always --network=host \
  --user 0 \
  -v /data/bifrost:/app/data:Z \
  -e APP_PORT=8787 \
  -e APP_HOST=0.0.0.0 \
  -e LOG_LEVEL=info \
  -e LOG_STYLE=json \
  -e BIFROST_ADMIN_USER=fjwood \
  -e BIFROST_ADMIN_PASSWORD='$BIFROST_ADMIN_PASSWORD' \
  '$BIFROST_IMAGE'"

echo "Bifrost container started."

# Wait for Bifrost to be ready
sleep 5

# Seed Bifrost config if this is a fresh install (no config.db yet)
if [ ! -f /data/bifrost/config.db ]; then
  echo "Seeding Bifrost config..."
  python3 /dev/stdin << SEEDEOF
import sqlite3, json, os, sys, uuid, subprocess

PROJECT = "${project_id}"
DB = "/data/bifrost/config.db"

def gcp_secret(name):
    """Fetch a secret from GCP Secret Manager."""
    try:
        r = subprocess.run(
            ["gcloud", "secrets", "versions", "access", "latest",
             "--secret", name, "--project", PROJECT],
            capture_output=True, text=True, timeout=10
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""

conn = sqlite3.connect(DB)

print("  Creating providers...")

# ── Providers ──────────────────────────────────────────────────────────
providers = {
    "Deepinfra": {"base_url": "https://api.deepinfra.com/v1/openai"},
    "Novita": {"base_url": "https://api.novita.ai/v3/openai"},
    "parasail": {"base_url": "https://api.parasail.io/v1"},
    "vertex": {"base_url": "https://us-central1-aiplatform.googleapis.com/v1beta1/projects/mori-genai/locations/us-central1/endpoints/openapi"},
    "Cloudflare Workers AI": {"base_url": "https://api.cloudflare.com/client/v4/accounts/8d97411e384474732cfab7b9599a2253/ai"},
    "fireworks": {"base_url": "https://api.fireworks.ai/inference/v1"},
}

for name, cfg in providers.items():
    conn.execute("""
        INSERT OR IGNORE INTO config_providers (name, network_config_json, custom_provider_config_json, status)
        VALUES (?, ?, '{"base_provider_type":"openai","is_key_less":false}', 'unknown')
    """, (name, json.dumps({"base_url": cfg["base_url"], "default_request_timeout_in_seconds": 30, "max_retries": 0})))

conn.commit()

# ── API Keys ────────────────────────────────────────────────────────────
prov_map = {r[1]: r[0] for r in conn.execute("SELECT id, name FROM config_providers").fetchall()}
print(f"  Provider IDs: {prov_map}")

keys_map = {
    "mori-genai-deepinfra": ("Deepinfra", "DEEPINFRA_API_KEY"),
    "mori-genai-novita": ("Novita", "NOVITA_API_KEY"),
    "mori-genai-parasail": ("parasail", "PARASAIL_API_KEY"),
    "mori-genai-vertex": ("vertex", "VERTEX_API_KEY"),
    "mori-genai-cloudflare": ("Cloudflare Workers AI", "CLOUDFLARE_API_KEY"),
    "mori-genai-fireworks": ("fireworks", "FIREWORKS_API_KEY"),
}

for kname, (prov, secret_name) in keys_map.items():
    value = gcp_secret(secret_name)
    if not value:
        print(f"  WARN: {secret_name} not found in Secret Manager, skipping")
        continue
    pid = prov_map.get(prov)
    if not pid:
        print(f"  WARN: provider {prov} not found, skipping key {kname}")
        continue
    conn.execute("""
        INSERT OR IGNORE INTO config_keys (name, provider_id, provider, value, enabled, status)
        VALUES (?, ?, ?, ?, 1, 'success')
    """, (kname, pid, prov, value))
    print(f"  Added key {kname} for {prov}")

conn.commit()

# ── Virtual Key for mori-advisor ───────────────────────────────────────
vk_id = str(uuid.uuid4())
vk_value = "sk-bf-mori-advisor-gce-001"
conn.execute("""
    INSERT OR IGNORE INTO governance_virtual_keys (id, name, value, description)
    VALUES (?, ?, ?, ?)
""", (vk_id, "mori-advisor", vk_value, json.dumps({"model_override": "Deepinfra/deepseek-ai/DeepSeek-V4-Flash"})))
print(f"  VK created: {vk_value}")

# Link VK to all providers
for pid in prov_map.values():
    conn.execute("""
        INSERT OR IGNORE INTO governance_virtual_key_provider_configs (virtual_key_id, provider, allow_all_keys)
        VALUES (?, ?, 1)
    """, (vk_id, pid))

conn.commit()
conn.close()
print("Bifrost config seeded.")
SEEDEOF
  echo "Bifrost config seeded."
fi

# Start mori-advisor container (port 8968) — Bifrost mode via localhost:8787.
# Uses ghcr.io/fjwood69/moku:latest until the mori image exists.
# The moku image uses MOKU_* env vars (not MORI_*) — do not rename.
su - mori -c "XDG_RUNTIME_DIR=$RUNTIME_DIR podman run -d --name mori-advisor --restart=always --network=host \
  --user 0 \
  -v /data/mori-advisor:/data/mori-advisor:Z \
  -e MOKU_ADVISOR_DATA=/data/mori-advisor \
  -e MOKU_PROVIDER_MODE=bifrost \
  -e MOKU_API_KEY=sk-bf-mori-advisor-gce-001 \
  -e MOKU_ADVISOR_API_KEY='$MORI_ADVISOR_API_KEY' \
  -e MOKU_BASE_URL=http://localhost:8787 \
  -e MOKU_MODEL='$MORI_MODEL' \
  -e MOKU_DREAM_MODEL='$MORI_DREAM_MODEL' \
  -e MOKU_TRUSTED_DREAMERS='$MORI_TRUSTED_DREAMERS' \
  -e MOKU_NATS_URL='$MORI_NATS_URL' \
  '$CONTAINER_IMAGE'"

echo "Mori-advisor container started."

# Start mori-ingestion container (port 8969) — same image, different entrypoint.
# Shares /data/mori-advisor bind-mount with mori-advisor (co-location mandatory).
su - mori -c "XDG_RUNTIME_DIR=$RUNTIME_DIR podman run -d --name mori-ingestion \
  --restart=always --network=host --user 0 \
  -v /data/mori-advisor:/data/mori-advisor:Z \
  -e MORI_ADVISOR_DATA=/data/mori-advisor \
  -e MORI_ADVISOR_API_KEY='$MORI_ADVISOR_API_KEY' \
  -e MORI_BASE_URL=http://localhost:8787 \
  -e MORI_MODEL='$MORI_MODEL' \
  -e MORI_DREAM_MODEL='$MORI_DREAM_MODEL' \
  -e MORI_TRUSTED_DREAMERS='$MORI_TRUSTED_DREAMERS' \
  -e MORI_NATS_URL='$MORI_NATS_URL' \
  '$CONTAINER_IMAGE' python -m mori_advisor.ingestion_server"

echo "Mori-ingestion container started."

# ── Set up dream cron (every 4 hours) ────────────────────────────────────
DREAM_CRON="0 */4 * * * XDG_RUNTIME_DIR=$RUNTIME_DIR podman exec mori-advisor python -m mori_advisor.dream_job >/data/mori-advisor/dream-cron.log 2>&1"
(crontab -l 2>/dev/null | grep -v mori-advisor; echo "$DREAM_CRON") | crontab -
echo "Dream cron installed."

# ── Set up backup cron (daily at 06:00 UTC) ──────────────────────────────
# Uses curl + GCE metadata server for auth — no gcloud SDK needed
BACKUP_SCRIPT="/usr/local/bin/mori-backup.sh"
cat > "$BACKUP_SCRIPT" << 'BACKUPEOF'
#!/bin/bash
# Daily SQLite backup to GCS backup bucket using metadata server auth
set -u
DB_DIR="/data/mori-advisor"
BUCKET="${backup_bucket}"
DATE=$(date +%Y%m%d)

# Get GCE service account access token from metadata server
TOKEN=$(curl -s "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" -H "Metadata-Flavor: Google" | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null || echo "")

if [ -z "$TOKEN" ]; then
  echo "ERROR: could not get GCE access token"
  exit 1
fi

for db in memories session_log; do
  if [ -f "$DB_DIR/$db.db" ]; then
    sqlite3 "$DB_DIR/$db.db" ".backup $DB_DIR/backup-$db-$DATE.db"
    gzip -f "$DB_DIR/backup-$db-$DATE.db"
    curl -sf -X PUT --data-binary @"$DB_DIR/backup-$db-$DATE.db.gz" \
      -H "Authorization: Bearer $TOKEN" \
      "https://storage.googleapis.com/$BUCKET/$db-$DATE.db.gz"
    RC=$?
    rm -f "$DB_DIR/backup-$db-$DATE.db.gz"
    if [ $RC -eq 0 ]; then
      echo "OK: $db-$DATE.db.gz uploaded"
    else
      echo "FAIL: $db upload exit code $RC"
    fi
  fi
done
echo "Backup complete: $DATE"
BACKUPEOF
chmod 755 "$BACKUP_SCRIPT"
BACKUP_CRON="0 6 * * * $BACKUP_SCRIPT >/data/mori-advisor/backup-cron.log 2>&1"
(crontab -l 2>/dev/null | grep -v mori-backup; echo "$BACKUP_CRON") | crontab -
echo "Backup cron installed."
