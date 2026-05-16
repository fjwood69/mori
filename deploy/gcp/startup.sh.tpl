#!/bin/bash
# Moku-advisor GCE startup script
# Installs Podman, mounts persistent disk, pulls and runs the container
# as a rootless Podman user for proper UID mapping.

set -euo pipefail

# ── Install dependencies ─────────────────────────────────────────────────
apt-get update -qq
apt-get install -y -qq podman sqlite3

# ── Create moku system user for rootless Podman ─────────────────────────
# UID 10001 matches the container's appuser. Rootless Podman maps this
# through /etc/subuid — the container sees UID 10001, the host sees the
# mapped UID from the moku user's namespace.
if ! id moku &>/dev/null; then
  useradd -r -u 10001 -m -s /bin/bash moku
  # Enable lingering for rootless containers to survive logout
  loginctl enable-linger moku
fi

MOKU_HOME=$(getent passwd moku | cut -d: -f6)

# ── Mount persistent data disk ──────────────────────────────────────────
DATA_DEV=$(readlink -f /dev/disk/by-id/google-moku-advisor-data || echo "")
if [ -n "$DATA_DEV" ] && ! mountpoint -q /data; then
  mkdir -p /data
  blkid "$DATA_DEV" || mkfs.ext4 "$DATA_DEV"
  mount "$DATA_DEV" /data
  mkdir -p /data/moku-advisor
  # Use podman unshare for correct rootless ownership
  # Without this, the container's appuser (UID 10001) can't write to the volume
  su - moku -c "podman unshare chown 10001:10001 /data/moku-advisor"
  # Add to fstab for persistence across reboots
  UUID=$(blkid -s UUID -o value "$DATA_DEV")
  if ! grep -q "$UUID" /etc/fstab; then
    echo "UUID=$UUID /data ext4 defaults,nofail 0 2" >> /etc/fstab
  fi
fi

# ── Install Tailscale ────────────────────────────────────────────────────
if ! command -v tailscale &>/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
  tailscale up --auth-key="${tailscale_auth_key}" --hostname=moku-advisor
fi

# ── Pull and run container (rootless) ────────────────────────────────────
# The container runs as --network=host so Tailscale can reach it directly
# on port 8968. Secrets are pulled from GCP Secret Manager at startup.
# Note: rotating a secret requires a container restart to pick up the new value.

CONTAINER_IMAGE="${container_image}"

# Pull the image as the moku user
su - moku -c "podman pull '$CONTAINER_IMAGE'"

# Remove existing container if present
su - moku -c "podman rm -f moku-advisor 2>/dev/null || true"

# Run the container as the moku user (rootless)
su - moku -c "podman run -d --name moku-advisor --restart=always --network=host \
  -v /data/moku-advisor:/data/moku-advisor:Z \
  -e MOKU_ADVISOR_DATA=/data/moku-advisor \
  -e MOKU_PROVIDER_MODE=direct \
  -e MOKU_API_KEY=\"\$(gcloud secrets versions access latest --secret=MOKU_API_KEY --project=${project_id} 2>/dev/null || echo '')\" \
  -e MOKU_ADVISOR_API_KEY=\"\$(gcloud secrets versions access latest --secret=MOKU_ADVISOR_API_KEY --project=${project_id} 2>/dev/null || echo '')\" \
  -e MOKU_BASE_URL=\"\$(gcloud secrets versions access latest --secret=MOKU_BASE_URL --project=${project_id} 2>/dev/null || echo '')\" \
  -e MOKU_MODEL=\"\$(gcloud secrets versions access latest --secret=MOKU_MODEL --project=${project_id} 2>/dev/null || echo '')\" \
  -e MOKU_DREAM_MODEL=\"\$(gcloud secrets versions access latest --secret=MOKU_DREAM_MODEL --project=${project_id} 2>/dev/null || echo '')\" \
  -e MOKU_TRUSTED_DREAMERS=\"\$(gcloud secrets versions access latest --secret=MOKU_TRUSTED_DREAMERS --project=${project_id} 2>/dev/null || echo '')\" \
  -e MOKU_NATS_URL=\"\$(gcloud secrets versions access latest --secret=MOKU_NATS_URL --project=${project_id} 2>/dev/null || echo '')\" \
  '$CONTAINER_IMAGE'"

echo "Moku-advisor container started."