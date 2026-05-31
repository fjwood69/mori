# Mori Slash Commands

Eight slash commands — `/brief`, `/wrap`, `/dream`, `/consult`, `/pensieve`, `/req`, `/nats`, `/update` — that wire Mori's MCP tools into Claude Code's workflow. Each is a thin SKILL.md that delegates to a deterministic MCP tool on the Mori server.

---

## `/brief` — Session Bootstrap

Loads shared memories and team standards into context at the start of every session. Also runs per-device bootstrap checks (dotfiles, hostname, caveats).

**MCP tool:** `mori-brief`

**What it returns:**
- Shared memory count (e.g. "45 memories loaded")
- Team standards count with category breakdown (e.g. "5 standards loaded — coding=2, security=2, ethos=1")
- Dream pipeline state (watermark, backlog)

**Usage:** Runs automatically at session start. `/brief` to re-run.

**Project-scoped loading:**

| Command | Effect |
|---|---|
| `/brief` | Unscoped — all memories, up to limit |
| `/brief --project mori` | Scoped to `mori` — full body for project memories, global memories always included, lightweight index of other projects |
| `/brief --auto` | Auto-detect project from git working directory (checks `.mori-project` file → `MORI_PROJECT` env → `git rev-parse --show-toplevel`) |

Scoped briefs load full memory bodies for the target project rather than truncating at the global cap — the right memories in full, not a truncated slice of everything.

**Design rationale — session grounding instead of RAG:** All context is loaded up front (one LLM cost), not retrieved per-query. Works because the corpus is small (<50 documents). Beyond that, scale via namespace-separated Mori instances rather than adding a vector database.

---

## `/wrap` — Session Wrap

Session-closing counterpart to your session bootstrap skill (e.g. `/ready`). Summarises the session and publishes to cc-share and NATS so the next session (on any device) starts with context.

**MCP tools:** cc-share (`POST /cc-share/`), `mori-nats_pub`, `mori-dream_run`

**What it does:**
1. Identifies the user and host
2. Writes a concise session summary (key changes, pending, gotchas) to cc-share with a 7-day TTL
3. Publishes a one-liner to NATS
4. Runs `/dream` to flush any remaining undreamed events

Usage: `/wrap` at the end of a session before closing.

---

## `/dream` — Memory Distillation

Reads recent session events (tool calls, prompts, responses) since the last watermark, sends them to a configurable dream model, and writes extracted memories back to the store. Runs in-container — no host filesystem access needed.

**MCP tool:** `mori-dream_run`

**Variants:**

| Command | Effect |
|---|---|
| `/dream` | Run the dream pipeline, write memories |
| `/dream --dry-run` | Preview what would be written |
| `/dream --status` | Show watermark, event count, undreamed backlog |

**Scheduled execution:** Runs via cron on the server every 30 minutes. Also triggered synchronously by the `PreCompact` hook before context compression.

**Dream model:** Configurable via `MORI_DREAM_MODEL` (falls back to `MORI_MODEL`). Prefer models with strong structured JSON output and rationalisation capabilities.

---

## `/consult` — Strategic Advisor

Sends your question plus optional file context to a configurable advisor model. When a specific focus is given (`--focus security`), relevant team standards are automatically pulled from the memory store and injected into the prompt — so the advisor checks your code against your own baseline, not generic advice.

**MCP tool:** `mori-consult_advisor`

**Parameters:**

| Parameter | Values | Default |
|---|---|---|
| `--focus` | general, architecture, security, performance, style | general |
| `--depth` | quick, balanced, deep | balanced |
| `--file` / `-f` | File path(s) for code context (repeatable) | — |

**Examples:**

```
/consult "Should I use SQLite or JSONL for this?"
/consult "Review this auth flow" --focus security --depth deep
/consult "What about this?" --file src/main.py
/consult "Review against our baselines" --focus security --file snyk-report.json
```

The last example chains existing tooling (Snyk, linters, SAST scanners) into the advisory flow — CC runs the scan, then feeds the results to the advisor alongside team standards.

---

## `/pensieve` — Memory Search

Search the shared memory store by keyword, type, tag, device, or time window.

**MCP tool:** `mori-memory_search`

**Examples:**

| Input | Effect |
|---|---|
| `/pensieve` | Last 10 memories |
| `/pensieve bifrost` | Search for "bifrost" |
| `/pensieve read infra-gotchas` | Show full entry |
| `/pensieve --type decision` | Filter by type |
| `/pensieve "docker" --since 7d` | Search + time filter |
| `/pensieve --tag dream-phase` | Filter by tag |
| `/pensieve --all` | Show up to 50 results |

---

## `/req` — Requirements Tracking

