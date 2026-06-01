"""Mori — FastMCP server providing shared memory, consult, and dream tools.

Routes model calls through either a Bifrost gateway (VK-based) or a
direct OpenAI-compatible provider. Controlled by MORI_PROVIDER_MODE env var.

Usage:
    MORI_PROVIDER_MODE=direct MORI_API_KEY=sk-... python -m mori_advisor.main
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastmcp import FastMCP
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from mori_advisor.auth import configured_clients, generate_key, init_auth
from mori_advisor.bifrost_client import BifrostClient
from mori_advisor.dream import DreamPipeline
from mori_advisor.ingestion import IngestionPipeline
from mori_advisor.metrics import (
    events_counter,
    eviction_queue_gauge,
    init_metrics,
    memories_gauge,
    pending_writes_gauge,
)
from mori_advisor.store import get_store as _get_store

logger = logging.getLogger(__name__)

# ── Configuration from environment ──────────────────────────────────────

DATA_DIR = Path(os.environ.get("MORI_ADVISOR_DATA", "/data/mori-advisor"))
MCP_SERVER_NAME = os.environ.get("MORI_MCP_SERVER_NAME", "mori")
BIFROST_BASE_URL = os.environ.get("MORI_BASE_URL", "http://localhost:8787")
BIFROST_TIMEOUT = int(os.environ.get("MORI_BIFROST_TIMEOUT", "300"))
TRUSTED_DREAMERS = (
    os.environ.get(
        "MORI_TRUSTED_DREAMERS",
        "",
    ).split(",")
    if os.environ.get("MORI_TRUSTED_DREAMERS")
    else []
)

# Standards directory
STANDARDS_DIR = os.environ.get("MORI_STANDARDS_DIR", "")

# Skills directory (for /update tool to read skill content server-side)
SKILLS_DIR = os.environ.get("MORI_SKILLS_DIR", "")

NATS_URL = os.environ.get("MORI_NATS_URL", "nats://localhost:4222")

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
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".ico",
    ".webp",
    ".svg",
    ".mp3",
    ".mp4",
    ".wav",
    ".ogg",
    ".webm",
    ".avi",
    ".mov",
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".7z",
    ".rar",
    ".wasm",
    ".bin",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".ttf",
    ".otf",
    ".woff",
    ".woff2",
    ".eot",
    ".pyc",
    ".class",
    ".o",
    ".obj",
}

MAX_FILE_SIZE = 50 * 1024  # 50KB per file
MAX_TOTAL_FILE_SIZE = 200 * 1024  # 200KB total

# ── Global state ─────────────────────────────────────────────────────────

store = _get_store(DATA_DIR / "memories.db")

# Initialise OpenTelemetry metrics
init_metrics()


@asynccontextmanager
async def _lifespan(server):
    """Bootstrap the store on startup (async-safe for both SQLite and Postgres)."""
    result = store.bootstrap()
    if inspect.isawaitable(result):
        await result
    yield


mcp = FastMCP(MCP_SERVER_NAME, lifespan=_lifespan)
bifrost = BifrostClient(base_url=BIFROST_BASE_URL, timeout=BIFROST_TIMEOUT)
session_log = store._log if hasattr(store, "_log") else store
memory_store = store._mem if hasattr(store, "_mem") else store

dream_pipeline = DreamPipeline(
    db_path=DATA_DIR / "memories.db",
    bifrost_client=bifrost,
    trusted_dreamers=TRUSTED_DREAMERS,
    nats_url=NATS_URL,
    store=store,
)

ingestion_pipeline = IngestionPipeline(
    db_path=DATA_DIR / "memories.db",
    bifrost_client=bifrost,
    memory_store=memory_store,
    store=store,
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _read_files(file_paths: list[str]) -> tuple[list[str], list[str]]:
    """Read files, returning (blocks, errors)."""
    blocks: list[str] = []
    errors: list[str] = []
    total_bytes = 0

    ext_to_lang = {
        ".py": "python",
        ".ts": "typescript",
        ".js": "javascript",
        ".tsx": "typescriptreact",
        ".jsx": "javascriptreact",
        ".go": "go",
        ".rs": "rust",
        ".rb": "ruby",
        ".java": "java",
        ".kt": "kotlin",
        ".scala": "scala",
        ".c": "c",
        ".h": "c",
        ".cpp": "cpp",
        ".hpp": "cpp",
        ".cs": "csharp",
        ".swift": "swift",
        ".sh": "bash",
        ".bash": "bash",
        ".zsh": "bash",
        ".ps1": "powershell",
        ".psm1": "powershell",
        ".sql": "sql",
        ".r": "r",
        ".md": "markdown",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".toml": "toml",
        ".xml": "xml",
        ".html": "html",
        ".css": "css",
        ".dockerfile": "dockerfile",
        ".tf": "terraform",
        ".hcl": "terraform",
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


# ── Brief tool (session bootstrap) ────────────────────────────────────────


@mcp.tool()
async def brief(
    project: str | None = None,
    include_global: bool = True,
    include_index: bool = True,
) -> str:
    """Session bootstrap: load shared memories and team standards.

    When project is specified, loads memories in tiered scopes:
      - Full body: all memories tagged project:<name> + global/cross-project memories
      - Index only: other projects (title + count, no body)
    When project is None, loads all memories (legacy behaviour, limit=50).

    Also runs a freshness check on canonical memories and reports dream state.

    Args:
        project: Scope loading to this project name (e.g. "mori", "bifrost").
                 Use /brief --auto in the skill to auto-detect from git root.
        include_global: Include scope:global / scope:cross-project / profile /
                        pattern memories (default True).
        include_index: Show a count-only index of memories from other projects
                       (default True).
    """
    from datetime import datetime, timezone

    parts: list[str] = []

    # ── Scoped loading ─────────────────────────────────────────────────
    if project:
        try:
            scoped = memory_store.get_memories_by_project(project, include_global=include_global)
            proj_mems = scoped["project_memories"]
            glob_mems = scoped["global_memories"]
            other = scoped["other_projects"]

            total_other = sum(c for _, c in other)
            header = (
                f"**Mori Brief — project: {project}** "
                f"({len(proj_mems)} project + {len(glob_mems)} global memories)"
            )
            parts.append(header)

            # Project memories — canonical full body, working split by age
            from datetime import timedelta

            cutoff_dt = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d")

            if proj_mems:
                canonical = [m for m in proj_mems if m["tier"] == "canonical"]
                working_recent = [
                    m
                    for m in proj_mems
                    if m["tier"] != "canonical" and m["updated_at"][:10] >= cutoff_dt
                ]
                working_older = [
                    m
                    for m in proj_mems
                    if m["tier"] != "canonical" and m["updated_at"][:10] < cutoff_dt
                ]

                lines = ["\n**Project memories:**"]
                for m in canonical:
                    tags_str = f"[{', '.join(m['tags'])}]" if m["tags"] else ""
                    lines.append(f"- **{m['name']}**: {m['title']} (canonical) {tags_str}")
                for m in working_recent:
                    tags_str = f"[{', '.join(m['tags'])}]" if m["tags"] else ""
                    lines.append(f"- **{m['name']}**: {m['title']} (working) {tags_str}")
                for m in working_older:
                    lines.append(
                        f"- **{m['name']}**: {m['title']} — "
                        f"{m['description'] or m['updated_at'][:10]} (working, >14d)"
                    )
                parts.append("\n".join(lines))

            # Global memories
            if glob_mems:
                lines = ["\n**Global memories:**"]
                for m in glob_mems:
                    lines.append(f"- **{m['name']}**: {m['title']} ({m['type']})")
                parts.append("\n".join(lines))

            # Other-project index
            if include_index and other:
                idx_parts = []
                for name, count in other[:8]:
                    idx_parts.append(f"{name} ({count})")
                parts.append(
                    f"\n*{total_other} memories from other projects: "
                    f"{', '.join(idx_parts)} — /pensieve to explore*"
                )

        except Exception as e:
            parts.append(f"**Memories:** error loading scoped brief ({e})")
            project = None  # fall through to unscoped

    # ── Unscoped loading (legacy behaviour) ────────────────────────────
    if not project:
        try:
            memories = memory_store.list(limit=50)
            mem_count = 0
            if memories and "No memories" not in memories:
                mem_count = sum(
                    1 for line in memories.split("\n") if line.strip().startswith("- **")
                )
            parts.append(f"**Shared memories:** {mem_count} loaded")
        except Exception as e:
            parts.append(f"**Shared memories:** error loading ({e})")

    # Freshness check on canonical infrastructure memories
    try:
        fc = memory_store.check_freshness(bifrost.consult, limit=20)
        if fc["checked"] > 0:
            parts.append(
                f"**Freshness check:** {fc['fresh']} fresh, {fc['stale']} stale, {fc['no']} invalid ({fc['checked']} checked)"
            )
            if fc.get("errors"):
                parts.append(f"  ({fc['errors']} errors)")
    except Exception as e:
        parts.append(f"**Freshness check:** error ({e})")

    # Eviction queue warning
    try:
        unresolved = store.eviction_count()
        if unresolved > 0:
            parts.append(
                f"**⚠ Eviction queue:** {unresolved} unresolved item(s) — "
                f"run `memory_review` to inspect"
            )
    except Exception:
        pass

    # Load standards
    try:
        standards = memory_store.list(type_filter="standard", limit=50)
        std_count = 0
        categories: dict[str, int] = {}
        if standards and "No memories" not in standards:
            for line in standards.split("\n"):
                line = line.strip()
                if line.startswith("- **"):
                    std_count += 1
                    # Extract category tag: "security-baseline (standard) [standard, security]"
                    if "[" in line:
                        tags = line.split("[")[1].split("]")[0]
                        for tag in tags.split(","):
                            tag = tag.strip()
                            if tag and tag != "standard":
                                categories[tag] = categories.get(tag, 0) + 1
        parts.append(f"**Team standards:** {std_count} loaded")
        if categories:
            parts.append(
                "  Categories: " + ", ".join(f"{k}={v}" for k, v in sorted(categories.items()))
            )
    except Exception as e:
        parts.append(f"**Team standards:** error loading ({e})")

    # Goals summary — show unresolved requirements per project
    try:
        goal_rows = store.get_unresolved_goals()
        if goal_rows:
            projects: dict[str, list[str]] = {}
            for row in goal_rows:
                proj = "general"
                for t in row.get("tags", []):
                    if t.startswith("project-"):
                        proj = t[len("project-") :]
                        break
                projects.setdefault(proj, []).append(f"{row['name']}: {row['title']}")
            parts.append("\n**Unresolved goals:**")
            for proj, items in sorted(projects.items()):
                parts.append(f"  {proj}: {len(items)} unresolved")
    except Exception:
        pass

    # State-of-play
    try:
        from mori_advisor.dream import DreamPipeline

        dp = DreamPipeline(db_path=DATA_DIR / "memories.db", bifrost_client=bifrost, store=store)
        status = await dp.get_status()
        parts.append(f"**Dream state:** {status}")
    except Exception:
        pass

    return "\n".join(parts)


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
            standards = memory_store.search(query=None, tag=focus, type_filter="standard", limit=10)
            if standards and "No memories" not in standards:
                user_prompt += f"\n\n## Relevant {focus} standards\n{standards}"
        except Exception:
            pass

    max_tokens = {"quick": 2048, "balanced": 8192, "deep": 16384}.get(depth, 8192)

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
        return "No standards directory configured (set MORI_STANDARDS_DIR)."

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
                tier="canonical",
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
    """Re-import all standards from MORI_STANDARDS_DIR.

    Only trusted dreamers can call this. After reload, call
    memory_list with type_filter=standard to see what's available.
    """
    return import_standards()


@mcp.tool()
async def key_generate(name: str) -> str:
    """Generate a new API key secret for a named client.

    The output line should be added to MORI_API_KEYS in the server's .env,
    and the secret stored on the client side (e.g. in ~/.claude/.secrets).
    The server must be restarted to pick up new keys.
    """
    secret = generate_key()
    return f"Add to server MORI_API_KEYS: {name}:{secret}"


# ── Pensieve tool ─────────────────────────────────────────────────────────


@mcp.tool()
async def pensieve(
    query: str = "",
    type_filter: str | None = None,
    tag: str | None = None,
    client: str | None = None,
    since: str | None = None,
    limit: int = 10,
) -> str:
    """Search and browse shared memories. Centralised replacement for the /pensieve skill.

    Takes parsed arguments and returns display-ready output.
    Present this output directly to the user without summarising or rephrasing.

    To read a specific memory by name, pass 'read' as the query followed by the name
    (e.g. query="read decision-bifrost-timeout").

    Args:
        query: Search keyword, or "read <name>" to read a specific memory.
        type_filter: Filter by type: project, decision, pattern, profile, standard.
        tag: Filter by tag name (partial match).
        client: Filter by client hostname.
        since: Time filter — "7d" (last 7 days), "30d", or ISO date.
        limit: Max results (default 10, max 50).
    """
    if query.startswith("read "):
        name = query[5:].strip()
        if not name:
            return "Usage: /pensieve read <memory-name>"
        return memory_store.read(name)

    return memory_store.search(
        query=query if query else None,
        type_filter=type_filter,
        tag=tag,
        client=client,
        since=since,
        limit=min(limit, 50),
    )


# ── Update tool (device profile commands) ────────────────────────────────


def _load_device_profiles() -> dict:
    """Load device profiles from MORI_DEVICES_CONFIG (path to JSON file).

    Example JSON structure:
    {
      "nuc": {"hostname": "my-nuc", "family": "linux",
              "profiles": [".claude"], "shell": "bash"}
    }
    """
    config_path = os.environ.get("MORI_DEVICES_CONFIG", "")
    if not config_path:
        return {}
    try:
        import json as _json

        with open(config_path) as f:
            return _json.load(f)
    except Exception as e:
        logger.warning("Could not load MORI_DEVICES_CONFIG %s: %s", config_path, e)
        return {}


DEVICE_PROFILES = _load_device_profiles()


def _list_skills() -> list[str]:
    """List available skill packages from the server's skills directory."""
    if not SKILLS_DIR:
        return []
    skills_dir = Path(SKILLS_DIR)
    if not skills_dir.is_dir():
        return []
    return sorted(p.name for p in skills_dir.iterdir() if p.is_dir() and (p / "SKILL.md").exists())


