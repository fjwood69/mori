- name: brief
- description: Session bootstrap — load shared knowledge, get oriented

1. Pull latest config: `git -C ~/mori-config pull 2>/dev/null || true`
2. Call `mori-memory_list` to see shared memories.
3. Call `mori-memory_list --type standard` to load team standards
   (security baseline, coding conventions, company ethos).
4. Read any memories or standards that look relevant to the
   likely working context (check CWD, recent git log).
5. Report "Ready" — summarise what was loaded
   (e.g. "5 memories, 3 standards loaded"). Do not take
   autonomous actions.