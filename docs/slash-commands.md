# Moku Slash Commands

Six slash commands — `/brief`, `/dream`, `/consult`, `/pensieve`, `/req`, `/nats` — that wire Moku's MCP tools into Claude Code's workflow. Each is a thin SKILL.md that delegates to a deterministic MCP tool on the Moku server.

---

## `/brief` — Session Bootstrap

Loads shared memories and team standards into context at the start of every session. Also runs per-device bootstrap checks (dotfiles, hostname, caveats).

**MCP tool:** `moku_advisor-brief`

**What it returns:**
- Shared memory count (e.g. "45 memories loaded")
- Team standards count with category breakdown (e.g. "5 standards loaded — coding=2, security=2, ethos=1")
- Dream pipeline state (watermark, backlog)

**Usage:** Runs automatically at session start. `/brief` to re-run.

**Design rationale — session grounding instead of RAG:** All context is loaded up front (one LLM cost), not retrieved per-query. Works because the corpus is small (<50 documents). Beyond that, scale via namespace-separated Moku instances rather than adding a vector database.

---

## `/dream` — Memory Distillation

Reads recent session events (tool calls, prompts, responses) since the last watermark, sends them to a configurable dream model, and writes extracted memories back to the store. Runs in-container — no host filesystem access needed.

**MCP tool:** `moku_advisor-dream_run`

**Variants:**

| Command | Effect |
|---|---|
| `/dream` | Run the dream pipeline, write memories |
| `/dream --dry-run` | Preview what would be written |
| `/dream --status` | Show watermark, event count, undreamed backlog |

**Scheduled execution:** Runs via cron on the server every 30 minutes. Also triggered synchronously by the `PreCompact` hook before context compression.

**Dream model:** Configurable via `MOKU_DREAM_MODEL` (falls back to `MOKU_MODEL`). Prefer models with strong structured JSON output and rationalisation capabilities.

---

## `/consult` — Strategic Advisor

Sends your question plus optional file context to a configurable advisor model. When a specific focus is given (`--focus security`), relevant team standards are automatically pulled from the memory store and injected into the prompt — so the advisor checks your code against your own baseline, not generic advice.

**MCP tool:** `moku_advisor-consult_advisor`

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

**MCP tool:** `moku_advisor-memory_search`

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

**MCP tool:** `moku_advisor-memory_req` / `moku_advisor-memory_write`

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

**MCP tools:** `moku_advisor-nats_pub`, `moku_advisor-nats_sub`, `moku_advisor-nats_ping`

**Examples:**

| Command | Effect |
|---|---|
| `/nats ping` | Check NATS connection |
| `/nats sub` | Show recent messages from all devices |
| `/nats pub "deploying bifrost v2 — hold off on reboots"` | Publish a message |

Messages persist for 7 days in the JetStream store. Offline instances catch up on reconnect.

---

## Dependencies

| Layer | Component |
|---|---|
| Moku server | Container on GCE VM (port 8968), deployed via Podman |
| Client connection | Direct MCP (`type: "http"`) — no proxy required |
| Storage | SQLite at `/data/moku-advisor/memories.db` (GCE persistent disk) |

All skills delegate to MCP tools on the server. The SKILL.md files are thin wrappers — no scripts, no fallback logic, no per-device customisation. Central control, instant rollback.