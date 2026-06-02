# Mori GCE Deployment — Terraform

Deploys mori-advisor to a GCE VM in `northamerica-northeast2` (Toronto).

## Resources

- **GCE VM** — `e2-small` (2 vCPU, 2GB RAM), Ubuntu 24.04 LTS, 20GB persistent disk
- **GCS buckets** — data bucket for SQLite, backup bucket for daily snapshots
- **Secret Manager** — API keys and config injected at runtime
- **IAM** — service account for GCS + Secret Manager access

## Prerequisites

```bash
gcloud auth application-default login
gcloud config set project mori-genai
```

## Deploy

```bash
cd deploy/gcp
terraform init
terraform plan
terraform apply
```

## Post-deploy

1. **Migrate secrets** — from the NUC:
   ```bash
   cd mori
   bash scripts/migrate-secrets.sh
   ```

2. **SSH in** and verify the container is running:
   ```bash
   gcloud compute ssh mori-advisor --zone northamerica-northeast2-a
   sudo podman ps
   curl http://localhost:8968/health
   ```

3. **Start the dream scheduler** on the GCE VM:
   ```bash
   sudo systemctl enable --now mori-dream.timer
   sudo systemctl enable --now mori-backup.timer
   ```

4. **Configure Monitoring (GCP Ops Agent)**:
   Copy the Prometheus scraping configuration template to the agent:
   ```bash
   gcloud compute ssh mori-advisor --zone northamerica-northeast2-a --command "
     sudo tee /etc/google-cloud-ops-agent/config.yaml << 'EOF'
   $(cat ops-agent-config.yaml)
   EOF
     sudo systemctl restart google-cloud-ops-agent
   "
   ```