def _update_all(device: str) -> str:
    """Generate a single shell command to deploy ALL skill packages to a device."""
    skills = _list_skills()
    if not skills:
        return "No skills available on server."
    cfg = DEVICE_PROFILES.get(device)
    if not cfg:
        return f"Unknown device '{device}'."

    profiles = cfg["profiles"]
    parts = []

    if cfg["family"] == "linux":
        for sk in skills:
            skill_path = Path(SKILLS_DIR) / sk / "SKILL.md"
            if not skill_path.exists():
                continue
            content = skill_path.read_text(encoding="utf-8")
            tmp = f"/tmp/_{sk}_skill.md"
            parts.append(f"cat > {tmp} << 'SKILLEOF'\n{content}\nSKILLEOF")
            for p in profiles:
                parts.append(f"mkdir -p ~/{p}/skills/{sk}")
                parts.append(f"cp {tmp} ~/{p}/skills/{sk}/SKILL.md")
            parts.append(f"rm -f {tmp}")
        joined = "\n".join(parts)
        return (
            f"**{device} — bash ({len(skills)} packages, {len(profiles)} profiles)**"
            f"\n\n```bash\n{joined}\n```"
        )

    # Windows — PowerShell
    for sk in skills:
        skill_path = Path(SKILLS_DIR) / sk / "SKILL.md"
        if not skill_path.exists():
            continue
        content = skill_path.read_text(encoding="utf-8")
        temp = f"$env:TEMP\\_{sk}_skill.md"
        parts.append(f'Set-Content -Path "{temp}" -Value @"{content}"@ -Encoding UTF8')
        for p in profiles:
            parts.append(
                f'New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\\{p}\\skills\\{sk}" | Out-Null'
            )
            parts.append(f'Copy-Item "{temp}" "$env:USERPROFILE\\{p}\\skills\\{sk}\\SKILL.md"')
        parts.append(f'Remove-Item "{temp}"')
    joined = "; ".join(parts)
    return (
        f"**{device} — PowerShell ({len(skills)} packages, {len(profiles)} profiles)**"
        f"\n\n```powershell\n{joined}\n```"
    )


