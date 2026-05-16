#!/bin/bash
# Moku-advisor GCE startup script
# Installs Podman + Tailscale, mounts the persistent data disk,
# ensures the moku user has subuid mappings for rootless Podman,
# then starts the container as the moku user.

set -u

# ── Install dependencies ─────────────────────────────────────────────────
apt-get update -qq
apt-get install -y -qq podman sqlite3

# Verify podman is available
if ! command -v podman &>/dev/null; then
  echo "FATAL: podman not installed"
  exit 1
fi

# ── Create moku user for rootless Podman ─────────────────────────────────
# NOTE: no -r flag — a regular user (GID=10001) gets auto subuid mappings.
if ! id moku &>/dev/null; then
  useradd -u 10001 -m -s /bin/bash moku
  loginctl enable-linger moku
fi

# Ensure home dir is accessible (su - moku needs this)
chmod 755 /home/moku 2>/dev/null || true

# ── Mount persistent data disk ──────────────────────────────────────────
DATA_DEV=$(readlink -f /dev/disk/by-id/google-moku-data || echo "")
if [ -n "$DATA_DEV" ] && ! mountpoint -q /data; then
  mkdir -p /data
  # Only format if blkid explicitly says "no filesystem" (exit 2)
  blkid "$DATA_DEV" 2>&1 | grep -q "unrecognized" && mkfs.ext4 "$DATA_DEV" || true
  mount "$DATA_DEV" /data || echo "WARN: mount failed, disk may already be mounted"
  mkdir -p /data/moku-advisor
  chown moku:moku /data/moku-advisor
  chmod 755 /data/moku-advisor
  UUID=$(blkid -s UUID -o value "$DATA_DEV")
  if [ -n "$UUID" ] && ! grep -q "$UUID" /etc/fstab 2>/dev/null; then
    echo "UUID=$UUID /data ext4 defaults,nofail 0 2" >> /etc/fstab
  fi
fi

# ── Install Tailscale ────────────────────────────────────────────────────
if ! command -v tailscale &>/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
  tailscale up --auth-key="${tailscale_auth_key}" --hostname=ca-gcp-moku-advisor
fi

# ── Fetch secrets as root (GCE service account) ─────────────────────────
# The moku user can't call gcloud secrets — it runs under GCE's service
# account identity which is only available to root or via metadata server.
MOKU_API_KEY=$(gcloud secrets versions access latest --secret=MOKU_API_KEY --project=${project_id} 2>/dev/null || echo "")
MOKU_ADVISOR_API_KEY=$(gcloud secrets versions access latest --secret=MOKU_ADVISOR_API_KEY --project=${project_id} 2>/dev/null || echo "")
MOKU_BASE_URL=$(gcloud secrets versions access latest --secret=MOKU_BASE_URL --project=${project_id} 2>/dev/null || echo "")
MOKU_MODEL=$(gcloud secrets versions access latest --secret=MOKU_MODEL --project=${project_id} 2>/dev/null || echo "")
MOKU_DREAM_MODEL=$(gcloud secrets versions access latest --secret=MOKU_DREAM_MODEL --project=${project_id} 2>/dev/null || echo "")
MOKU_TRUSTED_DREAMERS=$(gcloud secrets versions access latest --secret=MOKU_TRUSTED_DREAMERS --project=${project_id} 2>/dev/null || echo "")
MOKU_NATS_URL=$(gcloud secrets versions access latest --secret=MOKU_NATS_URL --project=${project_id} 2>/dev/null || echo "")
GHCR_TOKEN=$(gcloud secrets versions access latest --secret=GHCR_TOKEN --project=${project_id} 2>/dev/null || echo "")

# Validate critical secrets
if [ -z "$MOKU_API_KEY" ]; then
  echo "WARN: MOKU_API_KEY is empty — container will start without provider access"
fi

# ── Pull and run container (rootless) ────────────────────────────────────
# Uses su - moku -c so podman finds ~/.local/share/containers.
# XDG_RUNTIME_DIR is required for rootless podman to talk to the session.
# --user 0 maps container root → host UID 10001 (moku), so the container's
# appuser can write to the bind-mounted /data/moku-advisor.

CONTAINER_IMAGE="${container_image}"
RUNTIME_DIR="/run/user/10001"

# Safety net: ensure runtime dir exists (loginctl enable-linger may not
# have created it yet on first boot)
if [ ! -d "$RUNTIME_DIR" ]; then
  mkdir -p "$RUNTIME_DIR"
  chown moku:moku "$RUNTIME_DIR"
  chmod 700 "$RUNTIME_DIR"
fi

# Authenticate to GHCR
if [ -n "$GHCR_TOKEN" ]; then
  echo "$GHCR_TOKEN" | su - moku -c "XDG_RUNTIME_DIR=$RUNTIME_DIR podman login ghcr.io -u fjwood69 --password-stdin"
fi

# Pull image with retries
for i in 1 2 3; do
  su - moku -c "XDG_RUNTIME_DIR=$RUNTIME_DIR podman pull '$CONTAINER_IMAGE'" && break
  echo "Pull attempt $i failed, retrying in 10s..."
  sleep 10
done

# Remove old container if it exists
su - moku -c "XDG_RUNTIME_DIR=$RUNTIME_DIR podman rm -f moku-advisor 2>/dev/null; true"

# Start container
su - moku -c "XDG_RUNTIME_DIR=$RUNTIME_DIR podman run -d --name moku-advisor --restart=always --network=host \
  --user 0 \
  -v /data/moku-advisor:/data/moku-advisor:Z \
  -e MOKU_ADVISOR_DATA=/data/moku-advisor \
  -e MOKU_PROVIDER_MODE=direct \
  -e MOKU_API_KEY='$MOKU_API_KEY' \
  -e MOKU_ADVISOR_API_KEY='$MOKU_ADVISOR_API_KEY' \
  -e MOKU_BASE_URL='$MOKU_BASE_URL' \
  -e MOKU_MODEL='$MOKU_MODEL' \
  -e MOKU_DREAM_MODEL='$MOKU_DREAM_MODEL' \
  -e MOKU_TRUSTED_DREAMERS='$MOKU_TRUSTED_DREAMERS' \
  -e MOKU_NATS_URL='$MOKU_NATS_URL' \
  '$CONTAINER_IMAGE'"

echo "Moku-advisor container started."

# ── Set up dream cron (every 4 hours) ────────────────────────────────────
DREAM_CRON="0 */4 * * * XDG_RUNTIME_DIR=$RUNTIME_DIR podman exec moku-advisor python -m moku_advisor.dream_job >/data/moku-advisor/dream-cron.log 2>&1"
(crontab -l 2>/dev/null | grep -v moku-advisor; echo "$DREAM_CRON") | crontab -
echo "Dream cron installed."
