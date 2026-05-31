---
name: msg
description: Inter-agent messaging — send tasks, questions, and decisions to other Mori agents over NATS
---

Parse the user's input:

- `send <to> <type> <body>` — call `mori-msg_send(to=<to>, type=<type>, body=<body>)`
- `recv` or `inbox` — call `mori-msg_recv(unacked=True)`, format as inbox
- `thread <id>` — call `mori-msg_thread(id=<id>)`
- `send --broadcast <body>` — call `mori-msg_send(to="broadcast", type="broadcast", body=<body>)`
- `ack <id>` — call `mori-msg_send(to="<from_host>", type="ack", reply_to=<id>, body="acknowledged")`
- `done <id>` — call `mori-msg_send(to="<from_host>", type="done", reply_to=<id>, body="completed")`

Valid types: `task`, `decision`, `question`, `reply`, `ack`, `done`, `broadcast`.

For tasks and questions in inbox output, include a ready-made reply command so the agent can respond in one step.

Example inbox format:

```
── Pending Messages ─────────────────────────────────────────────
[task]  from twiggy  2026-05-31 14:22  id=a3f9c2b1
  Extract rate limiting into its own middleware module in bifrost/src/middleware/
  → ack: mori-msg_send(to="twiggy", type="ack", reply_to="a3f9c2b1-...", body="acknowledged")

[question]  from gce  2026-05-31 15:01  id=b4e8f3c2
  Did you resolve the JWT expiry edge case in session.rs?
  → reply: mori-msg_send(to="gce", type="reply", reply_to="b4e8f3c2-...", body="...")
```
