#!/bin/bash
# Idempotent provisioning of the mori-intake Stage-1 service on the GCE VM.
#
# Run AS THE `mori` USER on the VM (it drives the mori-pg container + the mori
# user's rootless systemd). Safe to re-run — every step is idempotent.
#
#   ssh mori@<gce> 'bash -s' < deploy/gcp/provision-intake.sh
#
# What it does (Stage 1 — write-only intake, nothing promotes):
#   1. Recovers the Postgres superuser password from the main mori .env.
#   2. Generates + persists intake secrets to /data/mori-intake/.env (first run).
#   3. Creates a LEAST-PRIVILEGE `intake_app` role + the `intake` database it owns,
#      and REVOKEs CONNECT on the `mori` canon DB from PUBLIC so intake_app is
#      kernel-blocked from ever reaching canon (defence in depth beyond the app's
#      check_data_boundary() guard and beyond simply not carrying the canon DSN).
#   4. Installs + starts the mori-intake Quadlet unit.
#   5. Verifies /ready.
set -euo pipefail

INTAKE_DIR=/data/mori-intake
ENVFILE="$INTAKE_DIR/.env"
MAIN_ENV=/data/mori-advisor/.env
QUADLET_DIR="$HOME/.config/containers/systemd"
RT_DIR="/run/user/$(id -u)"
export XDG_RUNTIME_DIR="$RT_DIR"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$RT_DIR/bus"

log() { echo "  [provision-intake] $*"; }

# ── 1. Recover the pg superuser password (mori role owns mori-pg) ─────────────
MORI_PG_PASSWORD=$(sed -nE 's#^MORI_DATABASE_URL=postgresql://[^:]+:(.*)@[^@/]+/[^?]*.*$#\1#p' "$MAIN_ENV")
[ -z "$MORI_PG_PASSWORD" ] && { echo "FATAL: cannot recover pg password from $MAIN_ENV"; exit 1; }
psql() { podman exec -e PGPASSWORD="$MORI_PG_PASSWORD" -i mori-pg psql -U mori "$@"; }

# Guard: the REVOKE-from-PUBLIC below is only safe if `mori` is a SUPERUSER
# (superusers bypass the CONNECT check, so canon access is unaffected).
IS_SUPER=$(psql -tAc "SELECT rolsuper FROM pg_roles WHERE rolname='mori'" -d postgres | tr -d '[:space:]')
if [ "$IS_SUPER" != "t" ]; then
  echo "FATAL: role 'mori' is not a superuser — REVOKE CONNECT FROM PUBLIC on canon"
  echo "       would lock out mori-advisor. Aborting; provision the role grants by hand."
  exit 1
fi
log "pg superuser check OK (mori is superuser)"

# ── 2. Generate + persist intake secrets (first run only) ─────────────────────
mkdir -p "$INTAKE_DIR"
if [ ! -s "$ENVFILE" ]; then
  INTAKE_APP_PW=$(openssl rand -hex 24)
  # The provider (v0.3.0) uses ONE MORI_API_KEY for both its read client (mori
  # canon, :8968) and its intake write client (:8971). To avoid breaking the
  # agent's existing reads we register the agent's EXISTING key here as the
  # write-role key. Pass it in via INTAKE_WRITE_KEY; otherwise a fresh key is
  # generated (and the agent must then be pointed at it for both paths).
  INTAKE_WRITE_KEY="${INTAKE_WRITE_KEY:-$(openssl rand -hex 32)}"
  cat > "$ENVFILE" <<EOF
# mori-intake Stage-1 env (mode 600). Single source of truth; persists on /data.
# NOTE: deliberately NO MORI_DATABASE_URL — the running service must never carry
# canon credentials. The data boundary is enforced by the intake_app role's lack
# of CONNECT on the mori DB (see provision-intake.sh step 3).
MORI_INTAKE_DATABASE_URL=postgresql://intake_app:${INTAKE_APP_PW}@localhost:5432/intake
MORI_API_KEYS=intake-hermes:${INTAKE_WRITE_KEY}
MORI_API_KEY_ROLES=intake-hermes:write
MORI_INTAKE_PORT=8971
MORI_INTAKE_HOST=0.0.0.0
MORI_INTAKE_POOL_MIN=2
MORI_INTAKE_POOL_MAX=4
MORI_INTAKE_RATE_LIMIT_PER_MIN=120
MORI_INTAKE_PENDING_TTL_HOURS=720
MORI_INTAKE_PURGE_INTERVAL_SEC=3600
MORI_INTAKE_MAX_CONTENT_BYTES=65536
EOF
  chmod 600 "$ENVFILE"
  log "wrote $ENVFILE (new secrets generated)"