@mcp.tool()
async def update(device: str, content: str = "", skill: str = "") -> str:
    """Generate device-specific commands to update a SKILL.md file.

    Knows each device's profile layout and outputs the exact shell or
    PowerShell command to apply the update. Run this on NUC, then paste
    the output on the target device.

    If content is omitted, the server reads SKILL.md from its local skills
    directory. Pass --package (skill name) to pick one; if omitted, all
    available packages are listed.

    Args:
        device: Target device — nuc, twiggy, ux3405, or cb14p.
        content: The full content of the SKILL.md file (omit to read from server).
        skill: Package/skill name (e.g. dream, consult, pensieve, brief, nats).
    """
    device = device.lower().strip()
    if device not in DEVICE_PROFILES:
        available = ", ".join(sorted(DEVICE_PROFILES.keys()))
        return f"Unknown device '{device}'. Available: {available}"

    cfg = DEVICE_PROFILES[device]

    # Resolve content: passed in or read from server skills dir
    if not content and not skill:
        available = _list_skills()
        if not available:
            return "No skills available on server."
        return (
            f"**Available packages for {device}:**\n\n"
            + "\n".join(f"- `{s}`" for s in available)
            + f"\n\nRun `/update --{device} --<package>` to deploy one."
        )

    if not content:
        if not skill:
            return "Either provide content or specify --package <name>."
        if skill == "all":
            return _update_all(device)
        if not SKILLS_DIR:
            return "Server skills directory not configured (set MORI_SKILLS_DIR)."
        skill_path = Path(SKILLS_DIR) / skill / "SKILL.md"
        if not skill_path.exists():
            available = _list_skills()
            return f"Package '{skill}' not found. Available: {', '.join(available)}"
        content = skill_path.read_text(encoding="utf-8")

    # Infer skill name from frontmatter if not provided
    inferred = skill
    if not inferred:
        for line in content.split("\n"):
            line = line.strip()
            if line == "---":
                continue
            if line.startswith("name:"):
                inferred = line.split(":", 1)[1].strip()
                break

    if not inferred:
        return "Could not determine skill name. Provide it as the 'skill' argument."

    import re

    if not re.match(r"^[a-zA-Z0-9_-]+$", inferred):
        return f"Invalid skill name '{inferred}'. Skill names must match ^[a-zA-Z0-9_-]+$."

    profiles = cfg["profiles"]

    import base64

    if cfg["family"] == "windows":
        # Base64-encode content to avoid all quoting issues with backticks/single-quotes
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        profile_lines = "\n    ".join(
            f'$d = Split-Path "$env:USERPROFILE\\{p}\\skills\\{inferred}\\SKILL.md" -Parent; '
            f"if (-not (Test-Path $d)) {{ New-Item -ItemType Directory -Path $d -Force | Out-Null }}; "
            f'Set-Content -Path "$env:USERPROFILE\\{p}\\skills\\{inferred}\\SKILL.md" -Value $c -Encoding UTF8'
            for p in profiles
        )
        return (
            f"**{device} — PowerShell ({len(profiles)} profiles)**\n\n"
            f"```powershell\n"
            f'$b = "{encoded}"; $c = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($b))\n'
            f"{profile_lines}\n"
            f"```"
        )
    else:
        # Linux: write once to /tmp, copy to all profiles
        temp = f"/tmp/_{inferred}_skill.md"
        cat_cmd = f"cat > {temp} << 'SKILLEOF'\n{content}\nSKILLEOF"
        copies = [f"cp {temp} ~/{p}/skills/{inferred}/SKILL.md" for p in profiles]
        mkdirs = [f"mkdir -p ~/${{p}}/skills/{inferred}" for p in set(profiles)]
        clean = f"rm -f {temp}"
        joined = " && ".join(mkdirs + [cat_cmd] + copies + [clean])
        return (
            f"**{device} — bash ({len(profiles)} profiles)**\n\n"
            f"```bash\n{joined}\n```\n"
            f"Write temp file → copy to all {len(profiles)} profiles → cleanup."
        )


