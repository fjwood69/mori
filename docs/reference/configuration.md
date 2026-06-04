# Configuration

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MORI_PROVIDER_MODE` | `bifrost` | `direct` or `bifrost`. New users without a custom gateway should set `direct`. |
| `MORI_API_KEY` | — | Provider key (required in `direct` mode) |
| `MORI_BASE_URL` | depends | OpenAI-compatible base URL |
| `MORI_ADVISOR_MODEL` | `moonshotai/kimi-k2.6` | Advisor VK model (bifrost mode) |
| `MORI_DREAM_MODEL` | `moonshotai/kimi-k2.6` | Dream VK model (bifrost mode) |
| `MORI_FAST_MODEL` | `Novita/deepseek/deepseek-v4-flash` | Fast VK model — contradiction scans, cheap tasks (bifrost mode) |
| `MORI_BIFROST_ADVISOR_VK` | `mori-advisor-local` | Bifrost virtual key name for advisor calls |
| `MORI_BIFROST_DREAM_VK` | `mori-dream-local` | Bifrost virtual key name for dream calls |
| `MORI_BIFROST_FAST_VK` | `mori-fast-local` | Bifrost virtual key name for fast calls |
| `MORI_MCP_SERVER_NAME` | `mori` | MCP tool prefix |
| `MORI_ADVISOR_DATA` | `/data/mori-advisor` | Data directory (memories.db, msg.db, etc.) |
| `MORI_DATABASE_URL` | — | PostgreSQL connection URL (`postgresql://user:pass@host/db`). If unset, SQLite is used. |
| `MORI_REQUIRE_POSTGRES` | — | If `true`, abort on startup when Postgres is unreachable. Prevents silent SQLite fallback in team/GCP deployments. |
| `APP_PORT` | `8968` | Override the server listen port. Useful for side-by-side UAT instances. |
| `MORI_NATS_URL` | — | NATS JetStream URL for cross-device messaging and `mori-msg` daemon (`nats://user:pass@host:4222`). |
| `MORI_API_KEYS` | — | Named API keys: `name:secret,name:secret,...` — see [Authentication](#authentication) |
| `MORI_ADVISOR_API_KEY` | — | Legacy single key (backward compat — prefer `MORI_API_KEYS`) |
| `MORI_TRUSTED_DREAMERS` | — | Comma-separated hostnames for write approval bypass |
| `MORI_STANDARDS_DIR` | — | Path to team standards .md directory |
| `MORI_SKILLS_DIR` | — | Path to slash command skill files (for /update) |
| `MORI_DREAM_INTERVAL` | `60` | Dream pipeline interval in minutes |
| `MORI_BIFROST_TIMEOUT` | `300` | API timeout in seconds |
| `MORI_MSG_HEADLESS_ENABLED` | `false` | Spawn headless `claude` process for incoming `task` messages |
| `MORI_MSG_HEADLESS_TRUSTED` | `""` | Comma-separated hostnames allowed to trigger headless CC |
| `MORI_POST_COMPACT_BRIEF` | `true` | Set to `false` to suppress the PostCompact re-grounding prompt |
| `MORI_POST_COMPACT_WINDOW` | `6h` | Default `since` window for `/brief --post-compact` when the client supplies no marker/session boundary. Accepts `6h`/`30m`/`7d` or ISO-8601. |
| `MORI_CONSULT_CAPTURE` | `true` | Set to `false` to suppress automatic capture of `consult_advisor` responses as working-tier memories |
| `MORI_CAPTURE_THINKING` | `false` | Set to `true` to also capture the assistant's thinking blocks (not just text) when extracting reasoning from the `Stop` hook |

## Authentication

Mori uses named API keys — one per client. Keys are validated at the transport
layer before any MCP tool or HTTP endpoint is reached.

### Setting keys

```bash
# In your server .env
MORI_API_KEYS=laptop:a1b2...,workstation:c3d4...,ci-runner:e5f6...
```

Each entry is `name:secret` where:
- `name` is a human-readable label used in logs and audit trail
- `secret` is a 32-byte hex string (64 chars)

Generate a secret:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Or use the MCP tool (requires an existing valid key to call):
```
mori-key_generate name="newclient"
```

The output line goes into `MORI_API_KEYS` on the server. The secret goes into
the client's MCP config as `X-Api-Key`.

### Open mode

If neither `MORI_API_KEYS` nor `MORI_ADVISOR_API_KEY` is set, the server starts
in **open mode** — any client on the network can connect. A warning is logged at
startup. Open mode is acceptable on a private Tailscale-only network; always set
keys for team or internet-accessible deployments.

### Open paths

The following endpoints are always accessible without a key (standard probe convention):

| Path | Purpose |
|------|---------|
| `/health` | Liveness probe |
| `/ready` | Readiness probe |
| `/metrics` | Prometheus scrape |

### Backward compatibility

If `MORI_ADVISOR_API_KEY` is set and `MORI_API_KEYS` is not, the single key is
loaded as `{"legacy": <key>}`. Existing deployments continue working without
config changes. Migrate to named keys when adding more than one client.

### Migration from single key

1. Generate a named key for each client: `mori-key_generate name="myhost"`
2. Add all keys to server `.env`: `MORI_API_KEYS=myhost:<key>,...`
3. Update each client's MCP config with its key
4. Restart server
5. Remove `MORI_ADVISOR_API_KEY` from `.env`

---

## PostgreSQL

Set `MORI_DATABASE_URL` to switch from SQLite to PostgreSQL:

```bash
MORI_DATABASE_URL=postgresql://mori:yourpassword@localhost:5432/mori
```

In team and GCP deployments, also set:

```bash
MORI_REQUIRE_POSTGRES=true   # abort instead of silently falling back to SQLite
```

SQLite remains the default — setting `MORI_DATABASE_URL` is the only change needed to activate
the PostgreSQL backend. The store factory (`get_store()`) selects the backend at startup; no
other code changes are required.

See [docs/reference/team-configuration.md](team-configuration.md) for replication, backup, pgBouncer
configuration, and the SQLite → PostgreSQL migration guide.

---

## Dream interval

How often to run the dream phase depends on session density. The `PreCompact`
hook fires on context compression regardless of schedule, so the cron is just
a safety net for sessions that never compact.

Set via `MORI_DREAM_INTERVAL` in your `.env` file (used by the Docker Compose
dream-cron sidecar). For Podman/systemd deployments, set via the dream timer.

| Team size | Suggested interval | Rationale |
|-----------|-------------------|-----------|
| Solo | 240 (4 hours) | Few events per session, low risk of losing context |
| 1–4 people | 60 (1 hour) | More events, catches cold restarts and short sessions |
| 5–10 people | 30 minutes | High event density, any session could be the last before the server goes down |

## PostCompact hook

The `mori-post-compact-brief.sh` hook fires after every context compression. It
outputs a `systemMessage` prompting the agent to run `/brief`, re-establishing
session context (NATS messages, pending mori-msg items, state from before compaction).

Enabled by default. Disable with:

```bash
export MORI_POST_COMPACT_BRIEF=false
```

The hook is deployed alongside other Mori hooks by `install-mori-claude.sh` /
`install-mori-claude.ps1`.

## Ports

| Port | Service |
|------|---------|
| `8968` | MCP server (streamable HTTP) + event capture API |
