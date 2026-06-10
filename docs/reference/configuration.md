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
| `MORI_API_KEY_ROLES` | — | Capability roles per key: `name:role,name:role,...` — roles: `read`, `write`, `dreamer`. Names absent from this list default to `read` (fail closed). Only consulted when `MORI_TD_MODE=api`. See [Capability roles](#capability-roles). |
| `MORI_TD_MODE` | `host` | Trusted-dreamer mode switch. `host` (default): existing hostname-based trust, no key-role enforcement — existing deployments unchanged. `api`: API key role is the sole authority for write/approve operations. |
| `MORI_LOCAL_FULL_ACCESS` | `false` | When `true`, a missing actor (e.g. stdio transport without an ASGI request) is treated as having dreamer access. Only set on fully-trusted single-user deployments. |
| `MORI_TRUSTED_DREAMERS` | — | Comma-separated hostnames for write approval bypass (host mode only) |
| `MORI_STANDARDS_DIR` | — | Path to team standards .md directory |
| `MORI_SKILLS_DIR` | — | Path to slash command skill files (for /update) |
| `MORI_PROMPTS_DIR` | packaged | Directory of distillation prompt files (`dreamer.txt`, `archivist.txt`). Overrides the packaged defaults — see [Distillation prompts](#distillation-prompts). |
| `MORI_DREAM_INTERVAL` | `60` | Dream pipeline interval in minutes |
| `MORI_BIFROST_TIMEOUT` | `300` | API timeout in seconds |
| `MORI_MSG_HEADLESS_ENABLED` | `false` | Spawn headless `claude` process for incoming `task` messages |
| `MORI_MSG_HEADLESS_TRUSTED` | `""` | Comma-separated hostnames allowed to trigger headless CC |
| `MORI_POST_COMPACT_BRIEF` | `true` | Set to `false` to suppress the PostCompact re-grounding prompt |
| `MORI_POST_COMPACT_WINDOW` | `6h` | Default `since` window for `/brief --post-compact` when the client supplies no marker/session boundary. Accepts `6h`/`30m`/`7d` or ISO-8601. |
| `MORI_CONSULT_CAPTURE` | `true` | Set to `false` to suppress automatic capture of `consult_advisor` responses as working-tier memories |
| `MORI_CAPTURE_THINKING` | `false` | Set to `true` to also capture the assistant's thinking blocks (not just text) when extracting reasoning from the `Stop` hook |
| `MORI_CORS_ORIGINS` | `*` | Comma-separated allowed origins for the read REST API (`GET /api/memories`, `GET /api/memories/{name}`, `GET /api/events`) — set to your dashboard origin(s) in production. Routes remain API-key gated regardless. |

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

### Web dashboard

Mori serves the web dashboard at its **root URL** (`http://<host>:<port>/`) — open it in
a browser and enter any valid key. It sends the secret as `X-Api-Key` on every `/api/*`
request, exactly like an MCP client. The dashboard is read-only, so any key with read
access works — no special role required. Because it's served same-origin, no base URL or
CORS config is needed. (The page is also available standalone at `dashboard/index.html`
for hosting elsewhere — that cross-origin case needs `MORI_CORS_ORIGINS` to permit the
dashboard's origin; default `*`.)

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

### Capability roles

Key-based capability scoping requires **both** `MORI_API_KEY_ROLES` and `MORI_TD_MODE=api`.
Setting `MORI_API_KEY_ROLES` alone has no effect — the mode switch must be explicitly opted in.

#### Roles (least to most privileged)

| Role | Allowed operations |
|------|-------------------|
| `read` | All read operations — `memory_read`, `memory_list`, `memory_search`, `brief`, `pensieve`, `GET /api/memories`, `GET /api/events`, etc. |
| `write` | Read + `memory_write`, `memory_import`, `memory_delete`, `memory_rollback`, `POST /api/memories`, `GET /api/pending` |
| `dreamer` | Write + `memory_approve`, `memory_reject`, `memory_protect`, `POST /api/memories/{name}/approve`, `POST /api/memories/{name}/reject`, `DELETE /api/memories/{name}` |

Hierarchy: `read < write < dreamer`. A dreamer key may call any operation.

#### Example configuration

```bash
# Server .env
MORI_API_KEYS=laptop:a1b2...,ci-runner:c3d4...,gce-dreamer:e5f6...
MORI_API_KEY_ROLES=laptop:write,ci-runner:read,gce-dreamer:dreamer
MORI_TD_MODE=api
```

- `laptop` — can write memories but cannot approve pending writes
- `ci-runner` — read-only (briefing, search, pensieve)
- `gce-dreamer` — full access including approve/reject/protect

#### Fail-closed defaults

- A name present in `MORI_API_KEYS` but **absent** from `MORI_API_KEY_ROLES` defaults to `read`.
  This is intentional — adding a new key without a role assignment does not accidentally grant write access.
- An **unknown role string** (not `read`, `write`, or `dreamer`) is rejected at startup with an error log
  and the key is assigned `read`.
- A **missing actor** (no ASGI request — e.g. stdio transport) is denied for privileged operations
  unless `MORI_LOCAL_FULL_ACCESS=true`.

#### Backward compatibility

In `host` mode (the default), `MORI_API_KEY_ROLES` is loaded but **not consulted** for authorisation —
existing hostname-based trusted-dreamer logic is unchanged. Switching to `api` mode is a per-deployment
opt-in that takes effect immediately on restart.

The write REST API (#14) is implemented and uses the same `require_role` check, so
"write/approve features require `api` mode" is automatic — they fail closed in `host` mode
until the operator opts in.

## Write REST API (#14)

Governed write/approve/reject/delete endpoints for non-MCP consumers (dashboard, CI, agents).
All routes require `MORI_TD_MODE=api` and an appropriate role.

### `POST /api/memories` — propose or write (role: write)

Propose-not-overwrite semantics:
- **New name** → insert as working tier immediately → `201 Created`, `{"status":"created"}`
- **Canonical or protected name** → create a pending proposal; canonical row unchanged → `202 Accepted`, `{"status":"pending"}`
- **Working name, same actor** → idempotent update → `200 OK`, `{"status":"updated"}`
- **Working name, different actor** → create a pending proposal → `202 Accepted`, `{"status":"pending"}`

Body (JSON, all fields optional except `name`):

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Required. Must match `^[a-zA-Z0-9_-]{1,128}$` |
| `title` | string | Human-readable title |
| `description` | string | One-line summary |
| `type` | string | `project`, `profile`, `pattern`, `decision`, `standard`, `requirement` |
| `tier` | string | `working` (default), `canonical`, `ephemeral` |
| `body` | string | Markdown content (max 64 KB) |
| `tags` | list[str] | Tag strings |
| `origin_clients` | list[str] | Contributing client hostnames |

Unexpected fields are rejected with 400. Oversized body → 400. Bad name → 400.

### `GET /api/pending` — list pending proposals (role: write)

Returns pending writes awaiting dreamer approval. Unapproved agent output is not for read-only eyes.

Query params: `status` (default `pending`; also `approved`, `rejected`).

### `POST /api/memories/{name}/approve` — approve pending write (role: dreamer)

Body: `{"write_id": <int>, "note": "...", "reviewer": "..."}`. `write_id` is required — fetch from `GET /api/pending`.

Race-safe: SQLite uses `BEGIN IMMEDIATE`; Postgres uses `SELECT ... FOR UPDATE` inside a transaction. Concurrent approvals cannot duplicate canonical rows.

### `POST /api/memories/{name}/reject` — reject pending write (role: dreamer)

Body: `{"write_id": <int>, "note": "...", "reviewer": "..."}`.

### `DELETE /api/memories/{name}` — hard-delete a memory (role: dreamer)

Permanently removes the memory entry. Soft-delete (`deleted_at`) is deferred to a follow-up (#16).

### Audit log

Every write/approve/reject/delete emits a structured log line at INFO level:

```
AUDIT op=<op> actor=<key_name> name=<name> content_hash=<sha256[:16]>
```

A structured audit table is deferred to a follow-up (#16).

### Deferred (#16)
- Per-key token-bucket rate-limiting + 1 MB body cap in middleware
- `Idempotency-Key` header + TTL replay cache
- Soft-delete (`deleted_at`)
- Structured audit table

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

The hook is deployed alongside other Mori hooks by `scripts/legacy/install-mori-claude.sh` /
`scripts/legacy/install-mori-claude.ps1` (legacy bespoke installers) or via the plugin (`plugins/mori/`).

## Distillation prompts

The prompts that drive memory distillation are **editable text files**, not baked into the
code — tune them without a code change or image rebuild:

| File | Used by | Schema |
|------|---------|--------|
| `dreamer.txt` | Dream pipeline (session events → memory) | `reason / confidence / path / body / evidence` |
| `archivist.txt` | Ingestion pipeline (`/ingest` corpus → memory) | `name / title / description / body / tier / tags / confidence` |

The shipped defaults live in `mori_advisor/prompts/`. To override, point `MORI_PROMPTS_DIR`
at your own directory (a missing or empty file falls back to a compact built-in prompt, logged):

```bash
# Container: bind-mount a host dir and edit prompts on the host, then restart
podman run ... \
  -e MORI_PROMPTS_DIR=/etc/mori/prompts \
  -v /srv/mori/prompts:/etc/mori/prompts:ro \
  ...
# edit /srv/mori/prompts/dreamer.txt → podman restart mori
```

Prompts are resolved **once at startup**, so changes take effect on the next restart. Do not
put the output-format instruction ("raw JSON only…") in the file — the pipeline appends that
contract last, after the dynamic focus/tier/tags lines, so it always sits in the model's
recency-most position.

## Ports

| Port | Service |
|------|---------|
| `8968` | MCP server (streamable HTTP) + event capture API |
