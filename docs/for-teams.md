![Mori — One Forest, Many Agents](https://raw.githubusercontent.com/fjwood69/mori/a555057224a2ab21e4cbf52e907773801c8afde4/docs/assets/header-dark%20.svg)

# Mori for Small Teams

For teams of 2–10 developers who want shared agent memory without infrastructure overhead.

---

## The problem

Your teammate fixed the auth bug yesterday. You are about to spend two hours investigating it.
Your colleague abandoned the Redis approach last week for a reason nobody documented. You are about to propose it again.
Agent B has no idea what Agent A decided — until something breaks and they find out the hard way.

Mori fixes this. One shared memory store. Every session starts informed.

---

## What Mori does differently

| | Slack | Notion | Per-session RAG | Mori |
|---|---|---|---|---|
| **When context arrives** | When someone remembers to post | When someone writes it down | Every query, ad hoc | At session start, automatically |
| **What gets captured** | Human summaries | Human documentation | Raw text chunks | Distilled decisions, patterns, fixes |
| **Cost to the team** | Interruptions and channel noise | Maintenance burden | API spend per query | Once per dream cycle |
| **Accuracy** | Variable | Stale quickly | High noise | Signal, with attribution |

---

## Standing it up

**One person deploys. Everyone else runs a script.**

**1. Deploy the server**

One shared instance — a dev box, a small GCP VM (~$12/month), or a homelab node.

- [Docker Compose / homelab](../deploy/homelab/docker-compose.yml)
- [GCP deployment](../deploy/gcp/)

Tailscale handles reachability across locations and machines. No port forwarding required.

**2. Connect each agent**

One command per developer per machine. Five minutes each. Installs the MCP server, lifecycle hooks, and skills.

- [Claude Code](getting-started/claude-code.md)
- [Cursor](getting-started/cursor.md)
- [Cline](getting-started/cline.md)
- [Antigravity](getting-started/antigravity.md)

**3. Run `/wrap` at the end of your first session**

This seeds the shared store. The dream pipeline distils it within minutes.

The next day, `/brief` surfaces your teammates' context before you write a line of code.

---

## What good looks like

After two days of normal usage, `/brief` surfaces something like this:

```
[decision]  Auth middleware refactor — agreed to extract rate limiting into its
            own module before adding the new OAuth provider. JWT validation stays
            in middleware, throttling moves to src/rate/. — 2 days ago

[pattern]   Postgres connection pooling — team standard is max 20 connections per
            service instance. Anything above this has caused timeouts in staging.

[fix]       Race condition in the task queue — fixed in commit 3a7f9c2. Root cause
            was double-ack on reconnect. If you see "duplicate task" errors, check
            your consumer group config first.

[decision]  Decided against Redis for session state — latency was fine but the
            ops overhead wasn't worth it for our scale. Sticking with Postgres.
```

Nobody wrote this manually. The dream pipeline distilled it from session events. Every agent on the team starts their next session with it already loaded.

---

## The one habit: `/wrap`

The store only compounds if sessions end cleanly.

`/wrap` summarises the session, publishes a one-liner to the team NATS bus, and flushes undreamed events. It takes 30 seconds.

- **Without `/wrap`** — events still capture on schedule, but rich context is lost. The store stagnates.
- **With `/wrap`** — every session contributes to the next person's starting context.

End your session with `/wrap`. Treat it like committing before you close your laptop.

---

## Trust model

One server, one shared memory pool. There are no per-user namespaces in v1.x — memories written by one agent are visible to all.

For small teams sharing a codebase, a repo, and a CI pipeline, this is the point. Mori is a shared brain, not a private notebook. Memories belong to the team, not the individual.

Apply the same judgement you use in a team Slack channel: do not run sessions containing credentials or secrets against a shared Mori instance.

Per-user namespacing ships in v2.1.

**Governed writes (v2.3.0).** Every write passes one audited chokepoint — a `write_audit` row is
recorded in the same transaction, so you always know who wrote what. On top of that, two opt-in
controls (`MORI_TIER_ENFORCE`, `MORI_ANATOMY_ENFORCE`) let you restrict who may write the protected
`canonical` tier and require new memories to carry a real warrant. Both default to *audit-mode* —
they observe and count before they ever block — so you can measure the impact, then flip enforcement
per-actor. See the [Write chokepoint](reference/configuration.md#write-chokepoint--audit--tieranatomy-enforcement).

---

## Cost

Dream pipeline costs depend on session volume and model choice. A solo developer running Claude Sonnet sees roughly $10/month. Configure `MORI_DREAM_MODEL` to balance cost against distillation quality — or swap in a locally-hosted open weights model for near-zero running costs.

For the fast VK freshness check, Claude Haiku is the right choice — it's a lightweight binary decision that doesn't need the full dream model. Configure separately via `MORI_FAST_VK_MODEL`.

Server infrastructure starts at ~$12/month on GCP, or nothing if you already run a homelab node.

---

## Advanced configuration

Running Mori for a larger team, a regulated environment, or a multi-team organisation?
See [Team configuration reference](reference/team-configuration.md) — `/brief` policy design, token cost profiles, standards corpus setup, governance, and multi-team deployment.
