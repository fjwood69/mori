- name: pensieve
- description: Search the shared memory store

1. Parse the user's input:
   - First positional is the search `query`
   - `--type`: one of `project`, `decision`, `pattern`, `profile`
   - `--tag`: filter by tag name
   - `--client` / `--device`: filter by client hostname
   - `--since`: time filter — `7d`, `30d`, or ISO date
   - `--all`: show up to 50 results
   - `--limit`: max results (default 10)
2. If the first positional is `read` followed by a kebab-case name, call `moku-memory_read` instead.
3. Otherwise call `moku-memory_search` with the parsed arguments.
4. Present the result.