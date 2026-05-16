#!/bin/bash
# Moku-advisor GCE startup script
# Installs Podman + Tailscale, mounts the persistent data disk,
# ensures the moku user has subuid mappings for rootless Podman,
# then starts the container as the moku user.

set -u

# ── Install dependencies ─────────────────────────────────────────────────
apt-get update -qq
apt-get install -y -qq podman sqlite3

# ── Create moku user for rootless Podman ─────────────────────────────────
# NOTE: no -r flag — a regular user (GID=10001) gets auto subuid mappings.
if ! id moku &>/dev/null; then
  useradd -u 10001 -m -s /bin/bash moku
  loginctl enable-linger moku
fi

# ── Mount persistent data disk ──────────────────────────────────────────
DATA_DEV=$(readlink -f /dev/disk/by-id/google-moku-data || echo "")
if [ -n "$DATA_DEV" ] && ! mountpoint -q /data; then
  mkdir -p /data
  blkid "$DATA_DEV" || mkfs.ext4 "$DATA_DEV"
  mount "$DATA_DEV" /data
  mkdir -p /data/moku-advisor
  chown moku:moku /data/moku-advisor
  chmod 755 /data/moku-advisor
  UUID=$(blkid -s UUID -o value "$DATA_DEV")
  if ! grep -q "$UUID" /etc/fstab; then
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

# ── Pull and run container (rootless) ────────────────────────────────────
# Uses su - moku -c with HOME set so podman finds ~/.local/share/containers.
# XDG_RUNTIME_DIR is required for rootless podman to talk to the session.

CONTAINER_IMAGE="${container_image}"
RUNTIME_DIR="/run/user/10001"

# Pull image
su - moku -c "XDG_RUNTIME_DIR=$RUNTIME_DIR podman pull '$CONTAINER_IMAGE'"

# Remove old container if it exists
su - moku -c "XDG_RUNTIME_DIR=$RUNTIME_DIR podman rm -f moku-advisor 2>/dev/null; true"

# Start container
su - moku -c "XDG_RUNTIME_DIR=$RUNTIME_DIR podman run -d --name moku-advisor --restart=always --network=host \
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