else
  log "$ENVFILE exists — reusing (idempotent)"
  INTAKE_APP_PW=$(sed -nE 's#^MORI_INTAKE_DATABASE_URL=postgresql://intake_app:(.*)@.*#\1#p' "$ENVFILE")
  [ -z "$INTAKE_APP_PW" ] && { echo "FATAL: could not parse intake_app password from $ENVFILE"; exit 1; }
fi

# ── 3. Role + database + kernel-enforced boundary (idempotent) ────────────────
# Role create/alter (DO block is transaction-safe; CREATE DATABASE is not, so it
# is issued separately below via \gexec).
psql -d postgres -v ON_ERROR_STOP=1 <<SQL
DO \$do\$
BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname='intake_app') THEN
    ALTER ROLE intake_app LOGIN PASSWORD '${INTAKE_APP_PW}';
  ELSE
    CREATE ROLE intake_app LOGIN PASSWORD '${INTAKE_APP_PW}';
  END IF;
END
\$do\$;
SQL

# CREATE DATABASE intake OWNER intake_app (only if absent — cannot run in a txn).
psql -d postgres -v ON_ERROR_STOP=1 -tAc \
  "SELECT 'CREATE DATABASE intake OWNER intake_app' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='intake')" \
  | grep -q CREATE && psql -d postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE intake OWNER intake_app" \
  && log "created database 'intake' (owner intake_app)" || log "database 'intake' already present"

# Kernel-enforced boundary: intake_app can NEVER connect to canon.
psql -d postgres -v ON_ERROR_STOP=1 <<SQL
REVOKE CONNECT ON DATABASE mori FROM PUBLIC;
REVOKE ALL      ON DATABASE mori FROM intake_app;
GRANT  CONNECT  ON DATABASE mori TO mori;   -- belt-and-braces: owner keeps access
SQL
log "boundary set: REVOKE CONNECT ON mori FROM PUBLIC (intake_app kernel-blocked)"

# Sanity: prove intake_app cannot reach canon, and CAN reach intake.
if podman exec -e PGPASSWORD="$INTAKE_APP_PW" mori-pg psql -U intake_app -d mori -tAc "SELECT 1" >/dev/null 2>&1; then
  echo "FATAL: intake_app CAN still connect to mori canon — boundary NOT enforced. Aborting."
  exit 1
fi
log "verified: intake_app is REFUSED on canon DB 'mori' ✓"
podman exec -e PGPASSWORD="$INTAKE_APP_PW" mori-pg psql -U intake_app -d intake -tAc "SELECT 1" >/dev/null \
  && log "verified: intake_app CAN connect to 'intake' ✓"

# ── 4. Install + start the Quadlet unit ───────────────────────────────────────
install -d -m 700 "$QUADLET_DIR"
if [ ! -f "$QUADLET_DIR/mori-intake.container" ]; then
  echo "FATAL: $QUADLET_DIR/mori-intake.container not installed — copy it from"
  echo "       deploy/gcp/quadlet/mori-intake.container first."
  exit 1
fi
systemctl --user daemon-reload
systemctl --user start mori-intake.service
log "mori-intake.service started"

# ── 5. Verify readiness ───────────────────────────────────────────────────────
for i in $(seq 1 15); do
  sleep 2
  if curl -sf "http://localhost:8971/ready" >/dev/null 2>&1; then
    log "READY: $(curl -s http://localhost:8971/ready)"
    exit 0
  fi
done
echo "FATAL: mori-intake did not become ready in 30s"
systemctl --user status mori-intake.service --no-pager 2>&1 | tail -20 || true
journalctl --user -u mori-intake.service --no-pager 2>&1 | tail -30 || true
exit 1
