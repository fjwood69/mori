#!/bin/bash
# Mori — Google Cloud Run deploy script
#
# This runs automatically when you click "Run on Google Cloud":
#   https://deploy.cloud.run/?git_repo=https://github.com/fjwood69/mori
#
# ⚠ IMPORTANT — persistent memory requires a Postgres database.
#   Cloud Run is stateless: without MORI_DATABASE_URL pointing at a durable
#   Postgres instance, ALL memories are lost on every cold start or redeploy.
#
#   Free options (no credit card for small usage):
#     • Neon:     https://neon.tech  — free tier: 0.5 GB, instant signup
#     • Supabase: https://supabase.com — free tier: 500 MB, 2 projects
#
#   Create a project, copy the connection string (postgresql://...), and paste
#   it when prompted below.

set -euo pipefail

PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
REGION="${GOOGLE_CLOUD_REGION:-us-central1}"
SERVICE_NAME="mori"
IMAGE="ghcr.io/fjwood69/mori:latest"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║           Mori — Cloud Run deployment                ║"
echo "║  Your own server. Your own data. AGPL-3.0.           ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "Project : $PROJECT_ID"
echo "Region  : $REGION"
echo "Service : $SERVICE_NAME"
echo "Image   : $IMAGE"
echo ""

# ── Collect required config ──────────────────────────────────────────────────

echo "Enter your LLM provider API key."
echo "Providers: Novita (novita.ai), DeepInfra (deepinfra.com), OpenAI (platform.openai.com)"
read -r -p "MORI_API_KEY: " MORI_API_KEY
if [[ -z "$MORI_API_KEY" ]]; then
  echo "Error: MORI_API_KEY is required." >&2; exit 1
fi

echo ""
echo "Enter the OpenAI-compatible base URL for your provider."
read -r -p "MORI_BASE_URL [https://api.novita.ai/v3/openai]: " MORI_BASE_URL
MORI_BASE_URL="${MORI_BASE_URL:-https://api.novita.ai/v3/openai}"

echo ""
echo "Enter your local machine's hostname (run 'hostname' to find it)."
echo "This lets your Claude Code sessions write memories without manual approval."
read -r -p "MORI_TRUSTED_DREAMERS: " MORI_TRUSTED_DREAMERS

# ── Postgres — REQUIRED for durable memory ───────────────────────────────────

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  IMPORTANT: persistent memory requires Postgres"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  Cloud Run is stateless. Without a Postgres database, ALL memories"
echo "  are lost on every cold start or redeploy."
echo ""
echo "  Free options (no ongoing cost for small usage):"
echo "    • Neon     — https://neon.tech     (free tier: 0.5 GB)"
echo "    • Supabase — https://supabase.com  (free tier: 500 MB)"
echo ""
echo "  Create a project there now, then paste the connection string below."
echo "  It looks like: postgresql://user:pass@host/dbname"
echo ""
read -r -p "MORI_DATABASE_URL (leave blank to deploy WITHOUT persistence — demo only): " MORI_DATABASE_URL

if [[ -z "$MORI_DATABASE_URL" ]]; then
  echo ""
  echo "⚠  WARNING: no Postgres URL provided."
  echo "   Mori will start, but memories will be WIPED on every cold start."
  echo "   This is fine for a quick trial; not suitable for real use."
  echo ""
  read -r -p "Deploy anyway without persistence? (yes/N): " CONFIRM_EPHEMERAL
  if [[ ! "$CONFIRM_EPHEMERAL" =~ ^[Yy][Ee][Ss]$ ]]; then
    echo "Aborted. Create a free Postgres database and re-run."
    exit 1
  fi
  USE_POSTGRES=false
else
  USE_POSTGRES=true
fi

# ── Generate a server API key ────────────────────────────────────────────────

echo ""
GENERATED_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
echo "Generating API key for your Mori server..."
echo ""
echo "  ⓘ  Copy this secret — you will paste it as the API key in your plugin config."
echo "      Key pair:  cloudrun:${GENERATED_SECRET}"
echo ""
read -r -p "Press Enter once you've copied the key above..."
MORI_API_KEYS="cloudrun:${GENERATED_SECRET}"

# ── Build env vars string ────────────────────────────────────────────────────

ENV_VARS="APP_PORT=8080"
ENV_VARS+=",MORI_PROVIDER_MODE=direct"
ENV_VARS+=",MORI_API_KEY=${MORI_API_KEY}"
ENV_VARS+=",MORI_BASE_URL=${MORI_BASE_URL}"
ENV_VARS+=",MORI_TRUSTED_DREAMERS=${MORI_TRUSTED_DREAMERS:-cloudrun}"
ENV_VARS+=",MORI_API_KEYS=${MORI_API_KEYS}"
ENV_VARS+=",MORI_ADVISOR_MODEL=moonshotai/kimi-k2.6"
ENV_VARS+=",MORI_DREAM_INTERVAL=60"

if [[ "$USE_POSTGRES" == "true" ]]; then
  ENV_VARS+=",MORI_DATABASE_URL=${MORI_DATABASE_URL}"
  ENV_VARS+=",MORI_REQUIRE_POSTGRES=true"
fi

# ── Deploy ───────────────────────────────────────────────────────────────────

echo ""
echo "Deploying to Cloud Run..."

gcloud run deploy "$SERVICE_NAME" \
  --image "$IMAGE" \
  --platform managed \
  --region "$REGION" \
  --allow-unauthenticated \
  --port 8080 \
  --set-env-vars "$ENV_VARS" \
  --memory 512Mi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 1 \
  --quiet

# ── Done ─────────────────────────────────────────────────────────────────────

SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" \
  --region "$REGION" \
  --format "value(status.url)" 2>/dev/null)

echo ""
echo "✓  Mori deployed at: $SERVICE_URL"
echo ""

if curl -sf --max-time 10 "${SERVICE_URL}/health" >/dev/null 2>&1; then
  echo "✓  Health check passed."
else
  echo "   (Health check pending — Cloud Run may still be starting.)"
  echo "   Verify: curl ${SERVICE_URL}/health"
fi

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  Next: configure the plugin                          ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "  Server URL : $SERVICE_URL"
echo "  API key    : cloudrun:${GENERATED_SECRET}"
echo ""
echo "  In your plugin config:"
echo "    server_url  → $SERVICE_URL"
echo "    api_key     → cloudrun:${GENERATED_SECRET}"
echo ""

if [[ "$USE_POSTGRES" == "true" ]]; then
  echo "✓  Postgres backend configured — memories persist across cold starts."
else
  echo "⚠  Running without Postgres — memories will be lost on cold starts."
  echo "   To add persistence later:"
  echo "   1. Create a free Postgres at https://neon.tech or https://supabase.com"
  echo "   2. gcloud run services update $SERVICE_NAME --region $REGION \\"
  echo "        --set-env-vars 'MORI_DATABASE_URL=postgresql://... ,MORI_REQUIRE_POSTGRES=true'"
fi
echo ""
