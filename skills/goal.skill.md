# Goal Skill — Requirements and Delivery Tracking

When the user runs `/goal`, parse the arguments and call the `moku_advisor-memory_goals` MCP tool, or perform CRUD operations on `type=requirement` memories.

Do NOT search for files locally. All tool calls go through the MCP server.

## Modes

### `/goal` — Dashboard
Call `moku_advisor-memory_goals()` with no arguments. Shows all requirements grouped by project with status counts.

### `/goal --project <name>` — Filter by project
Call `moku_advisor-memory_goals(project="<name>")`. Shows requirements tagged with `project-<name>`.

### `/goal --project <name> --status <value>` — Filter by project and status
Call `moku_advisor-memory_goals(project="<name>", status="<value>")`. Status values: `done`, `pending`, `in-progress`, `blocked`.

### `/goal add "<title>" --project <name> [--desc "<desc>"] [--pri high|medium|low] [--fr|--nfr]`

Create a new requirement memory:
1. Compute name as `req-<project>-<slugified-title>` (kebab-case)
2. Build tags: `["project-<name>", "status-pending", "pri-<priority>"]` + `"fr"` or `"nfr"` if specified
3. Call `moku_advisor-memory_write(name="<name>", title="<title>", description="<desc>", type="requirement", tags=<tags>, body="<desc>")`
4. Report the created memory name

### `/goal done <name>` — Mark complete

1. Call `moku_advisor-memory_read(name)` to get current tags
2. Replace any existing `status-*` tag with `status-done`
3. Call `moku_advisor-memory_write(name=..., tags=<updated tags>, type="requirement")` with the same name and updated tags

### `/goal block <name> [--reason "<text>"]` — Mark blocked

1. Read current memory via `moku_advisor-memory_read(name)`
2. Swap `status-*` to `status-blocked`
3. Append reason to body if provided
4. Write back via `moku_advisor-memory_write`

### `/goal wip <name>` — Mark in-progress

1. Read, swap `status-*` to `status-in-progress`, write back

### `/goal import <filepath> [--project <name>]` — Bulk import

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
| `/goal` | Dashboard of all requirements |
| `/goal --project bifrost` | View Bifrost requirements |
| `/goal --project moku --status done` | Completed Moku requirements |
| `/goal add "OAuth2 login" --project bifrost --pri high --fr` | Create new FR |
| `/goal done req-bifrost-oauth2-login` | Mark as done |
| `/goal block req-bifrost-oauth2 --reason "Waiting on IAM"` | Block with reason |
| `/goal import ./specs.md --project bifrost` | Bulk import from file |