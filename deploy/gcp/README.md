# Mori GCE Deployment — Terraform

Deploys mori-advisor to a GCE VM using Terraform.

## Resources

- **GCE VM** — `e2-small` (2 vCPU, 2GB RAM), Ubuntu 24.04 LTS, 20GB boot disk
- **Persistent disk** — data volume mounted at `/data/`; contains Postgres data, mori state, Tailscale identity, and SSH host keys. Survives VM stops and rebuilds (`prevent_destroy = true` in `main.tf`).
- **GCS bucket** — daily `pg_dump` backups with 14-day lifecycle policy
- **Secret Manager** — API keys and Postgres password injected at runtime; no credentials in the startup script or repository
- **IAM** — service account for Secret Manager, GCS, and IAP access

## What survives a VM rebuild

The startup script is designed so a full VM rebuild (e.g. `terraform taint` + `apply`) loses no data.

**Survives (on the persistent disk):**
- Postgres data directory — all memories, session events, message history
- mori data directory — memories.db (SQLite legacy), msg.db, `.env` containing `MORI_DATABASE_URL`
- Tailscale identity — VM retains its Tailscale address after rebuild
- SSH host keys — prevents host-key warnings after rebuild

**Does NOT survive:**
- Named container volumes — never use named volumes for stateful data on GCE; always bind-mount to `/data/`
- Any state stored only in container memory or the ephemeral boot disk

## Prerequisites

```bash
gcloud auth application-default login
gcloud config set project <your-project>
```

The GCE service account requires these IAM roles:
- `roles/secretmanager.secretAccessor` — read Postgres password and API keys at boot
- `roles/storage.objectAdmin` — write `pg_dump` backups to GCS
- `roles/iap.tunnelResourceAccessor` — SSH access via IAP (VM has no external IP)

## Deploy

```bash
cd deploy/gcp
terraform init
terraform plan
terraform apply
```

## Post-deploy

1. **Migrate secrets** — from your local machine:
   ```bash
   cd mori
   bash scripts/migrate-secrets.sh
   ```

2. **SSH in** and verify the containers are running:
   ```bash
   gcloud compute ssh mori@mori-advisor \
     --project=<your-project> \
     --zone=<your-zone> \
     --tunnel-through-iap
   podman ps
   curl http://localhost:8968/health
   curl http://localhost:8968/ready
   ```

3. **Verify Postgres and memory count**:
   ```bash
   podman exec mori-pg psql -U mori mori -c "SELECT COUNT(*) FROM memories;"
   ```

4. **Dream scheduling and backups run automatically** via cron in the startup script — no
   additional configuration required. To verify:
   ```bash
   crontab -l   # shows dream (every 4h) and pg_dump (daily 06:00 UTC) entries
   ```

## Observability

As of v2.1.13, mori exposes native Prometheus metrics at `/metrics` (`text/plain; version=0.0.4`).
Prometheus can scrape this endpoint directly without an intermediate collector.

The `ops-agent-config.yaml` in this directory is a reference configuration for teams using
Google Cloud Monitoring. It is no longer the recommended observability approach — native
`/metrics` scraping is preferred.

Key endpoints:
- `/health` — liveness probe
- `/ready` — readiness probe (returns 503 until dream pipeline initialises)
- `/metrics` — Prometheus scrape endpoint

## Upgrading from SQLite (≤v2.1.14)

If your deployment predates v2.1.15, the VM runs SQLite with Litestream backup. To migrate:

1. Export existing memories from the running container:
   ```bash
   # SSH into the VM, then:
   podman exec mori-advisor python3 -m mori_advisor.cli.export \
     --db /data/mori-advisor/memories.db \
     --output /tmp/sqlite-export.jsonl
   ```

2. Apply the updated startup script (v2.1.15+) and restart the VM. Postgres starts on
   the persistent disk; mori-advisor waits for it via `pg_isready`.

3. Import the exported memories:
   ```bash
   MORI_DATABASE_URL=$(grep MORI_DATABASE_URL /data/mori-advisor/.env | cut -d= -f2-) \
     podman exec -i mori-advisor python3 -m mori_advisor.cli.import_ /tmp/sqlite-export.jsonl
   ```

4. Verify the count matches: `SELECT COUNT(*) FROM memories;`

5. `MORI_REQUIRE_POSTGRES=true` is set by default in v2.1.15+ — the server will abort
   rather than silently fall back to SQLite if Postgres is unreachable.
