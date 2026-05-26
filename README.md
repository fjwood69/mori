![Mori — A shared memory layer for AI coding agents](https://raw.githubusercontent.com/fjwood69/mori/565341369703026ecaf74377abf259223f7dcdaf/docs/assets/header-dark-v0_1_4.svg)

Mori (森) is a shared memory layer for AI coding agents — one that compounds.
Sessions feed a dream pipeline that distils activity into durable knowledge,
so every instance starts informed rather than cold. One Mori, many agents —
every session benefits from what every other session learned.

Works with any OpenAI-compatible provider. No homelab, no Anthropic
account, no LLM Gateway required — though those all work too.

---

## Multi-Instance Coherence

![One Forest, Many Agents](https://raw.githubusercontent.com/fjwood69/mori/5d55d248dc91fc7c8292c3deaf6d2a2aa40192ce/docs/assets/figure-5-one-forest.svg)

If you run AI coding agents across multiple machines, profiles, or in a team — one focused on the API layer, another on the frontend, a third on infrastructure — you already know the problem: each instance is brilliant in isolation, but none of them know what the others decided.

Instance B doesn't know that Instance A just changed the auth contract. Instance C
doesn't know that Instance B's deployment assumptions shifted. They find out the
hard way, mid-task, when something breaks.

Mori solves this. Every coding agent instance sends its session events to the shared Mori
server. The dream pipeline distils those events from **all instances** into a
unified memory store. At the start of any session, `/brief` surfaces what the
other instances have been doing. From turn one, each instance knows what the
others know.

---

## Quickstart

![Runs Anywhere](https://raw.githubusercontent.com/fjwood69/mori/74f13d4dbb321d8af6bf3ea5640566d255f33c6c/docs/assets/figure-3-runs-anywhere.svg)

### 1. Deploy

See [docs/deployment/quickstart.md](docs/deployment/quickstart.md) for all
platforms. Docker Compose is the fastest path:

```bash
git clone https://github.com/fjwood69/mori.git
cd mori
cp deploy/homelab/.env.example deploy/homelab/.env
# Edit .env with your provider API key
docker compose -f deploy/homelab/docker-compose.yml up -d
```

### 2. Verify

```bash
curl http://localhost:8968/health
# {"status":"ok","service":"mori-advisor"}
```

### 3. Connect your agent

```bash
# Claude Code — automated installer:
./scripts/install-mori-claude.sh

# Cursor — automated installer:
./scripts/install-mori-cursor.sh

# Windows:
powershell -File scripts/install-mori-claude.ps1
powershell -File scripts/install-mori-cursor.ps1
```

### Platform guides

| Platform | Installer | Full guide |
|----------|-----------|------------|
| Claude Code | `./scripts/install-mori-claude.sh` | [docs/getting-started/claude-code.md](docs/getting-started/claude-code.md) |
| Cursor | `./scripts/install-mori-cursor.sh` | [docs/getting-started/cursor.md](docs/getting-started/cursor.md) |
| Google Antigravity IDE | `./scripts/install-mori-antigravity.sh` | [docs/getting-started/antigravity.md](docs/getting-started/antigravity.md) |
| Cline | `./scripts/install-mori-cline.sh` | [docs/getting-started/cline.md](docs/getting-started/cline.md) |

---

## Capabilities

| Capability | What it does | Slash command |
|-----------|-------------|---------------|
| **Dream pipeline** | Auto-distils session events into structured memories | `/dream` |
| **Session grounding** | Loads shared context at session start — not per-query RAG | `/brief` |
| **Universal ingestion** | Feed PDFs, images, git, transcripts into the memory store | `/ingest` |
| **Strategic review** | LLM guidance with focus areas and auto-injected standards | `/consult` |
| **Requirements tracking** | Lightweight project checklist surfaced via `/brief` | `/req` |
| **Governance** | Versioning, trusted dreamers, rollback, attribution | — |
| **NATS messaging** | Real-time cross-device awareness | `/nats` |
| **Skill deployment** | Push slash commands to all devices in one step | `/update` |

Full reference: [docs/reference/slash-commands.md](docs/reference/slash-commands.md)

---

## How it works

### Dream pipeline

Session events are captured via Claude Code lifecycle hooks and distilled into
structured memories by a configurable LLM.

![Dream Pipeline](https://raw.githubusercontent.com/fjwood69/mori/5d55d248dc91fc7c8292c3deaf6d2a2aa40192ce/docs/assets/figure-1-dream-pipeline.svg)

```
Hook fires  →  POST /api/events/raw  →  SQLite events table
                                             ↓
PreCompact  →  POST /api/precompact  →  dream_run() reads since watermark
                                             ↓
                                      LLM distills events → structured memories
                                             ↓
                                      memories written to store (with attribution)
                                             ↓
                                      watermark advanced
```

The `PreCompact` hook triggers an immediate synchronous dream before context
compression — so nothing is lost at the moment it matters most.

**What it captures:** `PostToolUse`, `PostToolUseFailure`, `PreCompact`,
`UserPromptSubmit`, `Stop` — tool calls, prompts, errors, stop reasons,
session ID, hostname, working directory, transcript path.

### Memory store

![The Forest Remembers](https://raw.githubusercontent.com/fjwood69/mori/5d55d248dc91fc7c8292c3deaf6d2a2aa40192ce/docs/assets/figure-2-the-forest-remembers.svg)

Memories live in SQLite (`memories.db`) with three tiers:

| Tier | Scope | Lifecycle |
|------|-------|-----------|
| **Ephemeral** | Session summaries | Auto-expire unless explicitly saved |
| **Working** | Patterns, decisions, project context | Flagged after 30 days without retrieval |
| **Canonical** | Explicitly promoted by a trusted dreamer | Indefinite, freshness-checked via `/brief` |

Versioning, diff, rollback, attribution, and governance built in.
See [docs/reference/configuration.md](docs/reference/configuration.md).

### Universal ingestion

![Feed Anything, Remember Everything](https://raw.githubusercontent.com/fjwood69/mori/4f7e3a984527d91bcc23db431bfae8089ce20006/docs/assets/figure-6-feed-anything.svg)

New team members start cold. `/ingest` bootstraps the memory store from
existing source material — applying the same distillation pipeline that
powers the dream phase.

```bash
# Preview (zero cost, no LLM):
/ingest --source ~/my-project --preview

# Dry-run to validate extraction quality:
/ingest --source ~/my-project --dry-run --focus decisions

# Commit:
/ingest --source ~/my-project --focus all --tier working
```

**Supported:** PDF, images/whiteboards (Kimi K2.6 vision), CC transcripts
(`.jsonl`), git history (`--since 30d`), text and code.

**Works with remote servers:** `/ingest` reads files on the client device and
sends content over the wire — no shared filesystem needed. Works whether
mori-advisor is running locally or on GCE.

**Cost guard:** `--max-cost` (default $5.00) aborts before spending. Preview
is always free. SHA256 dedup prevents re-ingesting the same content.

### Architecture

![Mori Architecture](https://raw.githubusercontent.com/fjwood69/mori/5d55d248dc91fc7c8292c3deaf6d2a2aa40192ce/docs/assets/figure-4-architecture.svg)

---

## Configuration

**Configuration reference** → [docs/reference/configuration.md](docs/reference/configuration.md)
**Recommended models** → [docs/reference/models.md](docs/reference/models.md)
**For teams** → [docs/for-teams.md](docs/for-teams.md)

Key environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `MORI_PROVIDER_MODE` | `bifrost` | `direct` or `bifrost` |
| `MORI_API_KEY` | — | Provider key (required in `direct` mode) |
| `MORI_BASE_URL` | — | OpenAI-compatible base URL |
| `MORI_MODEL` | `moonshotai/kimi-k2.6` | Advisor + consult model |
| `MORI_DREAM_MODEL` | falls back to `MORI_MODEL` | Dream + ingest distillation model |
| `MORI_FAST_MODEL` | `deepseek/deepseek-v4-flash` | Contradiction scan + freshness checks |
| `MORI_TRUSTED_DREAMERS` | — | Comma-separated trusted hostnames |
| `MORI_DREAM_INTERVAL` | `60` | Dream cron interval (minutes) |
| `MORI_STANDARDS_DIR` | — | Path to team standards `.md` files |

---

## Building

```bash
git clone https://github.com/fjwood69/mori.git
cd mori
podman build -t localhost/mori-advisor:latest .
# Or: docker build -t mori-advisor:latest .
```

## License

MIT

---

[![Support me on Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/moriapp)