# ── NATS tools ───────────────────────────────────────────────────────────


@mcp.tool()
async def nats_pub(message: str, subject: str = "") -> str:
    """Publish a message to the NATS cross-device message bus.

    If no subject is given, publishes to cc.<hostname> automatically.
    Use this to broadcast status or task summaries to other devices.

    Args:
        message: Text message to publish.
        subject: NATS subject (e.g. cc.uk-smr-nuc15pro). Auto-derived if empty.
    """
    try:
        import json
        import socket

        import nats

        hostname = socket.gethostname()
        subj = subject or f"cc.{hostname}"
        payload = json.dumps(
            {"from": hostname, "text": message, "ts": __import__("time").time(), "type": "msg"}
        )

        nc = await nats.connect(NATS_URL)
        await nc.publish(subj, payload.encode())
        await nc.flush()
        await nc.drain()
        return f"Published to {subj}: {message}"
    except Exception as e:
        return f"NATS pub failed: {e}"


@mcp.tool()
async def nats_sub(replay: bool = False, wait: int = 2) -> str:
    """Check the NATS message bus for recent cross-device messages.

    Shows messages from all devices. Use --replay to show messages
    from the last 7 days (useful for catching up after being offline).

    Args:
        replay: If true, replay messages from the last 7 days.
        wait: Seconds to wait for new messages (default 2, max 10).
    """
    try:
        import json

        import nats

        nc = await nats.connect(NATS_URL)
        js = nc.jetstream()

        if replay:
            from nats.errors import TimeoutError as JsTimeout
            from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
            from nats.js.errors import NotFoundError

            # Ensure stream exists — try info first, create if missing (idempotent)
            try:
                await js.stream_info("cc")
            except NotFoundError:
                await js.add_stream(
                    name="cc",
                    subjects=["cc.>"],
                    max_age=7 * 86400,
                    storage="file",
                    retention="limits",
                )

            config = ConsumerConfig(
                deliver_policy=DeliverPolicy.ALL,
                ack_policy=AckPolicy.NONE,
                max_deliver=1,
            )
            psub = await js.pull_subscribe("cc.>", stream="cc", config=config)
            msgs: list[str] = []
            try:
                batch = await psub.fetch(10, timeout=min(wait, 10))
                for msg in batch:
                    try:
                        data = json.loads(msg.data.decode())
                        msgs.append(f"[{data.get('from', '?')}] {data.get('text', '')}")
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        msgs.append(f"[raw] {msg.data[:200]}")
                    await msg.ack()
            except (asyncio.TimeoutError, JsTimeout):
                pass
            finally:
                await psub.unsubscribe()

            await nc.drain()
            if msgs:
                return "\n".join(msgs)
            return "No NATS messages."

        # Core NATS live subscription (no-replay path)
        sub = await nc.subscribe("cc.>")
        await asyncio.sleep(min(wait, 10))
        await sub.unsubscribe()

        msgs = []
        try:
            while True:
                msg = await sub.next_msg(timeout=0.5)
                try:
                    data = json.loads(msg.data.decode())
                    msgs.append(f"[{data.get('from', '?')}] {data.get('text', '')}")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    msgs.append(f"[raw] {msg.data[:200]}")
        except asyncio.TimeoutError:
            pass

        await nc.drain()

        if not msgs:
            return "No NATS messages."
        output = "\n".join(msgs)
        if len(msgs) > 50:
            output = "\n".join(msgs[:50]) + f"\n... ({len(msgs) - 50} more lines)"
        return output
    except Exception as e:
        return f"NATS sub failed: {e}"


@mcp.tool()
async def nats_ping() -> str:
    """Check if the NATS message bus is reachable.

    Reports connection status to the NATS server.
    """
    try:
        import nats

        nc = await nats.connect(NATS_URL)
        info = str(nc.connected_url or NATS_URL.split("@")[-1])
        await nc.drain()
        return f"NATS server reachable at {info}"
    except Exception as e:
        return f"NATS server not reachable: {e}"


# ── Inter-agent messaging tools ──────────────────────────────────────────


@mcp.tool()
async def msg_send(
    to: str,
    type: str,
    body: str,
    reply_to: str = "",
) -> str:
    """Send a typed message to another agent or broadcast to all.

    Args:
        to: Target hostname (e.g. "nuc15pro") or "broadcast" for all agents.
        type: Message type — task, decision, question, reply, ack, done, broadcast.
        body: Message content.
        reply_to: UUID of the message being replied to (for reply/ack/done types).
    """
    try:
        from .msg import MsgType, build_message, publish_message

        valid_types: list[MsgType] = [
            "task",
            "decision",
            "question",
            "reply",
            "ack",
            "done",
            "broadcast",
        ]
        if type not in valid_types:
            return f"Unknown type '{type}'. Valid: {', '.join(valid_types)}"

        msg = build_message(
            to=to,
            type=type,  # type: ignore[arg-type]
            body=body,
            reply_to=reply_to or None,
        )
        await publish_message(NATS_URL, msg)
        return f"Sent [{type}] to {to} (id={msg.id[:8]})"
    except Exception as e:
        return f"msg_send failed: {e}"


