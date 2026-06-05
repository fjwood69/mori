---
name: dream
description: Runs the dream pipeline that distils session events into durable memories. Use to flush undreamed events or check dream status.
---

1. Call `mori-dream_status` to see if there are undreamed events.
2. Call `mori-dream_run` to execute the dream phase.
3. Report what was produced.

For `--status` or `--dry-run`, pass the argument directly to `mori-dream_run`.