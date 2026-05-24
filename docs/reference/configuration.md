# Configuration

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MORI_PROVIDER_MODE` | `bifrost` | `direct` or `bifrost`. New users without a custom gateway should set `direct`. |
| `MORI_API_KEY` | — | Provider key (required in `direct` mode) |
| `MORI_BASE_URL` | depends | OpenAI-compatible base URL |
| `MORI_MODEL` | `moonshotai/kimi-k2.6` | Advisor model |
| `MORI_DREAM_MODEL` | falls back | Dream pipeline model |
| `MORI_MCP_SERVER_NAME` | `mori` | MCP tool prefix |
| `MORI_ADVISOR_DATA` | `/data/mori-advisor` | SQLite DB location |
| `MORI_ADVISOR_API_KEY` | — | Event capture auth (empty = no auth) |
| `MORI_TRUSTED_DREAMERS` | — | Comma-separated hostnames for write approval bypass |
| `MORI_STANDARDS_DIR` | — | Path to team standards .md directory |
| `MORI_SKILLS_DIR` | — | Path to slash command skill files (for /update) |
| `MORI_DREAM_INTERVAL` | `60` | Dream pipeline interval in minutes |
| `MORI_BIFROST_TIMEOUT` | `300` | API timeout in seconds |

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

## Ports

| Port | Service |
|------|---------|
| `8968` | MCP server (streamable HTTP) + event capture API |