@mcp.tool()
async def msg_recv(
    types: list[str] | None = None,
    from_agent: str = "",
    unacked: bool = False,
    include_broadcast: bool = True,
) -> str:
    """Fetch messages addressed to this agent.

    Reads from the local msg_log (populated by the mori-msg daemon).

    Args:
        types: Filter by type(s) — task, question, reply, ack, done, decision, broadcast.
        from_agent: Filter by sender hostname.
        unacked: Only return messages not yet acked or done (status=pending).
        include_broadcast: Include mori.msg.broadcast messages (default true).
    """
    try:
        import socket

        from .msg_store import MsgStore

        hostname = socket.gethostname()
        _msg_store = MsgStore(db_path=DATA_DIR / "msg.db")
        rows = _msg_store.get_pending(
            hostname=hostname,
            types=types,
            from_host=from_agent or None,
            unacked=unacked,
            include_broadcast=include_broadcast,
        )
        if not rows:
            return "No messages."

        lines = []
        for r in rows:
            ts = r["ts"][:16].replace("T", " ")
            reply_info = f" (reply_to={r['reply_to'][:8]})" if r.get("reply_to") else ""
            lines.append(
                f"[{r['type']}]  from {r['from_host']}  {ts}  status={r['status']}{reply_info}\n"
                f"  id={r['id']}\n"
                f"  {r['body']}"
            )
        return "\n\n".join(lines)
    except Exception as e:
        return f"msg_recv failed: {e}"


@mcp.tool()
async def msg_thread(id: str) -> str:
    """Get the full reply thread rooted at a message ID.

    Returns the root message and all replies in chronological order.

    Args:
        id: Root message UUID (or first 8 chars as a prefix — exact match required).
    """
    try:
        from .msg_store import MsgStore

        _msg_store = MsgStore(db_path=DATA_DIR / "msg.db")
        thread = _msg_store.get_thread(id)
        if not thread:
            return f"No message found with id={id}"

        lines = []
        for r in thread:
            ts = r["ts"][:16].replace("T", " ")
            indent = "  → " if r.get("reply_to") else ""
            lines.append(
                f"{indent}[{r['type']}]  {r['from_host']} → {r['to_host']}  {ts}\n"
                f"{indent}  id={r['id']}  status={r['status']}\n"
                f"{indent}  {r['body']}"
            )
        return "\n\n".join(lines)
    except Exception as e:
        return f"msg_thread failed: {e}"


# ── Ingestion tools ─────────────────────────────────────────────────────


@mcp.tool()
async def mori_ingest(
    source: list[str],
    type: str = "auto",
    focus: str = "all",
    tier: str = "working",
    tags: str = "",
    since: str = "",
    dry_run: bool = False,
    model: str = "",
    max_cost: float = 5.00,
    force: bool = False,
) -> str:
    """Run ingestion on source material — extract durable memories from
    PDFs, images, CC transcripts, git history, and text/code files.

    Feeds source material through the distillation model (Kimi K2.6)
    and writes structured memories to the shared store.

    Use mori-ingest_preview for a zero-cost preview of what would be parsed.
    Use mori-ingest_status to see past ingestion runs.

    Args:
        source: File or directory paths to ingest (repeatable).
        type: Source type — auto (default), transcripts, git, docs, image.
        focus: What to extract — all, decisions, architecture, conventions, gotchas.
        tier: Memory tier — working (default), canonical, ephemeral.
        tags: Comma-separated tags to apply to produced memories.
        since: Time filter for transcripts/git (e.g. "30d", "90d").
        dry_run: If true, calls the LLM but does not write anything.
        model: Override distillation model (ignored — uses dream VK).
        max_cost: Abort if estimated cost exceeds this threshold in USD.
        force: Re-ingest even if previously ingested.
    """
    parsed_tags = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    if tier not in ("working", "canonical", "ephemeral"):
        return f"Invalid tier '{tier}'. Must be working, canonical, or ephemeral."

    try:
        result = await ingestion_pipeline.ingest(
            sources=source,
            source_type=type,
            focus=focus,
            tier=tier,
            tags=parsed_tags,
            since=since,
            dry_run=dry_run,
            preview=False,
            force=force,
        )

        parts = [
            f"**Ingestion {'preview (dry-run)' if dry_run else 'complete'}**",
            f"  Sources processed: {result['sources']}",
            f"  Chunks sent:       {result['chunks']}",
            f"  Skipped (cached):  {result['skipped']}",
            f"  Errors:            {result['errors']}",
            f"  Est. cost:         ${result['cost_estimate']:.4f}",
        ]

        if dry_run:
            if result.get("memories_candidates", 0) > 0:
                parts.append(f"  Would write:       {result['memories_candidates']} memories")
            else:
                parts.append("  No memories would be written.")
            parts.append("\nRun without --dry-run to commit.")
        else:
            parts.append(f"  Memories written:  {result['memories_written']}")

        return "\n".join(parts)

    except Exception as e:
        logger.error("mori-ingest failed: %s", e)
        return f"Ingestion failed: {e}"


@mcp.tool()
async def mori_ingest_status(limit: int = 20) -> str:
    """Show ingestion log — what's been ingested, when, and how many memories.

    Args:
        limit: Maximum entries to show (default 20).
    """
    import inspect

    res = store.get_ingestion_status(limit=limit)
    return await res if inspect.isawaitable(res) else res


