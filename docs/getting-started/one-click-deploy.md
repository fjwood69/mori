# Deploy your Mori server

Mori is a self-hosted server you own. This guide walks through four cloud paths —
pick the one that suits you. All give you an HTTPS URL you paste into the plugin.

> **This is your server, in your cloud account.** Mori never runs a shared instance.
> Your memories live on your own infrastructure. The AGPL licence applies;
> the code is yours to inspect, fork, or self-host anywhere.

> **Testing status:** `render.yaml`, `railway.json`, `fly.toml`, and
> `.cloudshell/deploy.sh` were built from current platform documentation and
> validated for structure/syntax, but **not live-deployed** from this repo.
> The container (`ghcr.io/fjwood69/mori:latest`) is verified healthy on its own.
> If you hit a platform-specific snag, open an issue — the first deploy per
> platform will be smoke-tested and this note removed.

---

## Understanding persistence

Mori stores memories in a database. **Without durable storage, every restart wipes
your memory.** This is the key question for each cloud platform:

| Backend | Set-up | Persistence |
|---------|--------|-------------|
| **Postgres** (`MORI_DATABASE_URL`) | External managed DB | ✅ Survives restarts, redeploys, scale-to-zero |
| **SQLite** (default, no `MORI_DATABASE_URL`) | File at `/data/mori-advisor/memories.db` | ✅ Only if the volume persists — otherwise ❌ |

Stateless platforms (Cloud Run, Render free tier, Railway without a volume) have
no persistent filesystem. **SQLite on a stateless platform = data loss.** The
fix is always the same: attach a Postgres database via `MORI_DATABASE_URL`.

---

## What you need first

Before you click any button you need:

