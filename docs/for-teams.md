# Team Configuration

Each team member runs their own AI coding agent connected to the same Mori server.
Memories are shared. Trusted dreamers approve canonical writes. Every session
starts informed rather than cold.

---

## Getting started

1. Choose a deployment posture — SQLite (solo/small team) or PostgreSQL (multi-pod, PITR backups) — see below
2. Run Mori on a shared server or cloud container using the matching Compose file
3. Generate a named API key for each team member and add them to `MORI_API_KEYS` on the server:
   ```bash
   # On the server, add to .env:
   MORI_API_KEYS=alice:$(python3 -c "import secrets; print(secrets.token_hex(32))"),bob:...
   ```
   Each member passes their key to the installer as the **API Key** prompt. Keys appear by name in server logs and the audit trail. See [Authentication](../reference/configuration.md#authentication).
4. Each member points `mcpServers` at the shared URL
5. Each member runs the installer for their platform — see [Platform guides](../README.md#platform-guides)
6. Set `MORI_TRUSTED_DREAMERS` to the hostnames of team members who can approve canonical writes
7. The dream pipeline runs on a schedule — no manual consolidation needed
8. Install the git push hook in each shared repo so pushes are visible to all instances via `/brief` — see [docs/reference/git-hooks.md](reference/git-hooks.md)

---

## Deployment posture

Mori ships two Docker Compose configurations. Choose based on your team's concurrency and ops requirements.

### Solo / small team — SQLite (`deploy/solo/`)

Default. No database server, no extra deps. SQLite with WAL mode handles concurrent reads; the dream pipeline serialises writes.

```bash
cd deploy/solo
cp .env.example .env   # fill in MORI_BIFROST_BASE_URL + API key
docker compose up -d
```

Optional: set `LITESTREAM_GCS_BUCKET` in `.env` to enable continuous replication to GCS. If unset the Litestream sidecar idles silently — the stack runs cleanly either way.

### Team / multi-pod — PostgreSQL (`deploy/team/`)

Use when you need concurrent dream runs from multiple pods, PITR backups, or Postgres-native tooling.

```bash
cd deploy/team
cp .env.example .env   # fill in POSTGRES_PASSWORD, MORI_DATABASE_URL, etc.
docker compose up -d
```

`MORI_DATABASE_URL=postgresql://mori:<pw>@pgbouncer:5433/mori` selects the Postgres backend at runtime. pgBouncer runs in session mode (`statement_cache_size=0` required for asyncpg). WAL-G archiving activates when `WALG_GS_PREFIX` is set; otherwise the sidecar idles.

**Migrating from SQLite to Postgres:**
```bash
# 1. Export
python -m mori_advisor.cli.export --db /data/mori-advisor/memories.db --output /tmp/export.jsonl

# 2. Import
MORI_DATABASE_URL=postgresql://... python -m mori_advisor.cli.import_ /tmp/export.jsonl

# 3. Verify counts, then flip MORI_DATABASE_URL in .env and restart
```

Full migration guide and rollback procedure: [docs/reference/team-configuration.md](team-configuration.md).

---

## Designing your `/brief` policy

Every Mori session starts with `/brief`. What it loads — and what it doesn't —
is a deliberate choice. The `/brief` skill definition is where operational policy
meets agent behaviour. Define it deliberately.

### The three levers

**Memory scope** — which memories load, at what depth

**Standards injection** — which team standards and compliance frameworks load

**Project filter** — which project's memories load (all, or scoped to one)

Each lever has a cost and a benefit. The configurations below show how to combine them.

### Project scoping

Without scoping, `/brief` loads all memories up to a cap — which means bifrost sessions
get mori memories they don't need, and busy projects eventually lose relevant memories
to the truncation limit.

Project scoping fixes both problems. Three commands:

| Command | Effect |
|---|---|
| `/brief` | Unscoped — all memories, up to global cap |
| `/brief --project mori` | Scoped to `mori` — right memories in full |
| `/brief --auto` | Auto-detect project from working directory |

Scoped briefs load three buckets:
1. **Project memories** — full body for canonical; full body for working ≤14 days old; summary-only for older working memories
2. **Global memories** — always loaded in full (`scope:global`, `scope:cross-project`, type `profile`/`pattern`)
3. **Other-project index** — one line per project with count, so the agent knows what exists without loading it

Output header example:
```
**Mori Brief — project: mori** (23 project + 18 global memories)
153 memories from other projects — /pensieve to explore
```

#### How memories get project-tagged

The dream pipeline auto-tags new memories from the CWD of the session that produced them.
The resolver chain, in order:
1. `.mori-project` file — place at the repo root (or any parent) with the project name as its content
2. `MORI_PROJECT` environment variable — useful in CI or non-interactive shells
3. `git rev-parse --show-toplevel` — uses the git repository root directory name

For existing memories, run the backfill script once after upgrading:

```bash
python scripts/backfill_project_tags.py /data/mori-advisor/memories.db --dry-run
python scripts/backfill_project_tags.py /data/mori-advisor/memories.db
```

Global memories (profiles, patterns, hard rules, shared conventions) should be tagged
`scope:global` — they load regardless of which project is active.

### Cost reference

| Configuration | Approx tokens | Use case |
|---------------|--------------|---------|
| Minimal (project only) | 1,000–3,000 | Solo, cost-sensitive, small context models |
| Standard (project + global) | 4,000–8,000 | Small team, general development |
| Full (project + global + standards) | 10,000–20,000 | Regulated team, compliance-sensitive |
| Onboarding (everything) | 20,000–40,000 | First session only — new joiner or new device |
| `/consult` with focus | +2,000–5,000 | Per-query standards injection on demand |

Run `/brief --dry-run` to see the actual token estimate for your configuration.

---

## Example configurations

### Minimal — cost-conscious

Load only canonical memories for the current project. Good for cost-sensitive
setups or models with small context windows.

```
/brief --project <name> --include-global false
```
```
Load: project canonical memories only
Skip: global memories, standards, cross-project index
Cost: ~1,000–3,000 tokens
```

### Standard development team

Balanced context. Project memories plus shared conventions. Cross-project index
so agents know what else exists without loading it.

```
/brief --auto
```
```
Load: project memories (canonical full, working ≤14d full, older as summary)
      + global memories (profile, patterns, coding conventions)
      + one-line index of other projects
Skip: compliance standards (available via /consult --focus on demand)
Cost: ~4,000–8,000 tokens
```

### Regulated team

Every session is compliance-aware from turn one. Security baseline, regulatory
frameworks, and firm coding standards always present.

```
/brief --auto
```
```
Load: project memories (full)
      + global memories (profile, patterns)
      + standards: security-baseline, pii-handling, api-standards (loaded via MORI_STANDARDS_DIR)
      + compliance framework (NIST SSDF, PSD2, or relevant) injected at /consult time
      + open requirements for this project
Cost: ~10,000–20,000 tokens
```

The per-session cost is justified by the risk reduction — an agent that doesn't
know your PII handling rules can produce code that violates them.

### New joiner onboarding

One-time full load for the first session on a new project or machine.

```
/brief
```
```
Load: all memories (unscoped, no cap), all standards, all open requirements,
      full cross-project awareness, recent dream state
Cost: ~20,000–40,000 tokens (run once, not every session)
```

Switch to `/brief --auto` for subsequent sessions once oriented.

---

## Standards corpus

The `MORI_STANDARDS_DIR` directory is where your team's institutional knowledge
lives. Every `.md` file is imported as a protected canonical memory on startup
and injected into `/consult` responses when focus areas match.

A well-structured standards corpus:

```
standards/
  security/
    security-baseline.md        ← OWASP top 10, firm-specific rules
    pii-handling.md             ← data classification, retention
    approved-providers.md       ← approved AI/cloud providers
  compliance/
    NIST-SSDF.md               ← ingest from PDF via /ingest
    regulatory-framework.md     ← relevant framework for your domain
  coding/
    python-style-guide.md
    api-design-standards.md
    testing-requirements.md
  architecture/
    approved-patterns.md
    forbidden-patterns.md
```

Standards are read-only to non-trusted dreamers — agent sessions cannot modify
them, only trusted dreamers can promote updates.

**Injecting standards selectively:**

```bash
# Security review — injects security standards automatically
/consult "review this handler" --focus security

# Architecture review — injects architecture standards
/consult "should we split this service?" --focus architecture

# General session — no extra standards, available on demand
/brief --project payments
```

---

## Multi-team deployment

For organisations with multiple teams on separate codebases, run separate
Mori instances per namespace:

```bash
# Payments team
docker run ... -e MORI_STANDARDS_DIR=/standards/payments -p 8970:8968

# Risk team
docker run ... -e MORI_STANDARDS_DIR=/standards/risk -p 8971:8968

# Platform team
docker run ... -e MORI_STANDARDS_DIR=/standards/platform -p 8972:8968
```

Each team gets their own memory store, standards corpus, and dream pipeline.
A shared read-only Mori instance can serve firm-wide standards that all teams
inherit via standards ingestion.

---

## Governance

**Memory ownership** — configure `MORI_TRUSTED_DREAMERS` to restrict canonical
writes to approved hostnames or service accounts. Other instances queue pending
writes for review.

**Audit trail** — every memory write tracks `origin_session_ids` and
`origin_clients`. Any memory can be traced back to the session and device that
produced it. Export via `mori-memory_export_all`.

**Retention policy** — working memories are flagged after 30 days without
retrieval. Configure the eviction queue review cadence to match your data
retention policy.

**Data classification** — the memory store is either a SQLite file
(`/data/mori-advisor/memories.db`) or a PostgreSQL database. Apply your
organisation's standard controls to the appropriate layer — file permissions
and Litestream backup for SQLite; `pg_crypto` column encryption and WAL-G
for PostgreSQL.

**Memory poisoning** — persistent memory can be corrupted by malicious or
low-quality inputs. Apply input validation at the event ingestion layer and
consider guardrails on the dream pipeline's LLM calls for high-sensitivity
deployments.

---

## Recommended dream cadence

| Team size | Recommended interval |
|-----------|---------------------|
| 1–2 | 1 hour |
| 3–5 | 30 minutes |
| 5+ | 30 minutes + manual `/dream` after significant decisions |

The `PreCompact` hook triggers an immediate dream run before any instance's
context is compressed — ensuring nothing is lost at the moment it matters most.

### Cross-device push awareness

When any team member pushes to a shared repo, other instances are notified
automatically via NATS. The next `/brief` on any device surfaces the push:

```
[nuc15pro] GitPush: mori/main abc1234 — feat: content-based ingestion
```

Install the post-push hook in each shared repo:

```bash
./scripts/install-git-hooks.sh
./scripts/install-git-hooks.sh --repo ~/bifrost
./scripts/install-git-hooks.sh --repo ~/dotfiles
```

Set `MORI_URL` in your environment (e.g. `~/.bashrc`) so the hook knows where to send events — see [docs/reference/git-hooks.md](reference/git-hooks.md).

### Inter-agent messaging

`/msg` lets agents delegate tasks, ask questions, and record decisions across the device network. The receiving agent picks them up at the next `/brief` — no shared session required.

- **`decision`** — written directly to `memory_store` by the daemon (no human session needed on the receiving end)
- **`task`** — persisted and auto-acked; surfaced at next `/brief` with a ready-made reply command
- **`question` / `broadcast`** — persisted for `/brief` pickup

The `mori-msg` daemon runs alongside `mori-advisor` as a separate container (same image, different entrypoint). It is the sole writer to `msg.db` — distinct from `memories.db` so there is no write contention with the advisor or dream pipeline.

**Headless CC** (opt-in): set `MORI_MSG_HEADLESS_ENABLED=true` and `MORI_MSG_HEADLESS_TRUSTED=<hostnames>` to spawn `claude --print <task>` automatically for incoming tasks from trusted hosts. Off by default — requires explicit opt-in per deployment.

**Governance note:** `decision` messages bypass the normal dream/approval pipeline — they are written immediately. Ensure only trusted hostnames send decision messages to shared deployments.
