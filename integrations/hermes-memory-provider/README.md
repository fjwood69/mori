# hermes-mori-provider

A [hermes-agent](https://github.com/NousResearch/hermes-agent) `MemoryProvider`
plugin that mirrors the agent's durable learnings to a self-hosted
[mori](https://github.com/fjwood69/mori) server as **governed proposals** —
never direct canon.

## What it does

- **Recall** — `prefetch()` searches mori before each turn so the agent starts
  informed, not cold.
- **Mirror** — `on_memory_write()` intercepts durable agent learnings and
  enqueues them as proposals.  A human reviewer with the `dreamer` role must
  approve before they become canon.
- **Non-blocking** — writes are queued in a local SQLite outbox and sent by a
  background thread.  Crash-durable: pending rows survive process restarts.
- **Governance** — read-only tools (`mori_search`, `mori_list_pending`,
  `mori_proposal_status`) let the agent query its own proposal backlog.  It
  cannot approve its own proposals.

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

## Durability signal

The provider reads a YAML frontmatter block at the top of each memory file to
determine durability:

```markdown
---
memory_id: my-learning
durability: durable
type: pattern
tags: [python, async]
---

Body text here — frontmatter is stripped before writing to mori.
```

| Signal | Result |
|---|---|
| `durability: ephemeral` | Dropped — not sent to mori |
| `durability: durable` | Proposed to mori for review |
| No frontmatter, ephemeral target (scratch/temp/wip/draft) | Dropped |
| No frontmatter, other target | Proposed (degraded path — no stable name) |

### Retraction proposals

When hermes-agent emits `action="remove"`, the provider creates a **retraction
proposal** — a new mori memory that asserts the prior fact should be removed.
mori never deletes canon; a human reviewer confirms the retraction.

Retraction names use a `.retracted` suffix: `hermes.my-learning.retracted`.

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
