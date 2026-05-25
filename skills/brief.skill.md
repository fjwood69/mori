- name: brief
- description: Session bootstrap — load shared knowledge from the Mori server via MCP

1. Pull latest config: `git -C ~/mori-config pull 2>/dev/null || true`
2. Call `mori_brief` for counts and dream state. Shared memory lives on the **Mori server** (GCE/homelab) — never on the local machine.
3. Call `mori_memory_list` with `type_filter=standard`, then `mori_memory_list` for recent/canonical entries; use `mori_memory_read` on any that look relevant to the working context (CWD, recent git log).
4. Report "Ready" — summarise what was loaded. Do not take autonomous actions.
