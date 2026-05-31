# mori-msg — Inter-Agent Messaging

`mori-msg` lets Claude Code instances delegate tasks, ask questions, and share decisions across your device network. Messages are addressed to specific agents by hostname, or broadcast to all. Pickup happens at the next `/brief` — no mid-session push, no daemons required in the active CC session.

## Quick start

```
/msg send nuc15pro task "Extract rate limiting into its own module in bifrost/src/middleware/"
/msg inbox
/msg ack <message-id>
```

## Message types

| Type | Meaning |
|------|---------|
| `task` | Delegate work to another agent. Receiver auto-acks; task appears in `/brief`. |
| `decision` | Record a cross-cutting decision. Written directly to `memory_store` on receipt — no human session needed. |
| `question` | Ask another agent something. Receiver replies with type `reply`. |
| `reply` | Direct reply to a `question`. Always has `reply_to` set. |
| `ack` | Acknowledge receipt of a `task`. |
| `done` | Mark a `task` complete. `reply_to` points to original task ID. |
| `broadcast` | Session summary or general awareness (equivalent to `/wrap` NATS pub). |

## Slash command

```
/msg send <to> <type> <body>     # send addressed message
/msg send --broadcast <body>     # fan-out to all agents
/msg recv                        # inbox — all pending
/msg inbox                       # alias for recv
/msg thread <id>                 # full reply thread
/msg ack <id>                    # ack a task
/msg done <id>                   # mark task done
```

## MCP tools

### `mori-msg_send`

```
mori-msg_send(to="nuc15pro", type="task", body="...", reply_to="<uuid>")
```

Publishes directly to NATS. Returns `Sent [task] to nuc15pro (id=a3f9c2b1)`.

### `mori-msg_recv`

```
mori-msg_recv(unacked=True, types=["task", "question"])
```

Reads from `msg.db` (populated by the running `mori-msg` daemon). Returns formatted inbox.

Parameters:
- `types` — filter by type(s)
- `from_agent` — filter by sender hostname
- `unacked` — only `status=pending` messages
- `include_broadcast` — include `mori.msg.broadcast` (default `true`)

### `mori-msg_thread`

```
mori-msg_thread(id="a3f9c2b1-4d2e-4f1a-b3c2-d4e5f6a7b8c9")
```

Returns root message + all replies in chronological order.

## Infrastructure

**NATS stream:** `MORI_MSG` — subjects `mori.msg.*` + `mori.reply.*`, 7-day retention.  
**DB file:** `msg.db` in `MORI_ADVISOR_DATA` — separate from `memories.db`, sole writer is `mori-msg` daemon.  
**Daemon:** `python -m mori_advisor.msg_daemon` — durable pull consumer, survives restarts.

### Starting the daemon (NUC)

The `start-nuc.sh` script launches `mori-msg` automatically alongside `mori-advisor`.

### Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `MORI_MSG_HEADLESS_ENABLED` | `false` | Spawn headless `claude` for incoming tasks |
| `MORI_MSG_HEADLESS_TRUSTED` | `""` | Comma-separated hostnames allowed to trigger headless CC |

## `/brief` integration

At session start, `/brief` calls `mori-msg_recv(unacked=True)` and surfaces any pending tasks or questions with ready-made reply commands.

## `/wrap` integration

`/wrap` publishes the session summary as a `broadcast` type message alongside the existing NATS `cc.>` pub, making it replayable via `mori-msg_recv`.