Create, filter, and track project requirements with status and priority.

**MCP tool:** `mori-memory_req` / `mori-memory_write`

**Examples:**

| Command | Effect |
|---|---|
| `/req` | Dashboard — all requirements grouped by project |
| `/req --project bifrost` | Filter by project |
| `/req --project bifrost --status pending` | Filter by project and status |
| `/req add "Add rate limiting" --project bifrost --pri high` | Create requirement |
| `/req done req-bifrost-add-rate-limiting` | Mark complete |

Requirements persist as tagged memories in the shared store. They surface automatically in `/brief` until marked done.

---

## `/nats` — Cross-Device Messaging

Publish and subscribe to real-time messages across Claude Code instances (requires NATS server).

**MCP tools:** `mori-nats_pub`, `mori-nats_sub`, `mori-nats_ping`

**Examples:**

| Command | Effect |
|---|---|
| `/nats ping` | Check NATS connection |
| `/nats sub` | Show recent messages from all devices |
| `/nats pub "deploying bifrost v2 — hold off on reboots"` | Publish a message |

Messages persist for 7 days in the JetStream store. Offline instances catch up on reconnect.

### `GitPush` event

When the post-push git hook is installed, every `git push` automatically publishes a `GitPush` event to NATS. Other instances see it via `/nats sub` replay and `/brief` at session start.

**Event payload:**
```json
{
  "hook_event_name": "GitPush",
  "session_id": "abc1234",
  "repo": "mori",
  "branch": "main",
  "sha": "abc1234",
  "message": "feat: content-based ingestion",
  "remote": "origin",
  "client": "your-hostname"
}
```

**NATS message format** (what `/nats sub` shows):
```
[your-hostname] GitPush: mori/main abc1234 — feat: content-based ingestion
```

**Install the hook:** see [docs/getting-started/git-hooks.md](../getting-started/git-hooks.md)

---

## `/ingest` — Universal Ingestion

Extracts durable memories from PDFs, images, CC transcripts, git history, and code files into the shared memory store. Solves the cold-start problem for new users and unlocks institutional knowledge locked in files.

**MCP tools:** `mori-ingest`, `mori-ingest_content`, `mori-ingest_status`, `mori-ingest_preview`

**Parameters:**

| Parameter | Values | Default |
|---|---|---|
| `--source` | File or directory path (repeatable) | required |
| `--type` | auto, transcripts, git, docs, image | auto |
| `--focus` | all, decisions, architecture, conventions, gotchas | all |
| `--tier` | working, canonical, ephemeral | working |
| `--tags` | Comma-separated | — |
| `--since` | 30d, 90d (for transcripts/git) | — |
| `--dry-run` | Full pipeline with LLM, no writes | false |
| `--preview` | Zero-cost parse-only, no LLM call | false |
| `--force` | Re-ingest even if previously ingested | false |
| `--max-cost` | Abort if estimated cost exceeds (USD) | $5.00 |

**Three tiers of execution:**

| Command | LLM called? | Writes to DB? | Cost |
|---|---|---|---|
| `/ingest --preview --source <path>` | No | No | Zero |
| `/ingest --dry-run --source <path>` | Yes | No | Full |
| `/ingest --source <path>` | Yes | Yes | Full |

**Examples:**

```
/ingest --source ~/docs/arch-review.pdf --focus architecture
/ingest --source ~/project/docs/ --focus decisions --tier canonical
/ingest --source ~/whiteboard.jpg --focus architecture
/ingest --source ~/.claude/projects/ --type transcripts --since 30d
/ingest --source ~/my-project --type git --since 90d
/ingest --preview --source ~/docs/ --focus all
```

**Works with remote servers:** `/ingest` reads files on the client device and sends content over the wire — no shared filesystem required. Works whether mori-advisor runs locally or on GCE.

**Cost estimation note:** Token estimates are approximate — image-heavy PDFs may vary 2–3×. Use `--preview` before large ingestions.

**Supported parsers:** text/code (.py, .ts, .md, .json, etc.), PDF (.pdf via pymupdf/pypdf2), images (.png, .jpg, .webp via Pillow), CC transcripts (.jsonl), git history (git log + diffs via subprocess).

---

## Dependencies

| Layer | Component |
|---|---|
| Mori server | Container on GCE VM (port 8968), deployed via Podman |
| Client connection | Direct MCP (`type: "http"`) — no proxy required |
| Storage | SQLite at `/data/mori-advisor/memories.db` (GCE persistent disk) |

All skills delegate to MCP tools on the server. The SKILL.md files are thin wrappers — no scripts, no fallback logic, no per-device customisation. Central control, instant rollback.