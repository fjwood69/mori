# Mori Slash Commands

Nine slash commands — `/brief`, `/ready`, `/wrap`, `/dream`, `/consult`, `/pensieve`, `/req`, `/nats`, `/update` — that wire Moku's MCP tools into Claude Code's workflow. Each is a thin SKILL.md that delegates to a deterministic MCP tool on the Mori server.

---

## `/brief` — Session Bootstrap

Loads shared memories and team standards into context at the start of every session. Also runs per-device bootstrap checks (dotfiles, hostname, caveats).

**MCP tool:** `mori_advisor-brief`

**What it returns:**
- Shared memory count (e.g. "45 memories loaded")
- Team standards count with category breakdown (e.g. "5 standards loaded — coding=2, security=2, ethos=1")
- Dream pipeline state (watermark, backlog)

**Usage:** Runs automatically at session start. `/brief` to re-run.

**Design rationale — session grounding instead of RAG:** All context is loaded up front (one LLM cost), not retrieved per-query. Works because the corpus is small (<50 documents). Beyond that, scale via namespace-separated Mori instances rather than adding a vector database.

---

## `/ready` — Personal Session Bootstrap

Fred's personal bootstrap for his machines only. Reads `~/dotfiles/session-brief.md` and follows every instruction: pulls dotfiles, loads shared memories, identifies device, checks cc-share and NATS for cross-session state.

**MCP tool:** `mori_advisor-brief`

**What it does:**
1. Pull latest dotfiles (`git pull`)
2. Load shared memories via `mori_advisor-brief`
3. Identify device (NUC, Twiggy, CB14P, UX3405)
4. Load remaining context (cc-share, recent transcript, state-of-play summaries)
5. Report ready — no autonomous actions

Usage: `/ready` at the start of any personal session. Not deployed to devices other than Fred's machines.

---

## `/wrap` — Session Wrap

Session-closing counterpart to `/ready`. Summarises the session and publishes to cc-share and NATS so the next session (on any device) starts with context.

**MCP tools:** cc-share (`POST /cc-share/`), `mori_advisor-nats_pub`, `mori_advisor-dream_run`

**What it does:**
1. Identifies the user and host
2. Writes a concise session summary (key changes, pending, gotchas) to cc-share with a 7-day TTL
3. Publishes a one-liner to NATS
4. Runs `/dream` to flush any remaining undreamed events

Usage: `/wrap` at the end of a session before closing.

---

## `/dream` — Memory Distillation

Reads recent session events (tool calls, prompts, responses) since the last watermark, sends them to a configurable dream model, and writes extracted memories back to the store. Runs in-container — no host filesystem access needed.

**MCP tool:** `mori_advisor-dream_run`

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

**MCP tool:** `mori_advisor-consult_advisor`

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

**MCP tool:** `mori_advisor-memory_search`

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

**MCP tool:** `mori_advisor-memory_req` / `mori_advisor-memory_write`

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

**MCP tools:** `mori_advisor-nats_pub`, `mori_advisor-nats_sub`, `mori_advisor-nats_ping`

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
| Mori server | Container on GCE VM (port 8968), deployed via Podman |
| Client connection | Direct MCP (`type: "http"`) — no proxy required |
| Storage | SQLite at `/data/mori-advisor/memories.db` (GCE persistent disk) |

All skills delegate to MCP tools on the server. The SKILL.md files are thin wrappers — no scripts, no fallback logic, no per-device customisation. Central control, instant rollback.