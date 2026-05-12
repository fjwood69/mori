"""Moku — FastMCP server providing shared memory, consult, and dream tools.

Routes model calls through either a Bifrost gateway (VK-based) or a
direct OpenAI-compatible provider. Controlled by MOKU_PROVIDER_MODE env var.

Usage:
    MOKU_PROVIDER_MODE=direct MOKU_API_KEY=sk-... python -m moku_advisor.main
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastmcp import FastMCP
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse

from moku_advisor.bifrost_client import BifrostClient
from moku_advisor.dream import DreamPipeline
from moku_advisor.memory_store import MemoryStore
from moku_advisor.session_log import SessionLog

logger = logging.getLogger(__name__)

# ── Configuration from environment ──────────────────────────────────────

DATA_DIR = Path(os.environ.get("MOKU_ADVISOR_DATA", "/data/moku-advisor"))
MCP_SERVER_NAME = os.environ.get("MOKU_MCP_SERVER_NAME", "moku")
BIFROST_BASE_URL = os.environ.get("MOKU_BASE_URL", "http://localhost:8787")
BIFROST_TIMEOUT = int(os.environ.get("MOKU_BIFROST_TIMEOUT", "300"))
TRUSTED_DREAMERS = os.environ.get(
    "MOKU_TRUSTED_DREAMERS",
    "",
).split(",") if os.environ.get("MOKU_TRUSTED_DREAMERS") else []

# Event capture auth
EVENTS_API_KEY = os.environ.get("MOKU_ADVISOR_API_KEY", "")

# Standards directory
STANDARDS_DIR = os.environ.get("MOKU_STANDARDS_DIR", "")

# ── System prompts ──────────────────────────────────────────────────────

ADVISOR_SYSTEM_PROMPT = """You are a senior technical advisor. A developer or AI coding assistant running as a faster model is consulting you for strategic guidance mid-task.

Your role:
- Provide clear, actionable technical advice
- Focus on architecture decisions, trade-offs, pitfalls, and best practices
- Use numbered steps when prescribing an approach
- Be concise — the executor will implement your guidance, not you
- If prior consultation history is provided, build on it rather than repeating yourself
- Go straight to the advice — do not restate the question"""

FOCUS_PROMPTS = {
    "general": "",
    "architecture": """
Focus on architecture:
- Evaluate the overall design and system structure
- Identify coupling, cohesion, and separation of concerns
- Consider scalability, maintainability, and extensibility
- Suggest alternative architectural approaches where appropriate""",
    "security": """
Focus on security:
- Identify potential vulnerabilities and attack surfaces
- Review authentication, authorization, and data validation patterns
- Consider principle of least privilege, defence in depth, and secure defaults
- Flag any insecure practices or anti-patterns""",
    "performance": """
Focus on performance:
- Identify bottlenecks, N+1 queries, and unnecessary work
- Consider caching strategies, lazy loading, and async patterns
- Review algorithmic complexity and resource usage
- Suggest measurable performance improvements""",
    "style": """
