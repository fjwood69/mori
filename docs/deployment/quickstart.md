# Deployment

## Deployment matrix

| Platform | Recommended path | Complexity |
|----------|-----------------|------------|
| **Cloud — one-click** | Render / Railway / Cloud Run / Fly.io | Low — see [one-click-deploy.md](../getting-started/one-click-deploy.md) |
| Linux | Docker Compose or Podman Compose | Low |
| macOS | Docker Desktop or native Python | Low |
| Windows | Docker Desktop | Low |
| Windows (advanced) | WSL2 + Podman Compose | Medium |
| Cloud (GCP, full) | GCP Terraform ([deploy/gcp/](../deploy/gcp/)) | Medium |

## Docker Compose (all platforms — recommended)

The compose file in `deploy/homelab/docker-compose.yml` brings up Mori with a
dream-cron sidecar. Works with Docker Desktop (macOS/Windows), Podman Compose
(Linux), and `docker compose`.

```bash
git clone https://github.com/fjwood69/mori.git
cd mori
cp deploy/homelab/.env.example deploy/homelab/.env
# Edit deploy/homelab/.env with your provider API key and model
docker compose -f deploy/homelab/docker-compose.yml up -d
curl http://localhost:8968/health
```

### Linux — Docker Compose

Works with Podman Compose (`podman compose`) or Docker Compose (`docker compose`).

```bash
git clone https://github.com/fjwood69/mori.git
cd mori
cp deploy/homelab/.env.example deploy/homelab/.env
# Edit deploy/homelab/.env with your provider API key and model
docker compose -f deploy/homelab/docker-compose.yml up -d
curl http://localhost:8968/health
```

The compose file brings up Mori with a dream-cron sidecar that runs the dream
pipeline on a schedule. Configure `MORI_DREAM_INTERVAL` in `.env` (default: 60 minutes).

### macOS — Docker Desktop

Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) then:

```bash
git clone https://github.com/fjwood69/mori.git
cd mori
cp deploy/homelab/.env.example deploy/homelab/.env
# Edit deploy/homelab/.env with your provider API key and model
docker compose -f deploy/homelab/docker-compose.yml up -d
curl http://localhost:8968/health
```

Docker Desktop handles the Linux container layer transparently — no extra setup needed.

### macOS — native Python

```bash
git clone https://github.com/fjwood69/mori.git
cd mori
pip install -r requirements.txt
cp deploy/homelab/.env.example deploy/homelab/.env
# Edit deploy/homelab/.env with your provider API key
set -a; source deploy/homelab/.env; set +a
python -m mori_advisor.main &

# Dream cron: add to crontab (runs every hour — adjust to match MORI_DREAM_INTERVAL)
# 0 * * * * cd /path/to/mori && python -m mori_advisor.dream_job
```

SQLite WAL mode works natively on macOS. No container needed.

### Windows — Docker Desktop

Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) (handles WSL2 backend automatically) then in PowerShell:

```powershell
git clone https://github.com/fjwood69/mori.git
cd mori
copy deploy\homelab\.env.example deploy\homelab\.env
# Edit deploy\homelab\.env with your provider API key and model (Notepad works fine)
docker compose -f deploy\homelab\docker-compose.yml up -d
curl http://localhost:8968/health
```

No WSL knowledge required. Docker Desktop runs the Linux container transparently.
Dream cron is handled inside the container — no Windows Task Scheduler needed.

### Windows — WSL2 + Podman

