- name: nats
- description: Cross-device message bus commands

1. Parse the user's input:
   - `ping`: call `mori-nats_ping` and report connection status
   - `sub` or `subscribe`: call `mori-nats_sub` (optionally with `--replay` for last 7 days)
   - `pub` or `publish <message>`: call `mori-nats_pub` with the message text
2. Present the result verbatim.
