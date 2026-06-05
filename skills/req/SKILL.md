---
name: req
description: Lightweight project requirements checklist surfaced via /brief. Use to track or update outstanding project tasks.
---

# /req — Project Requirements and Delivery Tracking

When the user runs `/req`, parse the arguments and call the `mori_advisor-memory_req` MCP tool, or perform CRUD operations on `type=requirement` memories.

Do NOT search for files locally. All tool calls go through the MCP server.

## Modes

### `/req` — Dashboard
Call `mori_advisor-memory_req()` with no arguments. Shows all requirements grouped by project with status counts.

### `/req --project <name>` — Filter by project
Call `mori_advisor-memory_req(project="<name>")`. Shows requirements tagged with `project-<name>`.

### `/req --project <name> --status <value>` — Filter by project and status
Call `mori_advisor-memory_req(project="<name>", status="<value>")`. Status values: `done`, `pending`, `in-progress`, `blocked`.

### `/req add "<title>" --project <name> [--desc "<desc>"] [--pri high|medium|low] [--fr|--nfr]`

Create a new requirement memory:
1. Compute name as `req-<project>-<slugified-title>` (kebab-case)
2. Build tags: `["project-<name>", "status-pending", "pri-<priority>"]` + `"fr"` or `"nfr"` if specified
3. Call `mori_advisor-memory_write(name="<name>", title="<title>", description="<desc>", type="requirement", tags=<tags>, body="<desc>")`
4. Report the created memory name

### `/req done <name>` — Mark complete

1. Call `mori_advisor-memory_read(name)` to get current tags
2. Replace any existing `status-*` tag with `status-done`
3. Call `mori_advisor-memory_write(name=..., tags=<updated tags>, type="requirement")` with the same name and updated tags

### `/req block <name> [--reason "<text>"]` — Mark blocked

1. Read current memory via `mori_advisor-memory_read(name)`
2. Swap `status-*` to `status-blocked`
3. Append reason to body if provided
4. Write back via `mori_advisor-memory_write`

### `/req wip <name>` — Mark in-progress

1. Read, swap `status-*` to `status-in-progress`, write back

### `/req import <filepath> [--project <name>]` — Bulk import

Read the file from the given path, parse requirements, create each one.

## Tag conventions

Each requirement memory uses these tags:

| Tag | Purpose |
|---|---|
| `project-<name>` | Associates with a project |
| `status-pending` | Not started (default) |
| `status-in-progress` | Being worked on |
| `status-done` | Completed |
| `status-blocked` | Blocked by external dependency |
| `pri-high`, `pri-medium`, `pri-low` | Priority |
| `fr` | Functional requirement |
| `nfr` | Non-functional requirement |

## Argument parsing

Parse the raw input string:
- **First positional**: mode (`add`, `done`, `block`, `wip`, `import`) or empty for dashboard
- `--project` / `-p`: Project name
- `--status` / `-s`: Status filter (dashboard mode only)
- `--desc`: Description text
- `--pri`: Priority (`high`, `medium`, `low`, default `medium`)
- `--fr` | `--nfr`: Requirement type flag
- `--reason`: Reason (for `block` mode)

## Examples

| Input | Effect |
|---|---|
| `/req` | Dashboard of all requirements |
| `/req --project myapp` | View myapp requirements |
| `/req --project mori --status done` | Completed Mori requirements |
| `/req add "OAuth2 login" --project myapp --pri high --fr` | Create new FR |
| `/req done req-myapp-oauth2-login` | Mark as done |
| `/req block req-myapp-oauth2 --reason "Waiting on IAM"` | Block with reason |
| `/req import ./specs.md --project myapp` | Bulk import from file |