| Thing | Where to get it |
|---|---|
| **LLM provider API key** | [Novita](https://novita.ai) · [DeepInfra](https://deepinfra.com) · [OpenAI](https://platform.openai.com) · any OpenAI-compatible endpoint |
| **Provider base URL** | Listed in your provider's docs (e.g. `https://api.novita.ai/v3/openai`) |
| **Your machine hostname** | Run `hostname` in a terminal — this becomes `MORI_TRUSTED_DREAMERS` |

---

## Platforms at a glance

| Platform | Storage | Persistence | ~Cost/mo | Notes |
|----------|---------|-------------|----------|-------|
| **Fly.io** | Persistent volume | ✅ SQLite on volume | ~$3–5 | Free until recently; now billed. Volume + SQLite, no Postgres needed. |
| **Any platform + Neon/Supabase** | Free managed Postgres | ✅ Postgres | **$0** | Recommended free durable path |
| **Render** | Persistent disk in `render.yaml` | ✅ SQLite on disk | ~$7 (Starter + disk) | Simplest click; paid |
| **Railway** | Postgres plugin (manual step) | ✅ with Postgres | ~$5 Hobby | Needs Postgres after deploy |
| **Cloud Run** | Postgres required | ✅ with Postgres | Pay-per-request | Script prompts for Postgres URL |

---

## Recommended: free Postgres via Neon or Supabase

The cleanest free+persistent path works with **any** stateless cloud platform:

1. **Create a free Postgres database** (takes 2 minutes, no credit card):
   - [Neon](https://neon.tech) — free tier: 0.5 GB, instant signup, serverless Postgres
   - [Supabase](https://supabase.com) — free tier: 500 MB, 2 projects

2. **Copy the connection string** — it looks like:
   ```
   postgresql://user:password@host/dbname
   ```
   Neon calls it the "Connection string"; Supabase calls it the "URI" in Database Settings.

3. **Set it as `MORI_DATABASE_URL`** when deploying to any platform below.
   Mori detects Postgres automatically and uses it instead of SQLite.

4. **No volume needed** — Postgres is external, so you get full persistence
   even on scale-to-zero platforms with no persistent filesystem.

This sidesteps every platform's disk cost entirely. Use it with Railway, Cloud Run,
or Render's free tier.

---

## Fly.io — free+persistent with SQLite volume

Fly.io with a persistent volume gives durable SQLite without a separate Postgres
database. No deploy button; five CLI commands.

### Prerequisites

```bash
# Install flyctl: https://fly.io/docs/hands-on/install-flyctl/
brew install flyctl   # macOS
# or: curl -L https://fly.io/install.sh | sh

fly auth login
```

### Deploy

```bash
# 1. Clone the repo (fly.toml is at the root)
git clone https://github.com/fjwood69/mori.git
cd mori

# 2. Create an app (pick a unique name — becomes your URL)
fly apps create mori-<yourname>

# 3. Create a persistent volume for the memory store
fly volumes create mori_data --size 1 --region iad

# 4. Set secrets (never stored in fly.toml)
fly secrets set \
  MORI_API_KEY="sk-your-provider-key" \
  MORI_BASE_URL="https://api.novita.ai/v3/openai" \
  MORI_TRUSTED_DREAMERS="$(hostname)" \
  "MORI_API_KEYS=myname:$(python3 -c 'import secrets; print(secrets.token_hex(32))')"

# 5. Deploy (edit fly.toml first — change app = "mori" to your app name from step 2)
fly deploy
```

Edit `fly.toml` to set `app = "<your-app-name>"` and `primary_region` to the
[region closest to you](https://fly.io/docs/reference/regions/).

**Persistence:** the `[[mounts]]` block in `fly.toml` attaches the volume to
`/data/mori-advisor`. SQLite data survives restarts and redeployments.

**Your server URL:** `https://<app-name>.fly.dev`

**Or use free Postgres instead of a volume** — skip step 3 and add to step 4:
```bash
MORI_DATABASE_URL="postgresql://..."   # your Neon or Supabase URL
```
Then remove the `[[mounts]]` block from `fly.toml`.

---

## Render

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/fjwood69/mori)

**Cost: ~$7/month** (Render Starter plan $7 + $0.25/GB disk). The persistent disk
that makes memory durable is only available on paid plans. There is no free persistent
path on Render without using an external Postgres database.

**What happens:**

1. Render reads `render.yaml` and shows a form for the `sync: false` env vars.
2. Fill in:
   - `MORI_API_KEY` — your LLM provider key
   - `MORI_BASE_URL` — change the default if needed (current: Novita)
   - `MORI_TRUSTED_DREAMERS` — your machine's hostname (`hostname`)
   - `MORI_API_KEYS` — a `name:secret` pair:
     ```bash
     python3 -c "import secrets; print(secrets.token_hex(32))"
     # then set: myname:<that-secret>
     ```
3. Click **Apply**. Render builds and deploys.

**For $0 instead:** use Render's free web service tier + set `MORI_DATABASE_URL`
to a free Neon or Supabase Postgres URL. Remove the `disk:` block from `render.yaml`
(or override `MORI_DATABASE_URL` in the env vars form). Free tier web services
sleep after 15 minutes of inactivity — first request after sleep has a cold-start
delay.

---

## Railway

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/new/template?template=https://github.com/fjwood69/mori)

**Cost: ~$5/month** (Railway Hobby plan).

**⚠ Railway requires a manual persistence step — do not skip it.**
The `railway.json` in this repo tells Railway how to build and run Mori, but
Railway does not configure persistent storage from a config file. Without
completing step 3 below, memories are lost on every redeploy.

**Steps:**

1. Click the button — Railway detects the `Dockerfile` and `railway.json` and deploys.
2. Set environment variables in the Railway dashboard → **Variables**:
   ```
   MORI_PROVIDER_MODE=direct
   MORI_API_KEY=<your-provider-key>
   MORI_BASE_URL=https://api.novita.ai/v3/openai
   MORI_ADVISOR_MODEL=moonshotai/kimi-k2.6
   MORI_TRUSTED_DREAMERS=<hostname>
   MORI_API_KEYS=myname:<generated-secret>
   MORI_DREAM_INTERVAL=60
   ```
   Railway injects `PORT` automatically — do **not** set `APP_PORT`.

3. **Add Postgres (required for durable memory)** — two options:

   **Option A — Railway managed Postgres (simplest):**
   - In your project: **New** → **Database** → **Add PostgreSQL**
   - Once created, add this variable to your Mori service:
     ```
     MORI_DATABASE_URL=${{Postgres.DATABASE_URL}}
     ```
   Railway wires `${{Postgres.DATABASE_URL}}` automatically.

   **Option B — External free Postgres (Neon or Supabase):**
   - Create a free database at [neon.tech](https://neon.tech) or [supabase.com](https://supabase.com)
   - Set `MORI_DATABASE_URL=<connection-string>` in Railway variables

4. Redeploy after setting `MORI_DATABASE_URL` — Mori switches to Postgres on start.

---

## Google Cloud Run

[![Run on Google Cloud](https://deploy.cloud.run/button.svg)](https://deploy.cloud.run/?git_repo=https://github.com/fjwood69/mori)

**Cost: pay-per-request** (effectively $0 when idle; small charges on use).

**⚠ Requires Postgres for durable memory.** Cloud Run is stateless — SQLite data is
lost on every cold start. The deploy script below will prompt you for a Postgres URL
and refuse to proceed without one unless you explicitly confirm a demo/ephemeral
deploy.

Clicking the button opens Google Cloud Shell and runs `.cloudshell/deploy.sh` from
this repo. It will:

1. Prompt for `MORI_API_KEY`, `MORI_BASE_URL`, and `MORI_TRUSTED_DREAMERS`.
2. Prompt for `MORI_DATABASE_URL` — paste your Neon or Supabase connection string here.
   If you skip it, the script warns loudly and asks you to confirm before proceeding.
3. Generate and display a `MORI_API_KEYS` pair.
4. Deploy `ghcr.io/fjwood69/mori:latest` and print the service URL.

**Get a free Postgres database first:**
- [Neon](https://neon.tech) — free tier, instant signup
- [Supabase](https://supabase.com) — free tier, 500 MB

**After deploy, verify:**
```bash
curl https://<your-service>.run.app/health
# → {"status":"ok","service":"mori-advisor"}
```

---

## After deploy — connect the plugin

Once your server is running:

1. **Copy the server URL** (e.g. `https://mori-myname.onrender.com`).
2. **Copy the API key** — the `name:secret` pair you set as `MORI_API_KEYS`.
3. **Configure the plugin:**
   - Claude Code: on next session start you'll be prompted for *server URL* and *API key*.
   - Or set them in your plugin config manually:
     ```
     server_url = https://your-server-url
     api_key    = myname:the-secret-you-generated
     ```
4. **Verify:** start a Claude Code session — the health sentinel should go green and
   `/brief` should return memory counts.

---

## Changing the LLM provider

`MORI_API_KEY` and `MORI_BASE_URL` accept any OpenAI-compatible endpoint.

| Provider | Base URL |
|----------|----------|
| [Novita](https://novita.ai) | `https://api.novita.ai/v3/openai` |
| [DeepInfra](https://deepinfra.com) | `https://api.deepinfra.com/v1/openai` |
| [OpenAI](https://platform.openai.com) | `https://api.openai.com/v1` |
| [Together AI](https://www.together.ai) | `https://api.together.xyz/v1` |

Set `MORI_ADVISOR_MODEL` to the model name your provider uses, e.g. `moonshotai/kimi-k2.6` on Novita.

---

## Troubleshooting

**Memories disappear after redeploy / restart** — you're on a stateless platform
without Postgres. Add `MORI_DATABASE_URL` pointing at a Neon or Supabase database.
This is the most common issue and the fix is always the same.

**Health check fails immediately after deploy** — Cloud Run and Render have a cold-start
delay. Wait 30 seconds and retry: `curl https://your-url/health`.

**`/brief` returns "server unreachable"** — check that the URL in your plugin config
matches the deployed service URL exactly (including `https://`, no trailing slash).

**Railway — `${{Postgres.DATABASE_URL}}` not resolving** — make sure the Postgres
database service is in the same Railway project as the Mori service, and that you've
triggered a redeploy after adding the variable.

**Port mismatch** — Mori reads `APP_PORT`, then `PORT`, then defaults to 8968.
Render, Railway, and Cloud Run inject `PORT` automatically — do not set `APP_PORT`
on those platforms. Fly.io requires `APP_PORT = "8080"` in `fly.toml` (already set).