@mcp.tool()
async def mori_ingest_preview(
    source: list[str],
    type: str = "auto",
    since: str = "",
) -> str:
    """Preview what would be ingested — zero-cost, no LLM calls.

    Parses sources and shows chunk breakdown, token estimates, and
    expected memory counts. Use this before running mori-ingest to
    understand what you'll get and how much it will cost.

    Args:
        source: File or directory paths to preview (repeatable).
        type: Source type — auto (default), transcripts, git, docs, image.
        since: Time filter for transcripts/git (e.g. "30d").
    """
    return IngestionPipeline.preview(
        sources=source,
        source_type=type,
        since=since,
    )


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
        memories = await dream_pipeline.run(dry_run=dry_run)
        if dry_run:
            if not memories:
                return "No memories would be produced from the current events."
            return f"**Dry run: {len(memories)} memories would be written**\n\n" + "\n".join(
                f"- `{m.get('path', '?')}` [{m.get('action', '?')}] "
                f"({m.get('confidence', '?')}) — {m.get('reason', '')}"
                for m in memories
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

    Returns the same info as the old mori-dream --status command.
    """
    return await dream_pipeline.get_status()


# ── Memory CRUD tools ────────────────────────────────────────────────────


@mcp.tool()
async def memory_write(
    name: str | None = None,
    title: str = "",
    description: str = "",
    type: str = "project",
    tier: str = "working",
    body: str = "",
    tags: list[str] | None = None,
    origin_session_id: str | None = None,
    origin_session_ids: list[str] | None = None,
    origin_clients: list[str] | None = None,
) -> str:
    """Write a memory entry. Creates or updates (upserts by kebab-case name).

    If name is omitted it is auto-derived from the title.

    If the memory is protected, the write is queued as a pending write
    instead (MCP tool callers are treated as non-dreamers).

    Args:
        name: kebab-case identifier. Auto-derived from title if omitted.
        title: Human-readable title.
        description: One-line summary.
        type: Classification: project, profile, pattern, or decision.
        tier: Memory tier — ephemeral, working (default), or canonical.
        body: Markdown content body.
        tags: List of tag strings for filtering.
        origin_session_id: UUID of the creating session (legacy single-UUID field).
        origin_session_ids: JSON array of session UUIDs that contributed.
        origin_clients: JSON array of client hostnames that contributed.
    """
    return memory_store.write(
        name=name,
        title=title,
        description=description,
        type=type,
        tier=tier,
        body=body,
        tags=tags,
        origin_session_id=origin_session_id,
        origin_session_ids=origin_session_ids,
        origin_clients=origin_clients,
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
        type_filter=type_filter,
        tag=tag,
        session=session,
        client=client,
        limit=limit,
    )


@mcp.tool()
async def memory_req(
    project: str = "",
    status: str = "",
    tag: str = "",
    limit: int = 50,
) -> str:
    """List requirement memories as a delivery-status table.

    Filters by project tag (project-<name>), status tag (status-<value>),
    or arbitrary tag. Returns a grouped Markdown table with summary counts.

    Args:
        project: Project filter (e.g. "mori" to match tag project-mori).
        status: Status filter (e.g. "done" to match tag status-done).
        tag: Arbitrary tag filter (overrides project/status if provided).
        limit: Max results (default 50).
    """
    _conn = store.get_conn()
    import sqlite3

    sql = "SELECT name, title, tags, description, body FROM memories WHERE type = 'requirement'"
    params: list = []

    if tag:
        sql += " AND tags LIKE ?"
        params.append(f'%"{tag}"%')
    else:
        if project:
            sql += " AND tags LIKE ?"
            params.append(f'%"project-{project}"%')
        if status:
            sql += " AND tags LIKE ?"
            params.append(f'%"status-{status}"%')

    sql += " ORDER BY name ASC LIMIT ?"
    params.append(min(limit, 100))

    try:
        cur = _conn.execute(sql, params)
        rows = cur.fetchall()
    except sqlite3.Error as e:
        return f"Database error: {e}"
    finally:
        _conn.close()

    if not rows:
        return "No requirements found."

    lines = [
        "| Requirement | Title | Status | Project | Pri | FR/NFR | Preview |\n|---|---|---|---|---|---|---|\n"
    ]
    status_counts: dict[str, int] = {}
    for name, title, tags_raw, desc, body in rows:
        tags = store.parse_tags(tags_raw)
        status_val = "unknown"
        project_val = ""
        priority = ""
        fr_nfr = ""
        for t in tags:
            if t.startswith("status-"):
                status_val = t[7:]
            elif t.startswith("project-"):
                project_val = t[8:]
            elif t.startswith("pri-"):
                priority = t[4:]
            elif t in ("fr", "nfr"):
                fr_nfr = t.upper()
        status_counts[status_val] = status_counts.get(status_val, 0) + 1
        preview = (body or desc or "").strip().split("\n")[0][:60].replace("|", "\\|")
        title_clean = title[:40].replace("|", "\\|")
        lines.append(
            f"| {name} | {title_clean} | {status_val} "
            f"| {project_val} | {priority} | {fr_nfr} | {preview} |"
        )

    total = len(rows)
    parts: list[str] = []
    for s in ("pending", "in-progress", "done", "blocked", "unknown"):
        if s in status_counts:
            parts.append(f"{status_counts[s]} {s}")
    summary = " | ".join(parts) if parts else f"{total} total"
    lines.append(f"\n**Total**: {total} — {summary}")
    return "\n".join(lines)


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

    Returns a formatted Markdown table. Present this output directly to the user
    without summarising, rephrasing, or adding commentary — show it verbatim.

    Args:
        query: Keyword to search across name, title, description, and body.
        type_filter: Filter by type (project, profile, pattern, decision).
        tag: Filter by tag name (partial match).
        client: Filter by client hostname.
        since: Time filter — "7d" (last 7 days), "30d", or ISO date.
        limit: Max results (default 10, max 50).
    """
    return memory_store.search(
        query=query,
        type_filter=type_filter,
        tag=tag,
        client=client,
        since=since,
        limit=min(limit, 50),
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


# ── Eviction tools ────────────────────────────────────────────────────


@mcp.tool()
async def memory_review(
    orphan_days: int = 30,
    dry_run: bool = True,
) -> str:
    """Review memory health: orphans, stale canonical entries, and superseded memories.

    Produces a dashboard showing:
    - Orphans: working-tier memories not retrieved in N days
    - Stale canonical: memories flagged STALE or invalid by freshness check
    - Superseded: memories replaced by newer ones via contradiction scan
    - Eviction queue summary

    Args:
        orphan_days: Days since last retrieval to flag as orphan (default 30).
        dry_run: If True, show what would be flagged without writing to eviction_queue.
    """

    parts = ["# Memory Review Dashboard\n"]

    # 1. Orphans
    try:
        orphan_result = memory_store.scan_orphans(days=orphan_days, dry_run=dry_run)
        parts.append(orphan_result)
    except Exception as e:
        parts.append(f"Orphan scan failed: {e}")

    # 2. Stale canonical
    parts.append("\n## Stale Canonical Memories")
    try:
        _conn = store.get_conn()
        cur = _conn.execute(
            "SELECT name, title, freshness_status, freshness_checked_at FROM memories "
            "WHERE freshness_status IN ('stale', 'no') ORDER BY freshness_checked_at DESC"
        )
        rows = cur.fetchall()
        _conn.close()
        if rows:
            for name, title, status, checked_at in rows:
                parts.append(f"- **{name}**: {title} ({status}) — checked {checked_at}")
        else:
            parts.append("No stale canonical memories.")
    except Exception as e:
        parts.append(f"Error: {e}")

    # 3. Superseded
    parts.append("\n## Superseded Memories")
    try:
        superseded_rows = store.get_superseded_memories()
        if superseded_rows:
            for row in superseded_rows:
                parts.append(
                    f"- **{row['name']}**: {row['title']} → superseded by {row['superseded_by']} ({row['updated_at']})"
                )
        else:
            parts.append("No superseded memories.")
    except Exception as e:
        parts.append(f"Error: {e}")

    # 4. Eviction queue summary
    parts.append("\n## Eviction Queue")
    try:
        _conn = store.get_conn()
        cur = _conn.execute(
            "SELECT reason, COUNT(*), SUM(CASE WHEN resolved THEN 1 ELSE 0 END) "
            "FROM eviction_queue GROUP BY reason"
        )
        rows = cur.fetchall()
        _conn.close()
        if rows:
            for reason, total, resolved in rows:
                parts.append(f"- **{reason}**: {total} total, {resolved} resolved")
        else:
            parts.append("Queue is empty.")
    except Exception as e:
        parts.append(f"Error: {e}")

    return "\n".join(parts)


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
        output_dir: Absolute path to write .md files (default: /data/mori-advisor/exports/).
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


def _get_client_from_request(request: Request) -> str:
    return request.query_params.get("client", "")


def _map_hook_payload(raw: dict, client_override: str = "") -> EventLogEntry:
    hook_event = raw.get("hook_event_name", "")
    session_id = raw.get("session_id", "")

    client = client_override or raw.get("client", "")

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
    try:
        raw = await request.body()
        body = json.loads(raw.decode("utf-8", errors="replace"))
        if not body:
            return JSONResponse({"status": "error", "error": "empty body"}, status_code=400)

        client = _get_client_from_request(request)

        if "event_name" in body:
            body_client = body.get("client", "")
            entry = EventLogEntry(
                **{k: v for k, v in body.items() if k != "client"}, client=client or body_client
            )
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
        logger.info(
            "Logged event %s for session %s (id=%s)", entry.event_name, entry.session_id, row_id
        )
        return JSONResponse({"status": "accepted", "event_id": row_id}, status_code=202)
    except Exception as e:
        logger.error("Failed to log event: %s", e)
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


async def _nats_publish_git_push(payload: dict) -> None:
    """Fire-and-forget NATS publish for GitPush events."""
    try:
        import nats as _nats

        repo = payload.get("repo", "unknown")
        branch = payload.get("branch", "unknown")
        sha = payload.get("sha", payload.get("session_id", "unknown"))
        message = payload.get("message", "")
        client = payload.get("client", "unknown")

        text = f"GitPush: {repo}/{branch} {sha}"
        if message:
            text += f" — {message}"

        msg = json.dumps(
            {"from": client, "text": text, "ts": __import__("time").time(), "type": "git-push"}
        )
        nc = await _nats.connect(NATS_URL)
        await nc.publish(f"cc.{client}", msg.encode())
        await nc.flush()
        await nc.drain()
    except Exception as e:
        logger.warning("GitPush NATS publish failed: %s", e)


@mcp.custom_route("/api/events/raw", methods=["POST"])
async def log_event_raw(request: Request) -> JSONResponse:
    """Accept raw CC hook stdin JSON and map to structured event."""
    try:
        raw = await request.body()
        body = json.loads(raw.decode("utf-8", errors="replace"))
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
        logger.info(
            "Raw event %s for session %s (id=%s)", event.event_name, event.session_id, row_id
        )
        if event.event_name == "GitPush" and NATS_URL:
            await _nats_publish_git_push(body)
        return JSONResponse({"status": "accepted", "event_id": row_id}, status_code=202)
    except Exception as e:
        logger.error("Failed to log raw event: %s", e)
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@mcp.custom_route("/api/events/health", methods=["GET"])
async def events_health(request: Request) -> JSONResponse:
    """Simple health check for the event logging endpoint."""
    return JSONResponse(
        {
            "status": "ok",
            "total_events": session_log.count_events(),
        }
    )


@mcp.custom_route("/api/smoke", methods=["GET"])
async def smoke_test(request: Request) -> JSONResponse:
    """End-to-end smoke test — verifies DB, event pipeline, dream watermark, NATS, ingestion."""
    import urllib.request as _urllib

    checks: dict = {}
    critical_failed = False
    degraded = False

    async def _a(val):
        """Await val if it's a coroutine (Postgres store), else return as-is (SQLite store)."""
        import inspect

        return await val if inspect.isawaitable(val) else val

    # 1. db_read — store readable
    try:
        await _a(store.ping())
        mem_count = await _a(store.count())
        checks["db_read"] = {"status": "ok", "memory_count": mem_count}
    except Exception as e:
        checks["db_read"] = {"status": "failed", "error": str(e)}
        critical_failed = True

    # 2. db_write — integrity check + write-lock test (SQLite only; skipped for Postgres)
    _db_path = DATA_DIR / "memories.db"
    if _db_path.exists():
        try:
            import sqlite3 as _sql

            _c = _sql.connect(str(_db_path), timeout=5)
            _c.execute("PRAGMA journal_mode=WAL")
            integrity = _c.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"integrity_check returned: {integrity}")
            _c.execute("BEGIN IMMEDIATE")
            _c.execute("ROLLBACK")
            _c.close()
            checks["db_write"] = {"status": "ok"}
        except Exception as e:
            checks["db_write"] = {"status": "failed", "error": str(e)}
            critical_failed = True
    else:
        checks["db_write"] = {
            "status": "skipped",
            "detail": "Postgres backend — SQLite integrity check not applicable",
        }

    # 3. event_log — session log accessible
    try:
        total = await _a(session_log.count_events())
        checks["event_log"] = {"status": "ok", "total_events": total}
    except Exception as e:
        checks["event_log"] = {"status": "failed", "error": str(e)}
        critical_failed = True

    # 4. event_roundtrip — direct internal append, verify count increments
    try:
        before = await _a(session_log.count_events())
        await _a(
            session_log.append_event(
                session_id="smoke-test-probe",
                event_name="SmokeTest",
                client="smoke",
            )
        )
        after = await _a(session_log.count_events())
        if after != before + 1:
            raise RuntimeError(f"count did not increment: {before} → {after}")
        checks["event_roundtrip"] = {"status": "ok", "before": before, "after": after}
    except Exception as e:
        checks["event_roundtrip"] = {"status": "failed", "error": str(e)}
        critical_failed = True

    # 5. dream_watermark — pipeline state accessible
    try:
        wm_val = await _a(store.get_dream_state("last_dreamed_event_id", default="0"))
        watermark = int(wm_val) if wm_val else 0
        try:
            total_events = await _a(session_log.count_events())
            undreamed = max(0, total_events - watermark)
        except Exception:
            total_events = None
            undreamed = None
        checks["dream_watermark"] = {
            "status": "ok",
            "watermark": watermark,
            **(
                {"total_events": total_events, "undreamed": undreamed}
                if total_events is not None
                else {}
            ),
        }
    except Exception as e:
        checks["dream_watermark"] = {"status": "failed", "error": str(e)}
        critical_failed = True

    # 6. NATS — degraded only
    try:
        import asyncio

        import nats as _nats

        _nc = await asyncio.wait_for(_nats.connect(NATS_URL), timeout=2.0)
        await _nc.drain()
        checks["nats"] = {"status": "ok"}
    except Exception as e:
        checks["nats"] = {"status": "failed", "error": str(e)}
        degraded = True

    # 7. Ingestion pod — degraded only
    try:
        _urllib.urlopen("http://localhost:8969/health", timeout=3)
        checks["ingestion"] = {"status": "ok"}
    except Exception as e:
        checks["ingestion"] = {"status": "failed", "error": str(e)}
        degraded = True

    # 8. msg_daemon — degraded only (daemon may not be running on all deployments)
    try:
        from .msg_store import MsgStore

        _ms = MsgStore(db_path=DATA_DIR / "msg.db")
        msg_count = _ms.count()
        checks["msg_daemon"] = {"status": "ok", "msg_count": msg_count}
    except Exception as e:
        checks["msg_daemon"] = {"status": "failed", "error": str(e)}
        degraded = True

    # 9. auth — configuration status
    clients = configured_clients()
    checks["auth"] = (
        {"status": "ok", "clients": clients}
        if clients
        else {"status": "warn", "detail": "No API keys configured — server is open"}
    )

    overall = "failed" if critical_failed else ("degraded" if degraded else "ok")
    return JSONResponse(
        {"status": overall, "checks": checks},
        status_code=500 if critical_failed else 200,
    )


@mcp.custom_route("/api/precompact", methods=["POST"])
async def precompact(request: Request) -> JSONResponse:  # noqa: C901
    """PreCompact hook: log the event and immediately run dream pipeline synchronously.

    This endpoint is designed for the PreCompact lifecycle hook which fires before
    context compression. Unlike /api/events/raw, this also triggers a synchronous
    dream run so memories are distilled before the context window compacts.

    The dream pipeline runs synchronously — SQLite connections are thread-bound.
    PreCompact fires once per long session so blocking briefly is acceptable.
    """
    try:
        body = await request.json()
        if not body:
            return JSONResponse({"status": "skipped", "reason": "empty body"}, status_code=200)

        client = _get_client_from_request(request)
        event = _map_hook_payload(body, client_override=client)

        if not event.session_id:
            return JSONResponse({"status": "skipped", "reason": "no session_id"}, status_code=200)

        # Log the event first
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

        result = await dream_pipeline.run()
        memories_count = len(result) if result else 0
        logger.info(
            "PreCompact: dreamed session %s (event_id=%s, memories=%s)",
            event.session_id,
            row_id,
            memories_count,
        )
        return JSONResponse(
            {
                "status": "dreamed",
                "session_id": event.session_id,
                "event_id": row_id,
                "memories": memories_count,
            },
            status_code=200,
        )
    except Exception as e:
        logger.error("PreCompact failed: %s", e)
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@mcp.custom_route("/api/dream/run", methods=["GET", "POST"])
async def dream_trigger(request: Request) -> JSONResponse:
    """Cron-triggerable dream run."""
    try:
        result = await dream_pipeline.run()
        count = len(result) if result else 0
        logger.info("Cron dream: %s memories written", count)
        return JSONResponse({"status": "ok", "memories": count})
    except Exception as e:
        logger.error("Cron dream failed: %s", e)
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


# ── Observability endpoints ──────────────────────────────────────────────


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    """Liveness probe. Returns 200 if the server is running."""
    return JSONResponse({"status": "ok", "service": "mori-advisor"})


@mcp.custom_route("/ready", methods=["GET"])
async def readiness(request: Request) -> JSONResponse:
    """Readiness probe. Returns 200 if the database is accessible."""
    try:
        store.ping()
        return JSONResponse({"status": "ok", "db": "connected"})
    except Exception as e:
        return JSONResponse({"status": "error", "db": str(e)}, status_code=503)


@mcp.custom_route("/metrics", methods=["GET"])
async def metrics(request: Request) -> PlainTextResponse:
    """Prometheus metrics endpoint in OpenMetrics exposition format.

    Serves from the OTel SDK's Prometheus exporter bridge. Gauges are
    updated with current DB values on each scrape so the output is always
    consistent with the store state.
    """
    try:
        from prometheus_client import REGISTRY as prom_registry
        from prometheus_client import generate_latest

        # Push current DB values onto the global OTel instruments
        memories_gauge.set(memory_store.count())
        events_counter.set(session_log.count_events())
        pending_writes_gauge.set(memory_store.pending_count())
        eviction_queue_gauge.set(memory_store.eviction_count())

        data = generate_latest(prom_registry)
        return PlainTextResponse(data.decode("utf-8"))
    except Exception:
        # Fallback: hand-rolled text when Prometheus bridge not installed
        lines = [
            "# HELP mori_up Was the last query successful",
            "# TYPE mori_up gauge",
            "mori_up 1",
            "# HELP mori_memories_total Total number of memories in the store",
            "# TYPE mori_memories_total gauge",
            f"mori_memories_total {memory_store.count()}",
            "# HELP mori_events_total Total number of session events logged",
            "# TYPE mori_events_total gauge",
            f"mori_events_total {session_log.count_events()}",
            "# HELP mori_pending_writes Number of pending writes awaiting approval",
            "# TYPE mori_pending_writes gauge",
            f"mori_pending_writes {memory_store.pending_count()}",
            "# HELP mori_eviction_queue_size Number of unresolved eviction queue entries",
            "# TYPE mori_eviction_queue_size gauge",
            f"mori_eviction_queue_size {memory_store.eviction_count()}",
        ]
        return PlainTextResponse("\n".join(lines) + "\n")


# ── Entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Structured JSON logging for GCP Cloud Logging
    if os.environ.get("GCE_METADATA_HOST"):
        import json as _json
        import sys as _sys

        class GCPJsonFormatter(logging.Formatter):
            def format(self, record):
                entry = {
                    "severity": record.levelname,
                    "message": record.getMessage(),
                    "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%fZ"),
                }
                if record.exc_info and record.exc_info[0]:
                    entry["exception"] = self.formatException(record.exc_info)
                return _json.dumps(entry)

        handler = logging.StreamHandler(_sys.stdout)
        handler.setFormatter(GCPJsonFormatter())
        logging.basicConfig(level=logging.INFO, handlers=[handler])
    else:
        logging.basicConfig(level=logging.INFO)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if STANDARDS_DIR:
        logger.info("Standards directory: %s", STANDARDS_DIR)
        result = import_standards()
        logger.info(result)
    init_auth()
    from starlette.middleware import Middleware

    from mori_advisor.middleware import ApiKeyMiddleware

    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=8968,
        log_level="info",
        middleware=[Middleware(ApiKeyMiddleware)],
    )