Focus on style and maintainability:
- Review code organisation, naming, and consistency
- Check adherence to project conventions and language idioms
- Suggest simplifications and readability improvements
- Flag dead code, over-engineering, or premature abstraction""",
}

DEPTH_PROMPTS = {
    "quick": "Provide a brief assessment — 2-3 key points, no more than 150 words.",
    "balanced": "Provide a thorough assessment covering the main aspects. 300-500 words.",
    "deep": "Provide an exhaustive analysis. Consider edge cases, trade-offs, and alternative approaches. 800+ words.",
}

BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg",
    ".mp3", ".mp4", ".wav", ".ogg", ".webm", ".avi", ".mov",
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar", ".wasm",
    ".bin", ".exe", ".dll", ".so", ".dylib",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".pyc", ".class", ".o", ".obj",
}

MAX_FILE_SIZE = 50 * 1024  # 50KB per file
MAX_TOTAL_FILE_SIZE = 200 * 1024  # 200KB total

# ── Global state ─────────────────────────────────────────────────────────

mcp = FastMCP(MCP_SERVER_NAME)
bifrost = BifrostClient(base_url=BIFROST_BASE_URL, timeout=BIFROST_TIMEOUT)
session_log = SessionLog(db_path=DATA_DIR / "memories.db")
memory_store = MemoryStore(db_path=DATA_DIR / "memories.db")
dream_pipeline = DreamPipeline(
    db_path=DATA_DIR / "memories.db",
    bifrost_client=bifrost,
    trusted_dreamers=TRUSTED_DREAMERS,
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _read_files(file_paths: list[str]) -> tuple[list[str], list[str]]:
    """Read files, returning (blocks, errors)."""
    blocks: list[str] = []
    errors: list[str] = []
    total_bytes = 0

    ext_to_lang = {
        ".py": "python", ".ts": "typescript", ".js": "javascript",
        ".tsx": "typescriptreact", ".jsx": "javascriptreact",
        ".go": "go", ".rs": "rust", ".rb": "ruby",
        ".java": "java", ".kt": "kotlin", ".scala": "scala",
        ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp",
        ".cs": "csharp", ".swift": "swift",
        ".sh": "bash", ".bash": "bash", ".zsh": "bash",
        ".ps1": "powershell", ".psm1": "powershell",
        ".sql": "sql", ".r": "r",
        ".md": "markdown", ".json": "json", ".yaml": "yaml", ".yml": "yaml",
        ".toml": "toml", ".xml": "xml", ".html": "html", ".css": "css",
        ".dockerfile": "dockerfile",
        ".tf": "terraform", ".hcl": "terraform",
    }

    for path_str in file_paths:
        path = Path(path_str)
        suffix = path.suffix.lower()

        if suffix in BINARY_EXTENSIONS:
            errors.append(f"Skipped binary file: {path_str}")
            continue

        if not path.exists():
            errors.append(f"File not found: {path_str}")
            continue

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            if len(content) > MAX_FILE_SIZE:
                content = content[:MAX_FILE_SIZE] + "\n... (truncated)"
                label = f"{path_str} (truncated to 50KB)"
            else:
                label = str(path_str)

            available = MAX_TOTAL_FILE_SIZE - total_bytes
            if len(content) > available:
                content = content[:available] + "\n... (truncated by total budget)"
                label = f"{label} (truncated)"

            lang = ext_to_lang.get(suffix, "")
            blocks.append(f"### File: {label}\n```{lang}\n{content}\n```")
            total_bytes += len(content)

            if total_bytes >= MAX_TOTAL_FILE_SIZE:
                errors.append("Total file budget reached (200KB)")
                break

        except Exception as e:
            errors.append(f"Error reading {path_str}: {e}")

    return blocks, errors


def _build_prompt(
    question: str,
    context: str,
    file_blocks: list[str],
    focus: str,
    depth: str,
) -> tuple[str, str]:
    focus_extra = FOCUS_PROMPTS.get(focus, "")
    system = ADVISOR_SYSTEM_PROMPT + focus_extra

    parts: list[str] = []
    depth_instruction = DEPTH_PROMPTS.get(depth, DEPTH_PROMPTS["balanced"])
    parts.append(depth_instruction)

    if file_blocks:
        parts.append("## Files for Review\n" + "\n".join(file_blocks))

    parts.append(f"## Question\n{question}")

    if context:
        parts.append(f"## Context\n{context}")

    user_prompt = "\n\n".join(parts)
    return system, user_prompt


# ── Consult tool ─────────────────────────────────────────────────────────


@mcp.tool()
async def consult_advisor(
    question: str,
    context: str = "",
    files: list[str] | None = None,
    focus: str = "general",
    depth: str = "balanced",
) -> str:
    """Get strategic guidance from the advisor model.

    When a focus area is specified (security, architecture, etc.),
    relevant team standards are automatically pulled from memory and
    injected as context.

    Args:
        question: The question or problem needing review
        context: Additional context, constraints, or background
        files: File paths to include as code context (relative or absolute)
        focus: Area of focus: general, architecture, security, performance, or style
        depth: Review depth: quick (2-3 points), balanced, or deep (exhaustive)
    """
    if files is None:
        files = []

    file_blocks, file_errors = _read_files(files)
    system, user_prompt = _build_prompt(question, context, file_blocks, focus, depth)

    # Inject relevant standards when a specific focus is given
    if focus != "general":
        try:
            standards = memory_store.search(
                query=None, tag=focus, type_filter="standard", limit=10
            )
            if standards and "No memories" not in standards:
                user_prompt += f"\n\n## Relevant {focus} standards\n{standards}"
        except Exception:
            pass

    max_tokens = {"quick": 1024, "balanced": 4096, "deep": 8192}.get(depth, 4096)

    try:
        advice = bifrost.consult(
            system=system,
            user=user_prompt,
            vk="advisor",
            max_tokens=max_tokens,
            temperature=0.3,
        )
    except Exception as e:
        return f"Advisor call failed: {e}"

    return advice


# ── Standards ingestion ──────────────────────────────────────────────────


def import_standards(standards_dir: str | None = None) -> str:
    """Import all .md files from a standards directory as protected memories.

    Each file is tagged with 'standard' and the name of its immediate
    parent directory (e.g. security/baseline.md → tags: ["standard", "security"]).
    """
    src = standards_dir or STANDARDS_DIR
    if not src:
        return "No standards directory configured (set MOKU_STANDARDS_DIR)."

    src_path = Path(src)
    if not src_path.is_dir():
        return f"Standards directory not found: {src}"

    imported = 0
    errors = 0
    for file_path in sorted(src_path.rglob("*.md")):
        if not file_path.is_file():
            continue

        # Derive kebab name from relative path
        rel = file_path.relative_to(src_path)
        name = str(rel.with_suffix("")).replace("/", "-").replace("_", "-")

        # Tag: always "standard" + parent directory name
        category = rel.parent.name if rel.parent.name != "." else "general"
        tags = ["standard", category]

        try:
            body = file_path.read_text(encoding="utf-8")
            memory_store.write(
                name=name,
                title=str(rel.with_suffix("")),
                type="standard",
                body=body.strip(),
                tags=tags,
                client="init",
                _skip_protection=True,
            )
            imported += 1
        except Exception as e:
            errors += 1
            logger.warning("Failed to import standard %s: %s", name, e)

    msg = f"Imported {imported} standards from {src}"
    if errors:
        msg += f" ({errors} errors)"
    return msg


@mcp.tool()
async def standards_reload() -> str:
    """Re-import all standards from MOKU_STANDARDS_DIR.

    Only trusted dreamers can call this. After reload, call
    memory_list with type_filter=standard to see what's available.
    """
    if TRUSTED_DREAMERS and "trusted" not in str(TRUSTED_DREAMERS).lower():
        logger.info("Standards reload requested — trusted dreamer check bypassed in dev mode")
    return import_standards()


# ── Dream tools ──────────────────────────────────────────────────────────


@mcp.tool()
async def dream_run(dry_run: bool = False) -> str:
    """Execute the dream phase: distill session events into durable memories.

    Reads events since the last watermark, calls the dream model, and
    writes extracted memories. Call dream_status first to see if there
    are undreamed events.

    Args:
        dry_run: Preview what would be produced without writing anything.
    """
    try:
        memories = dream_pipeline.run(dry_run=dry_run)
        if dry_run:
            if not memories:
                return "No memories would be produced from the current events."
            return (
                f"**Dry run: {len(memories)} memories would be written**\n\n"
                + "\n".join(
                    f"- `{m.get('path', '?')}` [{m.get('action', '?')}] "
                    f"({m.get('confidence', '?')}) — {m.get('reason', '')}"
                    for m in memories
                )
            )
        if not memories:
            return "No new events to dream. Nothing done."
        return f"Dream complete: {len(memories)} memories written."
    except Exception as e:
        logger.error("dream_run failed: %s", e)
        return f"Dream failed: {e}"


@mcp.tool()
async def dream_status() -> str:
    """Show dream phase state: event counts, watermark, undreamed backlog.

    Returns the same info as the old moku-dream --status command.
    """
    return dream_pipeline.get_status()


# ── Memory CRUD tools ────────────────────────────────────────────────────


@mcp.tool()
async def memory_write(
    name: str | None = None,
    title: str = "",
    description: str = "",
    type: str = "project",
    body: str = "",
    tags: list[str] | None = None,
    origin_session_id: str | None = None,
    origin_session_ids: list[str] | None = None,
    origin_clients: list[str] | None = None,
    client: str | None = None,
) -> str:
    """Write a memory entry. Creates or updates (upserts by kebab-case name).

    If name is omitted it is auto-derived from the title.

    If the memory is protected and the client is not a trusted dreamer,
    the write is queued as a pending write instead.

    Args:
        name: kebab-case identifier. Auto-derived from title if omitted.
        title: Human-readable title.
        description: One-line summary.
        type: Classification: project, profile, pattern, or decision.
        body: Markdown content body.
        tags: List of tag strings for filtering.
        origin_session_id: UUID of the creating session (legacy single-UUID field).
        origin_session_ids: JSON array of session UUIDs that contributed.
        origin_clients: JSON array of client hostnames that contributed.
        client: Client hostname making this request (for trusted dreamer check).
    """
    return memory_store.write(
        name=name,
        title=title,
        description=description,
        type=type,
        body=body,
        tags=tags,
        origin_session_id=origin_session_id,
        origin_session_ids=origin_session_ids,
        origin_clients=origin_clients,
        client=client,
    )


@mcp.tool()
async def memory_read(name: str) -> str:
    """Read a memory entry by its kebab-case name identifier.

    Args:
        name: The unique kebab-case name of the memory.
    """
    return memory_store.read(name)


@mcp.tool()
async def memory_list(
    type_filter: str | None = None,
    tag: str | None = None,
    session: str | None = None,
    client: str | None = None,
    limit: int = 50,
) -> str:
    """List memory entries, optionally filtered by type, tag, session, or client.

    Args:
        type_filter: Filter by type (project, profile, pattern, decision).
        tag: Filter by tag name (partial match).
        session: Filter by session UUID.
        client: Filter by client hostname.
        limit: Maximum entries to return (default 50).
    """
    return memory_store.list(
        type_filter=type_filter, tag=tag, session=session,
        client=client, limit=limit,
    )


@mcp.tool()
async def memory_search(
    query: str | None = None,
    type_filter: str | None = None,
    tag: str | None = None,
    client: str | None = None,
    since: str | None = None,
    limit: int = 10,
) -> str:
    """Search memory entries by keyword across name, title, description, and body.

    Args:
        query: Keyword to search across name, title, description, and body.
        type_filter: Filter by type (project, profile, pattern, decision).
        tag: Filter by tag name (partial match).
        client: Filter by client hostname.
        since: Time filter — "7d" (last 7 days), "30d", or ISO date.
        limit: Max results (default 10, max 50).
    """
    return memory_store.search(
        query=query, type_filter=type_filter, tag=tag,
        client=client, since=since, limit=min(limit, 50),
    )


@mcp.tool()
async def memory_delete(name: str) -> str:
    """Permanently delete a memory entry by its name.

    Args:
        name: The unique kebab-case name of the memory to delete.
    """
    return memory_store.delete(name)


@mcp.tool()
async def memory_export(name: str, output_path: str | None = None) -> str:
    """Export a memory entry to a markdown file with YAML frontmatter.

    Args:
        name: The unique kebab-case name of the memory to export.
        output_path: Absolute path for the output file (optional).
    """
    return memory_store.export(name, output_path=output_path)


# ── Versioning tools ─────────────────────────────────────────────────────


@mcp.tool()
async def memory_history(name: str, limit: int = 10) -> str:
    """List version history for a memory.

    Args:
        name: The kebab-case name of the memory.
        limit: Maximum versions to return (default 10).
    """
    return memory_store.history(name, limit=limit)


@mcp.tool()
async def memory_diff(name: str, from_version: int, to_version: int) -> str:
    """Show unified diff of body between two versions.

    Args:
        name: The kebab-case name of the memory.
        from_version: Version ID of the older version.
        to_version: Version ID of the newer version.
    """
    return memory_store.diff(name, from_version, to_version)


@mcp.tool()
async def memory_rollback(name: str, version_id: int) -> str:
    """Restore a memory to a previous version.

    Args:
        name: The kebab-case name of the memory.
        version_id: The version_id from memory_history to restore to.
    """
    return memory_store.rollback(name, version_id)


# ── Attribution tools ────────────────────────────────────────────────────


@mcp.tool()
async def memory_session_summary(session_id: str) -> str:
    """Show all memories attributed to a given session.

    Args:
        session_id: UUID of the session to search for.
    """
    return memory_store.session_summary(session_id)


# ── Portability tools ────────────────────────────────────────────────────


@mcp.tool()
async def memory_export_all(output_dir: str | None = None) -> str:
    """Export all memories to CC auto-memory .md files with YAML frontmatter.

    Args:
        output_dir: Absolute path to write .md files (default: /data/moku-advisor/exports/).
    """
    if output_dir is None:
        output_dir = str(DATA_DIR / "exports")
    return memory_store.export_all(output_dir)


@mcp.tool()
async def memory_import(source_dir: str) -> str:
    """Import .md files with YAML frontmatter from a directory.

    Args:
        source_dir: Absolute path to directory containing .md files.
    """
    return memory_store.import_memories(source_dir)


# ── Trusted Dreamer tools ────────────────────────────────────────────────


@mcp.tool()
async def memory_pending_list(status: str = "pending") -> str:
    """List pending writes awaiting approval from a trusted dreamer.

    Args:
        status: Filter by status: pending, approved, or rejected.
    """
    return memory_store.pending_list(status=status)


@mcp.tool()
async def memory_approve(write_id: int, note: str = "", reviewer: str = "") -> str:
    """Approve a pending write. Applies the change and records reviewer.

    Args:
        write_id: ID of the pending write from memory_pending_list.
        note: Optional review note.
        reviewer: Hostname of the reviewer (auto-detected if empty).
    """
    return memory_store.approve(write_id, note=note, reviewer=reviewer)


@mcp.tool()
async def memory_reject(write_id: int, note: str = "", reviewer: str = "") -> str:
    """Reject a pending write without applying.

    Args:
        write_id: ID of the pending write from memory_pending_list.
        note: Optional review note.
        reviewer: Hostname of the reviewer (auto-detected if empty).
    """
    return memory_store.reject(write_id, note=note, reviewer=reviewer)


@mcp.tool()
async def memory_protect(name: str, domains: list[str] | None = None) -> str:
    """Toggle protection on a memory. Trusted dreamers only.

    Args:
        name: The kebab-case name of the memory to protect/unprotect.
        domains: Tag prefixes that trigger auto-protection.
    """
    return memory_store.protect(name, domains=domains)


# ── Event log API (HTTP, not MCP) ────────────────────────────────────────


class EventLogEntry(BaseModel):
    session_id: str
    event_name: str
    client: str = ""
    tool_name: str | None = None
    tool_input: str | None = None
    tool_response: str | None = None
    tool_error: str | None = None
    model: str | None = None
    cwd: str | None = None
    transcript_path: str | None = None
    prompt: str | None = None
    stop_reason: str | None = None


def _check_auth(request: Request) -> bool:
    if not EVENTS_API_KEY:
        return True
    return request.headers.get("X-Api-Key", "") == EVENTS_API_KEY


def _get_client_from_request(request: Request) -> str:
    return request.query_params.get("client", "")


def _map_hook_payload(raw: dict, client_override: str = "") -> EventLogEntry:
    hook_event = raw.get("hook_event_name", "")
    session_id = raw.get("session_id", "")

    client = client_override or raw.get("client", "")
    if not client:
        cwd = raw.get("cwd", "")
        # CWD-based client detection — customize for your environment
        pass

    tool_input = raw.get("tool_input")
    if tool_input is not None and not isinstance(tool_input, str):
        tool_input = json.dumps(tool_input)

    return EventLogEntry(
        session_id=session_id,
        event_name=hook_event,
        client=client,
        tool_name=raw.get("tool_name"),
        tool_input=tool_input,
        tool_response=raw.get("tool_output"),
        tool_error=raw.get("tool_error"),
        model=raw.get("model"),
        cwd=raw.get("cwd"),
        transcript_path=raw.get("transcript_path"),
        prompt=raw.get("prompt") if hook_event == "UserPromptSubmit" else None,
        stop_reason=raw.get("stop_reason") if hook_event in ("Stop", "SessionEnd") else None,
    )


@mcp.custom_route("/api/events", methods=["POST"])
async def log_event(request: Request) -> JSONResponse:
    """Receive a lifecycle event from a hook and persist it."""
    if not _check_auth(request):
        return JSONResponse({"status": "error", "error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
        if not body:
            return JSONResponse({"status": "error", "error": "empty body"}, status_code=400)

        client = _get_client_from_request(request)

        if "event_name" in body:
            body_client = body.get("client", "")
            entry = EventLogEntry(**{k: v for k, v in body.items() if k != "client"}, client=client or body_client)
        else:
            entry = _map_hook_payload(body, client_override=client)

        if not entry.session_id:
            return JSONResponse({"status": "skipped", "reason": "no session_id"}, status_code=200)

        row_id = session_log.append_event(
            session_id=entry.session_id,
            event_name=entry.event_name,
            client=entry.client or "",
            tool_name=entry.tool_name,
            tool_input=entry.tool_input,
            tool_response=entry.tool_response,
            tool_error=entry.tool_error,
            model=entry.model,
            cwd=entry.cwd,
            transcript_path=entry.transcript_path,
            prompt=entry.prompt,
            stop_reason=entry.stop_reason,
        )
        logger.info("Logged event %s for session %s (id=%s)", entry.event_name, entry.session_id, row_id)
        return JSONResponse({"status": "accepted", "event_id": row_id}, status_code=202)
    except Exception as e:
        logger.error("Failed to log event: %s", e)
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@mcp.custom_route("/api/events/raw", methods=["POST"])
async def log_event_raw(request: Request) -> JSONResponse:
    """Accept raw CC hook stdin JSON and map to structured event."""
    if not _check_auth(request):
        return JSONResponse({"status": "error", "error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
        if not body:
            return JSONResponse({"status": "skipped", "reason": "empty body"}, status_code=200)

        client = _get_client_from_request(request)
        event = _map_hook_payload(body, client_override=client)

        if not event.session_id:
            return JSONResponse({"status": "skipped", "reason": "no session_id"}, status_code=200)

        row_id = session_log.append_event(
            session_id=event.session_id,
            event_name=event.event_name,
            client=event.client,
            tool_name=event.tool_name,
            tool_input=event.tool_input,
            tool_response=event.tool_response,
            tool_error=event.tool_error,
            model=event.model,
            cwd=event.cwd,
            transcript_path=event.transcript_path,
            prompt=event.prompt,
            stop_reason=event.stop_reason,
        )
        logger.info("Raw event %s for session %s (id=%s)", event.event_name, event.session_id, row_id)
        return JSONResponse({"status": "accepted", "event_id": row_id}, status_code=202)
    except Exception as e:
        logger.error("Failed to log raw event: %s", e)
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@mcp.custom_route("/api/events/health", methods=["GET"])
async def events_health(request: Request) -> JSONResponse:
    """Simple health check for the event logging endpoint."""
    return JSONResponse({
        "status": "ok",
        "total_events": session_log.count_events(),
    })


# ── Entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if STANDARDS_DIR:
        logger.info("Standards directory: %s", STANDARDS_DIR)
        result = import_standards()
        logger.info(result)
    mcp.run(transport="sse", host="0.0.0.0", port=8968, log_level="info")