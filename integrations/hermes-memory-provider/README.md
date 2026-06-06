# hermes-mori-provider

A [hermes-agent](https://github.com/NousResearch/hermes-agent) `MemoryProvider`
plugin that mirrors the agent's durable learnings to a self-hosted
[mori](https://github.com/fjwood69/mori) server as **governed proposals** —
never direct canon.

## What it does

A **two-tier proxy** over a governed mori store:

- **Local Working Memory (LWM)** — a strongly-consistent SQLite overlay. When
  the agent's built-in memory tool edits MEMORY.md / USER.md, the write lands in
  the LWM **synchronously**, so `prefetch()` sees it on the very next turn
  (read-your-writes) — before mori governance has approved anything.
- **Governed proposal pipeline** — the same write is enqueued (non-blocking) in
  a crash-durable outbox and drained to mori as a **proposal**. A human reviewer
  with the `dreamer` role must approve it before it becomes canon. Pending rows
  survive process restarts.
- **Recall** — `prefetch()` merges the LWM overlay with mori canon search
  results, LWM winning on name collision. It never raises into the agent.
- **Reconciliation** — opportunistically (during `prefetch`) the provider
  promotes LWM rows to `canon` on content-hash match, evicts them on rejection,
  and — if a dreamer edited the content before approving — overwrites the local
  copy with the canon version.
- **Governance-safe tools** — read-only tools (`mori_search`,
  `mori_list_pending`, `mori_proposal_status`) let the agent query its own
  proposal backlog. It cannot approve its own proposals.

### Hook usage

| Hook | Behaviour |
|---|---|
| `on_memory_write(action, target, content, metadata=None)` | The **only** hook that drives proposals. `action ∈ {add, replace, remove}`, `target ∈ {memory, user}`. |
| `prefetch(query, *, session_id="")` | Merge LWM + canon; reconcile; never raises. `session_id` is keyword-only. |
| `sync_turn(...)` | Explicit **no-op** — mirroring every turn would flood the dreamer queue with noise. |

## Requirements

- Python 3.11+
- A running mori server (self-hosted; see [mori docs](https://github.com/fjwood69/mori))
- A mori API key with `write` role

No third-party dependencies — uses stdlib only (`urllib`, `sqlite3`, `threading`).

## Installation

hermes-agent's loader scans `$HERMES_HOME/plugins/<name>/` (e.g. `~/.hermes/plugins/mori/`)
for an `__init__.py` exposing `register()` or a `MemoryProvider`. Not yet on PyPI, so
install by dropping the package there:

```bash
mkdir -p "$HERMES_HOME/plugins/mori"
cp -r hermes_mori_provider/* plugin.yaml "$HERMES_HOME/plugins/mori/"
```

The package uses relative imports, so it loads correctly as a directory drop (no pip
install needed). Once published to PyPI: `pip install hermes-mori-provider`.

## Configuration

Set the following environment variable (required). It is the **bare secret** — the
64-char hex string alone, **not** `name:secret`. The `name:` prefix only labels the key
in the server's `MORI_API_KEYS`; sending it in the header returns `401 Unauthorized`.

```bash
export MORI_API_KEY=<your-64-char-bare-secret>   # must be a write-role key
```

Optionally override the server URL (default: `http://localhost:8968`):

```bash
export MORI_SERVER_URL=https://mori.example.com
```

hermes-agent will call `get_config_schema()` during setup and write
non-secret config to `~/.hermes/mori_config.json`.

## Naming

Every mirrored memory gets a deterministic, sanitised mori name of the form
`hermes-{target}-{stable_key}`, guaranteed to match `^[a-zA-Z0-9_-]{1,128}$`
(invalid characters stripped, consecutive hyphens collapsed, right-truncated to
128 while always preserving the `hermes-{target}-` prefix):

| Target | `stable_key` source |
|---|---|
| `user` | `metadata["user_id"]` (default `"default"`) |
| `memory` | `metadata["memory_id"]`, else a deterministic slug of the first ~64 chars of content + an 8-char content-hash suffix |

Names are stable so `replace`/`remove` keep lineage with the original `add`.
There are **no random UUIDs** and **no frontmatter parsing** (a misconception
from an earlier draft — removed in 0.2.0).

## Action mapping & coalescing

| `action` | Op | Behaviour |
|---|---|---|
| `add` | propose | LWM upsert + enqueue a proposal. |
| `replace` | supersede | LWM upsert; if the prior proposal is still **unsent** in the outbox, the queued row is updated in place (no duplicate proposal). |
| `remove` | retract | If the prior proposal is still unsent, the outbox row is deleted and the LWM entry cleared (add-then-remove while local = net no-op). Otherwise a **retraction proposal** is emitted — mori never hard-deletes canon; a reviewer confirms. |

## Outbox back-pressure & circuit breaker

If more than 100 proposals are queued and unsent, new enqueues are dropped with
a WARNING log (configurable via `max_pending`). The background drainer retries
transport errors and 429s with capped exponential back-off, and **opens a
circuit breaker** after repeated mori unavailability to stop hammering the
server. 4xx (non-429) responses dead-letter the row. Lightweight counters are
exposed via `outbox.metrics_snapshot()` (`outbox_depth`, `lwm_pending`,
`proposals_sent`, `proposals_failed`, `breaker_trips`).

## Outbox back-pressure

If more than 100 proposals are queued and unsent, new enqueues are silently
dropped with a WARNING log.  This prevents the mori governance queue from being
flooded.  The threshold is configurable via the `max_pending` constructor
argument.

## Testing

```bash
cd integrations/hermes-memory-provider
pip install pytest
python -m pytest tests/ -v
```

All tests are deterministic — no real network calls, no real sleeps.

## Licence

AGPL-3.0-only — same as mori.
