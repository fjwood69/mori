# CLAUDE.md vs mori `/brief` — complementary, not competing

Both inject context at the start of a coding-agent session. They serve different
roles, and the most robust setup uses **both**.

## `CLAUDE.md` — the unconditional baseline

- Loaded automatically by Claude Code at session start **and after every compaction**.
- Always present — independent of network, MCP connection, or server state.
- **Static** — changes only when you hand-edit the file.
- **Use for:** operational facts that never change — SSH commands, sudo/no-sudo
  rules, non-negotiable working practices, infrastructure constants.

## mori `/brief` — the dynamic, compounding layer

- Injected via an MCP tool call at session start.
- Requires the mori server to be reachable.
- Does **not** survive compaction unless the SessionStart/PostCompact hook
  explicitly re-calls it (`/brief --post-compact`).
- **Dynamic** — updated by the dream pipeline as sessions accumulate.
- **Curated** — dreamer-approved canonical memories, freshness-checked, tiered
  (ephemeral / working / canonical).
- **Use for:** decisions made, patterns established, standards, and institutional
  knowledge that evolves over time.

## The rule

> **If it never changes → `CLAUDE.md`.**
> **If it compounds and evolves → mori.**

## Across compaction

`CLAUDE.md` is re-read automatically after a context compaction. mori requires the
SessionStart hook (`source: "compact"`) to fire and call `/brief --post-compact`.
Keep **both** active for full coverage.

`CLAUDE.md` is the unconditional floor. mori is the compounding layer above it.
They are complementary, not competing.
