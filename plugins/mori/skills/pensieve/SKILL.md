---
name: pensieve
description: Ranked full-text search and browse over the shared memory store. Use to recall past decisions, patterns, or project context.
---

**If the Mori server is unreachable** (a tool call returns a connection error, ECONNREFUSED, or timeout), do not show a stack trace. Tell the user the Mori server appears to be down and that a running Mori server is required. Point them to the quickstart at https://github.com/fjwood69/mori#quickstart and suggest checking the server URL in plugin settings. Then stop.

1. Parse the user's input:
   - First positional is the search `query`
   - `--type`: one of `project`, `decision`, `pattern`, `profile`
   - `--tag`: filter by tag name
   - `--client` / `--device`: filter by client hostname
   - `--since`: time filter — `7d`, `30d`, or ISO date
   - `--all`: show up to 50 results
   - `--limit`: max results (default 10)
2. If the first positional is `read` followed by a kebab-case name, call `mori-memory_read` instead.
3. Otherwise call `mori-memory_search` with the parsed arguments.
4. Present the result.