Follow the [Linux path](#linux--docker-compose) inside WSL2 Ubuntu. Docker
Desktop is the easier path for most users.

### Cloud — GCP Terraform

See [deploy/gcp/](../deploy/gcp/) for Terraform configs. Creates a GCE e2-small VM
with Podman rootless, persistent disk, Tailscale, and GCP Secret Manager.

```bash
cd deploy/gcp
terraform init
terraform plan
terraform apply
```

## Homelab (Podman raw, Linux advanced)

Systemd user services for the dream timer and backup timer are in [deploy/homelab/](../deploy/homelab/):

```bash
git clone https://github.com/fjwood69/mori.git
cd mori
podman build -t localhost/mori-advisor:latest .
podman run -d --name mori --restart=unless-stopped --network=host \
  -v /data/mori-advisor:/data/mori-advisor:Z \
  --env-file deploy/homelab/.env \
  localhost/mori-advisor:latest

# Install systemd timers (user-level)
cp deploy/homelab/mori-dream.*   ~/.config/systemd/user/
cp deploy/homelab/mori-backup.*  ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mori-dream.timer
systemctl --user enable --now mori-backup.timer
```

## GCP (GCE VM)

See [deploy/gcp/](../deploy/gcp/) for Terraform configs. Creates:

- GCE e2-small VM (2 vCPU, 2GB RAM, 20GB persistent disk) — ~$12/month
- Ubuntu 24.04 LTS with Podman rootless
- Persistent disk for Postgres data, mori state, Tailscale identity — survives VM rebuilds
- GCS bucket for daily `pg_dump` backups (14-day lifecycle)
- GCP Secret Manager for all secrets — no credentials in the startup script
- Tailscale join for access (no public ports)
- Dream scheduling and pg_dump backup via cron in the startup script

```bash
cd deploy/gcp
terraform init
terraform apply
# Populate secrets in GCP Secret Manager (see deploy/gcp/README.md for the full list),
# or run scripts/migrate-secrets.sh if migrating from an existing instance.
# SSH in and verify (VM has no external IP — IAP tunneling is required):
gcloud compute ssh mori@mori-advisor --project=<your-project> --zone=<your-zone> --tunnel-through-iap
curl http://localhost:8968/health
curl http://localhost:8968/ready
```

## Dual deployment (migration period)

During migration, both homelab and GCP instances can run in parallel pointing
at separate databases. Claude Code points at either one via `.mcp.json`.

To copy memories from an existing instance (both tools need a `dreamer` key in api mode):
1. On the old instance: `mori-memory_export_all` → flat `.md` files in its `<data dir>/exports/`
2. Copy those files into the new instance's `<data dir>/imports/`
3. On the new instance: `mori-memory_import` → loads them into the new DB. Each write is
   attributed to your key, and a `tier:` in the frontmatter is a request judged by the tier
   rules, not an authority.
4. Verify with `mori-memory_list`

Both directories are fixed (v2.3.12): the tools no longer take a server-side path.

No downtime — both instances serve during the cutover.

### Moving from SQLite to Postgres: what each path carries

**Supported: export and import (above).** Every memory is written through mori's write path, so
it gets fresh ids, a provenance-tagged audit row and correct encoding. It carries memory
**content and tier only**. Version history, the eviction/review queue, the audit log, session
events and dream state stay behind and start empty on the new database.

**Not supported: copying tables row by row with their ids** (for example a script or `pgloader`
run against `memories.db`). If you do it anyway:

1. **Sequences.** Copied ids do not advance Postgres sequences, so the next insert into that table
   reuses an id and fails. From v2.3.11 mori raises every column-owned sequence above its column's
   maximum on each start (logged as `SEQUENCE-REPAIR`, counted in `mori_sequence_repairs_total`) —
   start mori once against the new database before using it. Before v2.3.11 this failed silently:
   supersession writes rolled back on every attempt.
2. **Version history.** Copied `memory_versions` rows have no `memory_id`; migration 17 fills it
   only where the memory's name has a single incarnation. Others stay keyed by name.
3. **JSON columns.** SQLite stores tags and origin arrays as text; they must arrive as JSON arrays
   (`jsonb_typeof = 'array'`). Since v2.3.10 a CHECK constraint rejects anything else.
4. **Run migrations by starting mori** against the target database; do not create the schema by
   hand.

## Observability endpoints

| Endpoint | Purpose | Response |
|----------|---------|----------|
| `/health` | Liveness probe | 200 if process is alive |
| `/ready` | Readiness probe (HTTP endpoint) | 200 if DB accessible, 503 otherwise |
| `/metrics` | Prometheus exposition format | Counts for memories, events, pending writes, eviction queue |
| `/api/events/health` | Legacy event endpoint | Event count |

## Verify it's running

```bash
curl http://localhost:8968/health
# {"status":"ok","service":"mori-advisor"}

curl http://localhost:8968/api/events/health
# {"status":"ok","total_events":0}

curl http://localhost:8968/metrics
# Prometheus-formatted metrics
```

## Git push hooks (optional but recommended)

Install the post-push hook into each repo to automatically ingest commit messages into Mori's memory store and publish push events to the NATS bus:

```bash
# From the mori repo root:
./scripts/install-git-hooks.sh                    # this repo
./scripts/install-git-hooks.sh --repo ~/path/to/your-other-repo
```

Add your API key to `~/.claude/.secrets` (the hook reads it automatically at push time):

```
MORI_API_KEY_<HOSTNAME_UPPER>=your-key
```

See [docs/reference/git-hooks.md](../reference/git-hooks.md) for full setup, verification steps, and Windows instructions.