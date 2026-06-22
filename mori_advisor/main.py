"""Mori — FastMCP server providing shared memory, consult, and dream tools.

Routes model calls through either a Bifrost gateway (VK-based) or a
direct OpenAI-compatible provider. Controlled by MORI_PROVIDER_MODE env var.

Usage:
    MORI_PROVIDER_MODE=direct MORI_API_KEY=sk-... python -m mori_advisor.main
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import inspect
import json
import logging
import os
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastmcp import FastMCP
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from mori_advisor import canon_export
from mori_advisor.auth import configured_clients, generate_key, init_auth
from mori_advisor.bifrost_client import BifrostClient
from mori_advisor.dream import DreamPipeline
from mori_advisor.ingestion import IngestionPipeline
from mori_advisor.metrics import (
    collect_metrics,
    events_counter,
    eviction_queue_gauge,
    init_metrics,
    memories_gauge,
    metrics_content_type,
    pending_writes_gauge,
    record_brief_injection,
)
from mori_advisor.policy import PermissionDenied, current_actor, require_role
from mori_advisor.store import get_store as _get_store
from mori_advisor.throttle import (
    IdempotencyState,
    idempotency_ttls,
    make_idempotency_store,
    throttle_safety_warning,
)

logger = logging.getLogger(__name__)


def _load_user_env() -> None:
    """Load ~/.config/mori/env before module-level config is read.

    Enables Homebrew / local installs to configure mori via a plain env file
    without touching the system environment.  Uses os.environ.setdefault so
    variables already set in the process environment (e.g. from a Docker
    --env-file or a systemd EnvironmentFile) always take priority.

    Only called when the module is run as __main__ (i.e. `python -m
    mori_advisor.main`), never when imported — so tests and the ingestion
    server are not affected.
    """
    env_path = Path.home() / ".config" / "mori" / "env"
    if not env_path.is_file():
        return
    try:
        with env_path.open() as fh:
            for raw in fh:
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    os.environ.setdefault(key.strip(), val.strip())
    except OSError:
        pass  # unreadable config is not fatal


if __name__ == "__main__":
    _load_user_env()

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

# Set to "false" to suppress consult output capture (e.g. for sensitive/experimental questions)
CONSULT_CAPTURE = os.environ.get("MORI_CONSULT_CAPTURE", "true").lower() != "false"

# Set to "false" to disable the per-memory LLM freshness check on the brief() hot path.
# The check is bounded (24h cache, 5-worker cap) and safe for single-operator/low-concurrency
# use, but operators running brief() frequently across many concurrent clients may wish to
# disable it here and schedule check_freshness() as a background task instead.
FRESHNESS_ON_BRIEF = os.environ.get("MORI_FRESHNESS_ON_BRIEF", "true").lower() != "false"

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


# ── Consult file-read security ────────────────────────────────────────────
# Paths allowed as roots for _read_files().  Defaults to cwd; can be
# overridden (os.pathsep-separated) by setting MORI_CONSULT_FILE_ROOTS.
# Each requested path is resolved and must be relative_to at least one root.
#
# WARNING: if MORI_CONSULT_FILE_ROOTS is unset the allowed root defaults to
# the process cwd (typically the repo root).  Operators should set this to a
# dedicated read-only directory in production to restrict the blast radius of
# any future path-escape bugs.
def _build_consult_roots() -> list[Path]:
    raw = os.environ.get("MORI_CONSULT_FILE_ROOTS", "")
    if raw:
        return [Path(p).resolve() for p in raw.split(os.pathsep) if p.strip()]
    return [Path.cwd().resolve()]


CONSULT_FILE_ROOTS: list[Path] = _build_consult_roots()


# Standards-specific allowed roots.  Callers that pass a ``standards_dir``
# parameter to import_standards() must keep the resolved directory inside
# one of these roots.  Defaults to STANDARDS_DIR (when set) or the same
# roots used for consult file reads.
def _build_standards_roots() -> list[Path]:
    raw_std = os.environ.get("MORI_STANDARDS_ROOTS", "")
    if raw_std:
        return [Path(p).resolve() for p in raw_std.split(os.pathsep) if p.strip()]
    # Fall back to the configured STANDARDS_DIR if available.
    std_dir = os.environ.get("MORI_STANDARDS_DIR", "")
    if std_dir:
        p = Path(std_dir).resolve()
        if p.is_dir():
            return [p]
    return CONSULT_FILE_ROOTS


STANDARDS_ROOTS: list[Path] = _build_standards_roots()


def _is_within_standards_roots(resolved: Path) -> bool:
    """Return True if *resolved* falls under at least one allowed standards root."""
    for root in STANDARDS_ROOTS:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


# Basenames (lowercased, exact match) that are always rejected regardless of
# location.  Lower-cased so the check is case-insensitive.
SENSITIVE_BASENAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".secrets",
        ".credentials",
        ".netrc",
        ".htpasswd",
        "passwd",
        "shadow",
        "sudoers",
        "id_rsa",
        "id_ecdsa",
        "id_ed25519",
        "id_dsa",
    }
)

# Prefixes (lowercased) of basenames that are always rejected.
# Covers .env, .env.local, .env.production, etc.
SENSITIVE_BASENAME_PREFIXES: tuple[str, ...] = (".env",)

# Substrings (lowercased) that, when present in the lowercased basename, mark
# the file as sensitive.
SENSITIVE_BASENAME_SUBSTRINGS: tuple[str, ...] = ("secret", "credential")

# Suffixes (lowercased) that are always rejected.
SENSITIVE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".key",
        ".pem",
        ".p12",
        ".pfx",
        ".crt",
        ".cer",
        ".der",
        ".sqlite",
        ".db",
        ".credentials",
    }
)

# Path segment (directory name) — reject any file whose resolved path passes
# through a directory with this name.
SENSITIVE_SEGMENTS: frozenset[str] = frozenset({".git", ".ssh", ".gnupg"})


def _is_sensitive_path(resolved: Path) -> bool:
    """Return True if the resolved path matches any sensitive pattern.

    All checks are case-insensitive on the basename so that ``PASSWD``,
    ``Secrets.txt``, ``.Env.local``, etc. are caught alongside their
    lower-case siblings.
    """
    name_lower = resolved.name.lower()

    # Exact basename match (case-insensitive).
    if name_lower in SENSITIVE_BASENAMES:
        return True

    # Prefix match — catches .env, .env.local, .env.production, etc.
    for prefix in SENSITIVE_BASENAME_PREFIXES:
        if name_lower.startswith(prefix):
            return True

    # Substring match — catches Secrets.txt, my-credentials.json, etc.
    for substr in SENSITIVE_BASENAME_SUBSTRINGS:
        if substr in name_lower:
            return True

    # Suffix match (already lowercased).
    suffix = resolved.suffix.lower()
    if suffix in SENSITIVE_SUFFIXES:
        return True

    # Directory segment match (e.g. .git/config, .ssh/id_rsa).
    for part in resolved.parts:
        if part.lower() in SENSITIVE_SEGMENTS:
            return True

    return False


def _is_within_allowed_roots(resolved: Path) -> bool:
    """Return True if resolved falls under at least one allowed root."""
    for root in CONSULT_FILE_ROOTS:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


# ── Skill name validation ─────────────────────────────────────────────────
# A safe skill name is a single path component: no path separators, no `..`,
# no leading dot or absolute prefix, printable ASCII only.
_SAFE_SKILL_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _is_safe_skill_name(sk: str) -> bool:
    """Return True only if *sk* is a safe single-component skill name.

    Rejects anything containing ``/``, ``\\``, ``..``, leading dots,
    or characters outside ``[a-zA-Z0-9_-]``.
    """
    return bool(_SAFE_SKILL_RE.match(sk))


# ── Global state ─────────────────────────────────────────────────────────

store = _get_store(DATA_DIR / "memories.db")

# Initialise OpenTelemetry metrics
init_metrics()


async def _throttle_cleanup_loop():
    """Periodically evict expired idempotency records + idle rate-limit buckets."""
    from mori_advisor import middleware as _mw  # lazy — avoid import cycle

    while True:
        await asyncio.sleep(300)
        try:
            await idempotency_store.cleanup()
            await _mw.rate_limit_store.cleanup()
        except Exception as exc:  # never let cleanup crash the loop
            logger.debug("throttle cleanup failed: %s", exc)


async def _orphan_scan_loop():
    """Periodically scan for stale working-tier memories and surface them for human review.

    Runs dry_run=True (flag only, no eviction queue writes) by default — the eviction
    queue is human-reviewed, so silently enqueueing rows on every scan would be
    surprising.  Set MORI_ORPHAN_SCAN_DRY_RUN=false to enable queue writes.
    Interval controlled by MORI_ORPHAN_SCAN_INTERVAL_SEC (default: daily).
    """
    interval = int(os.environ.get("MORI_ORPHAN_SCAN_INTERVAL_SEC", "86400"))
    dry_run = os.environ.get("MORI_ORPHAN_SCAN_DRY_RUN", "true").lower() != "false"
    while True:
        await asyncio.sleep(interval)
        try:
            result = await _a(memory_store.scan_orphans(days=30, dry_run=dry_run))
            logger.info("orphan scan (dry_run=%s): %s", dry_run, result)
        except Exception as exc:  # never let scan failure crash the loop
            logger.warning("orphan scan failed: %s", exc)


# ── Off-loop LLM execution ────────────────────────────────────────────────
# bifrost.consult() is the SYNCHRONOUS OpenAI client. Calling it directly in an
# async handler freezes the single-worker event loop for the whole 30-90s
# generation, taking the WHOLE server down (every session) → MCP connections drop.
# Run blocking consults off the loop, on a DEDICATED pool (so a slow /consult can't
# starve short to_thread tasks on the default pool), bounded by a semaphore
# (backpressure) and a timeout backstop. The default executor is left untouched.
_llm_executor: ThreadPoolExecutor | None = None
_llm_sem = asyncio.Semaphore(6)


async def _run_llm(fn, **kwargs):
    """Run a blocking bifrost call off the event loop, bounded by a semaphore and a
    timeout backstop. Falls back to the default executor if the lifespan never ran
    (e.g. in unit tests)."""
    loop = asyncio.get_running_loop()
    async with _llm_sem:
        return await asyncio.wait_for(
            loop.run_in_executor(_llm_executor, functools.partial(fn, **kwargs)),
            timeout=330,
        )


@asynccontextmanager
async def _lifespan(server):
    """Bootstrap the store on startup (async-safe for both SQLite and Postgres)."""
    global _llm_executor
    _llm_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="mori-llm")
    result = store.bootstrap()
    if inspect.isawaitable(result):
        await result
    warning = throttle_safety_warning()
    if warning:
        logger.warning(warning)
    # Warn operators if MORI_CONSULT_FILE_ROOTS is unset — the cwd default is
    # permissive and should be narrowed to a dedicated read-only directory in
    # production.  See _build_consult_roots() for details.
    if not os.environ.get("MORI_CONSULT_FILE_ROOTS", ""):
        logger.warning(
            "MORI_CONSULT_FILE_ROOTS is not set; consult file reads are allowed from "
            "the process cwd (%s). Set this variable to a dedicated read-only directory "
            "in production to restrict file-read access.",
            Path.cwd(),
        )
    cleanup_task = asyncio.create_task(_throttle_cleanup_loop())
    orphan_task = asyncio.create_task(_orphan_scan_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        orphan_task.cancel()
        if _llm_executor is not None:
            _llm_executor.shutdown(wait=False)


mcp = FastMCP(MCP_SERVER_NAME, lifespan=_lifespan)
bifrost = BifrostClient(base_url=BIFROST_BASE_URL, timeout=BIFROST_TIMEOUT)
session_log = store._log if hasattr(store, "_log") else store
memory_store = store._mem if hasattr(store, "_mem") else store

# Idempotency store for POST /api/memories (#23 C) — in-memory by default
# (single-instance). See throttle.make_idempotency_store for the scale-out path.
idempotency_store = make_idempotency_store()


async def _a(val):
    """Await val if it's a coroutine, else return as-is."""
    import inspect

    if inspect.isawaitable(val):
        return await val
    return val


def _mori_version() -> str:
    """Best-effort mori version for the export pin (env override → dist metadata → 'dev')."""
    v = os.environ.get("MORI_VERSION", "")
    if v:
        return v
    try:
        from importlib.metadata import PackageNotFoundError, version

        for dist in ("mori-advisor", "mori"):
            try:
                return version(dist)
            except PackageNotFoundError:
                continue
    except Exception:
        pass
    return "dev"


def _export_scope(project: str, type_filter: str, include_working: bool) -> str:
    """Human-readable export scope for the pin header."""
    bits = []
    if project:
        bits.append(f"project:{project}")
    if type_filter:
        bits.append(f"type:{type_filter}")
    if include_working:
        bits.append("+working")
    return " ".join(bits) if bits else "all"


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

        # ── Security: resolve FIRST so all checks operate on the canonical path.
        # This catches both .. traversal and symlink escapes in one step.
        try:
            resolved = path.resolve()
        except Exception as exc:
            errors.append(f"Path resolution error for {path_str}: {exc}")
            continue

        suffix = resolved.suffix.lower()

        if suffix in BINARY_EXTENSIONS:
            errors.append(f"Skipped binary file: {path_str}")
            continue

        if not _is_within_allowed_roots(resolved):
            errors.append(f"Access denied (outside allowed roots): {path_str}")
            continue

        if _is_sensitive_path(resolved):
            errors.append(f"Access denied (sensitive path): {path_str}")
            continue

        if not resolved.exists():
            errors.append(f"File not found: {path_str}")
            continue

        if not resolved.is_file():
            errors.append(f"Not a file: {path_str}")
            continue

        try:
            # O_NOFOLLOW: if a symlink is swapped in after resolve() the open
            # fails with OSError (ELOOP on Linux, ENOTSUP on macOS) instead of
            # silently following a new destination. This is defence in depth —
            # the allowlist check above already catches symlinks resolved at
            # call time, but a race between resolve() and open() is closed here.
            try:
                fd = os.open(str(resolved), os.O_RDONLY | os.O_NOFOLLOW)
            except OSError as e:
                errors.append(f"Access denied (symlink race or O_NOFOLLOW): {path_str}: {e}")
                continue
            with os.fdopen(fd, encoding="utf-8", errors="replace") as fh:
                content = fh.read()
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


async def _post_compact_brief(project: str | None = None, since: str | None = None) -> str:
    """Lightweight delta re-grounding after context compaction.

    Surfaces only what changed in shared state since `since` — new/updated
    memories, decisions superseded under you, and fresh evictions — and skips the
    static bulk: the full memory base, the standards dump, and crucially the
    per-memory LLM `check_freshness` scan. Driven by the `--post-compact` brief
    skill flag (fired by the PostCompact hook).

    `since` is the client-supplied boundary (marker file → session start →
    default window); absent, it falls back to `MORI_POST_COMPACT_WINDOW`
    (default "6h"). Best-effort, not exactly-once consumption — two memories at
    the same second straddling the boundary may be missed or repeated; for
    re-grounding that is acceptable. See `normalise_since` for boundary
    semantics.
    """
    from mori_advisor.memory_store import normalise_since

    cap = 30
    raw_since = since or os.environ.get("MORI_POST_COMPACT_WINDOW", "6h")
    try:
        since_norm = normalise_since(raw_since)
    except (ValueError, TypeError):
        raw_since = "6h"
        since_norm = normalise_since(raw_since)

    header = f"**Mori Brief (post-compact delta since {since_norm} UTC)**"
    if project:
        header += f" — project: {project}"
    parts: list[str] = [header]
    changed_any = False

    # Changed memories (new / updated) — store does the time + scope filtering
    try:
        changed = await _a(
            memory_store.get_memories_changed_since(raw_since, project=project, limit=cap + 1)
        )
        if changed:
            changed_any = True
            lines = ["\n**Changed memories:**"]
            for m in changed[:cap]:
                created = str(m.get("created_at", ""))[:19]
                marker = "+" if created and created > since_norm else "~"
                tags = m.get("tags") or []
                tag_str = f" [{', '.join(tags)}]" if tags else ""
                lines.append(f"- {marker} **{m['name']}**: {m['title']}{tag_str}")
            if len(changed) > cap:
                lines.append(
                    f"  …{len(changed) - cap} more — run a full /brief if you need the rest."
                )
            parts.append("\n".join(lines))
    except Exception as e:
        parts.append(f"**Changed memories:** error ({e})")

    # Superseded since — highest-value signal: a decision may have been overturned
    # under you mid-task. Time-filtered client-side on the exclusive [:19] bound.
    try:
        sup = await _a(store.get_superseded_memories())
        recent = [s for s in (sup or []) if str(s.get("updated_at", ""))[:19] > since_norm]
        if recent:
            changed_any = True
            lines = ["\n**⚠ Superseded since:**"]
            for s in recent[:cap]:
                lines.append(f"- **{s['name']}** → {s['superseded_by']} ({s['title']})")
            if len(recent) > cap:
                lines.append(f"  …{len(recent) - cap} more.")
            parts.append("\n".join(lines))
    except Exception:
        pass

    # Evicted since
    try:
        evicts = await _a(store.get_eviction_summary())
        recent_e = [e for e in (evicts or []) if str(e.get("detected_at", ""))[:19] > since_norm]
        if recent_e:
            changed_any = True
            lines = ["\n**Evicted since:**"]
            for e in recent_e[:cap]:
                lines.append(f"- **{e['memory_name']}**: {e['reason']}")
            if len(recent_e) > cap:
                lines.append(f"  …{len(recent_e) - cap} more.")
            parts.append("\n".join(lines))
    except Exception:
        pass

    if not changed_any:
        parts.append("\nNothing changed in shared state since last brief.")

    # Dream state one-liner (cheap) — no freshness check on this path
    try:
        from mori_advisor.dream import DreamPipeline

        dp = DreamPipeline(db_path=DATA_DIR / "memories.db", bifrost_client=bifrost, store=store)
        status = await dp.get_status()
        parts.append(f"\n**Dream state:** {status}")
    except Exception:
        pass

    return "\n".join(parts)


@mcp.tool()
async def brief(
    project: str | None = None,
    include_global: bool = True,
    include_index: bool = True,
    post_compact: bool = False,
    since: str | None = None,
    scope: str | None = None,
) -> str:
    """Session bootstrap: load shared memories and team standards.

    When project is specified, loads memories in tiered scopes:
      - Full body: all memories tagged project:<name> + explicit global/cross-project memories
      - Other projects: NOT surfaced (zero-knowledge in safe scope — see below)
    When project is None, loads only the global/transferable lane in safe scope.

    Provenance scope (the ``scope`` arg / ``MORI_BRIEF_SCOPE`` env, default "safe"):
      - "safe":  cross-project body exposure is provenance-routed. A memory reaches the
                 global lane ONLY via an explicit scope:global / scope:cross-project tag
                 (type=profile/pattern alone no longer auto-globalizes). Out-of-project
                 memories are given ZERO-KNOWLEDGE in the passive brief — not even an index
                 — because an index teaser of an out-of-scope rule induces the agent to
                 hallucinate the missing payload (use /pensieve to search other projects
                 explicitly). Unscoped brief surfaces the global lane only.
      - "all":   legacy behaviour — auto-global by type, other-project index shown, and an
                 unscoped brief lists everything (limit=50). Opt-in for solo/single-repo use.

    Also runs a freshness check on canonical memories and reports dream state.

    Args:
        project: Scope loading to this project name (e.g. "mori", "bifrost").
                 Use /brief --auto in the skill to auto-detect from git root.
        include_global: Include scope:global / scope:cross-project / profile /
                        pattern memories (default True).
        include_index: Show a count-only index of memories from other projects
                       (default True).
        post_compact: Lightweight delta re-grounding after context compaction —
                      returns only what changed since `since`, skipping the full
                      memory base, standards, and the LLM freshness check. Fired
                      by the `--post-compact` brief skill flag / PostCompact hook.
        since: Boundary for the post_compact delta — relative ("6h"/"7d") or
               ISO-8601. Ignored unless post_compact is True. Defaults to
               MORI_POST_COMPACT_WINDOW ("6h") when absent.
    """
    from datetime import datetime, timezone

    if post_compact:
        result = await _post_compact_brief(project=project, since=since)
        # MCP-tool channel: the return value is delivered into the agent's transcript by the MCP transport,
        # so delivery is CONFIRMED whenever we return substantive content.
        record_brief_injection(
            channel="mcp_tool", scope="post_compact", confirmed=bool(result and result.strip())
        )
        return result

    # Provenance scope resolution (default "safe"). safe_scope routes cross-project body
    # exposure: strict global lane + zero-knowledge out-of-scope. "all" = legacy.
    _scope = (scope or os.environ.get("MORI_BRIEF_SCOPE", "safe")).strip().lower()
    safe_scope = _scope != "all"

    parts: list[str] = []

    # ── Scoped loading ─────────────────────────────────────────────────
    if project:
        try:
            # H2 cutover: the generic scope filter replaces the special-cased oracle.
            # Proven byte-identical for legacy (NULL-scope) rows (test_parity_manifest);
            # a per-memory scope map now governs routing where present. get_memories_by_project
            # is retained as the parity oracle.
            scoped = await _a(
                memory_store.filter_by_scope(
                    project, include_global=include_global, strict_global=safe_scope
                )
            )
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

            # Other-project memories. In SAFE scope these are ZERO-KNOWLEDGE — not even an
            # index — because teasing the agent with an out-of-scope rule title induces it to
            # hallucinate the missing payload (adversarial self-injection). The most we surface
            # is a name-free pointer to the explicit search tool.
            if other:
                if safe_scope:
                    parts.append(
                        f"\n*{total_other} memories from other projects are out of scope here "
                        f"— use /pensieve to search them explicitly.*"
                    )
                elif include_index:
                    idx_parts = [f"{name} ({count})" for name, count in other[:8]]
                    parts.append(
                        f"\n*{total_other} memories from other projects: "
                        f"{', '.join(idx_parts)} — /pensieve to explore*"
                    )

        except Exception as e:
            parts.append(f"**Memories:** error loading scoped brief ({e})")
            project = None  # fall through to unscoped

    # ── Unscoped loading ───────────────────────────────────────────────
    if not project:
        try:
            if safe_scope:
                # No project context → surface only the global/transferable lane
                # (provenance-safe). Project-bound memories are withheld — there is no
                # project to scope to, and surfacing everything is the unscoped leak.
                gl = await _a(
                    memory_store.filter_by_scope(  # H2 cutover (unscoped global lane)
                        "", include_global=True, strict_global=True
                    )
                )
                glob = gl.get("global_memories", [])
                if glob:
                    lines = ["**Global memories:**"]
                    for m in glob:
                        lines.append(f"- **{m['name']}**: {m['title']} ({m['type']})")
                    parts.append("\n".join(lines))
                else:
                    parts.append("**Shared memories:** 0 global (no project scope set)")
                parts.append(
                    '*Project-scoped memories withheld — pass a project or scope="all" to include them.*'
                )
            else:
                memories = await _a(memory_store.list(limit=50))
                mem_count = 0
                if memories and "No memories" not in memories:
                    mem_count = sum(
                        1 for line in memories.split("\n") if line.strip().startswith("- **")
                    )
                parts.append(f"**Shared memories:** {mem_count} loaded")
        except Exception as e:
            parts.append(f"**Shared memories:** error loading ({e})")

    # Freshness check on canonical infrastructure memories.
    # Controlled by MORI_FRESHNESS_ON_BRIEF (default true).  The check is
    # bounded by a 24h in-memory cache and a 5-worker concurrency cap, making
    # it safe for single-operator / low-concurrency deployments.  Set
    # MORI_FRESHNESS_ON_BRIEF=false to disable and move the check to a
    # scheduled background task for higher-concurrency environments.
    if FRESHNESS_ON_BRIEF:
        try:
            fc = await _a(memory_store.check_freshness(bifrost.consult, limit=20))
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
        unresolved = await _a(store.eviction_count())
        if unresolved > 0:
            parts.append(
                f"**⚠ Eviction queue:** {unresolved} unresolved item(s) — "
                f"run `memory_review` to inspect"
            )
    except Exception:
        pass

    # Load standards
    try:
        standards = await _a(memory_store.list(type_filter="standard", limit=50))
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
        goal_rows = await _a(store.get_unresolved_goals())
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

    result = "\n".join(parts)
    record_brief_injection(
        channel="mcp_tool",
        scope=("project" if project else "unscoped"),
        confirmed=bool(result and result.strip()),
    )
    return result


# ── Consult tool ─────────────────────────────────────────────────────────


async def _capture_consult(
    question: str, focus: str, context: str, files: list, advice: str
) -> None:
    """Write a consult advisory response to the memory store as a working-tier decision.

    Uses SHA-12 of (question + focus + context_length) as the memory name so that
    the same question asked with the same focus and roughly the same context updates
    the same memory (idempotent across devices). Distinct contexts produce distinct
    memories (no advisory diversity collapse).

    Fires as a background task — never raises into the caller.
    """
    try:
        digest_input = f"{question}|{focus}|{len(context)}|{len(files)}"
        name = f"consult-{hashlib.sha256(digest_input.encode()).hexdigest()[:12]}"
        short_q = question[:80] + "..." if len(question) > 80 else question
        provenance = f"context_len={len(context)} files={len(files)}"
        body = (
            f"**Prompt:** {question}\n\n"
            f"**Focus:** {focus}  ({provenance})\n\n"
            f"**Response:**\n{advice}"
        )
        tags = ["consult", "advisor-output", f"focus:{focus}"]
        client = os.environ.get("MORI_CLIENT_HOSTNAME", "unknown")
        await _a(
            store.write(
                name=name,
                title=f"Advisor: {short_q}",
                body=body,
                type="project",
                tier="working",
                tags=tags,
                origin_clients=[client],
            )
        )
        logger.debug("consult captured: %s", name)
    except Exception as e:
        logger.warning("consult capture failed: %s", e)


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
            standards = await _a(
                memory_store.search(query=None, tag=focus, type_filter="standard", limit=10)
            )
            if standards and "No memories" not in standards:
                user_prompt += f"\n\n## Relevant {focus} standards\n{standards}"
        except Exception:
            pass

    max_tokens = {"quick": 2048, "balanced": 8192, "deep": 16384}.get(depth, 8192)

    try:
        advice = await _run_llm(
            bifrost.consult,
            system=system,
            user=user_prompt,
            vk="advisor",
            max_tokens=max_tokens,
            temperature=0.3,
        )
    except Exception as e:
        return f"Advisor call failed: {e}"

    if CONSULT_CAPTURE:
        await _capture_consult(question, focus, context, files, advice)

    return advice


# ── Standards ingestion ──────────────────────────────────────────────────


async def import_standards(standards_dir: str | None = None) -> str:
    """Import all .md files from a standards directory as protected memories.

    Each file is tagged with 'standard' and the name of its immediate
    parent directory (e.g. security/baseline.md → tags: ["standard", "security"]).

    Security: the effective standards directory must be contained inside one of
    the allowed standards roots (MORI_STANDARDS_ROOTS or MORI_STANDARDS_DIR or
    MORI_CONSULT_FILE_ROOTS).  Passing an external ``standards_dir`` that
    resolves outside these roots is rejected.  Each rglob'd file is additionally
    checked against _is_sensitive_path() as defence in depth.
    """
    src = standards_dir or STANDARDS_DIR
    if not src:
        return "No standards directory configured (set MORI_STANDARDS_DIR)."

    src_path = Path(src).resolve()
    if not _is_within_standards_roots(src_path):
        logger.warning("import_standards: rejected standards_dir outside allowed roots: %s", src)
        return (
            f"Access denied: standards directory '{src}' is outside the allowed standards roots. "
            f"Set MORI_STANDARDS_ROOTS or MORI_STANDARDS_DIR to an appropriate path."
        )

    if not src_path.is_dir():
        return f"Standards directory not found: {src}"

    imported = 0
    errors = 0
    for file_path in sorted(src_path.rglob("*.md")):
        if not file_path.is_file():
            continue

        resolved_file = file_path.resolve()

        # Defence in depth: skip any file that resolves to a sensitive path
        # (e.g. a symlink inside the standards dir pointing to .ssh/config).
        if _is_sensitive_path(resolved_file):
            logger.warning("import_standards: skipping sensitive path: %s", resolved_file)
            errors += 1
            continue

        # Derive kebab name from relative path
        rel = file_path.relative_to(src_path)
        name = str(rel.with_suffix("")).replace("/", "-").replace("_", "-")

        # Tag: always "standard" + parent directory name
        category = rel.parent.name if rel.parent.name != "." else "general"
        tags = ["standard", category]

        try:
            body = file_path.read_text(encoding="utf-8")
            await _a(
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
            )
            imported += 1
        except Exception as e:
            errors += 1
            logger.warning("Failed to import standard %s: %s", name, e)

    msg = f"Imported {imported} standards from {src}"
    if errors:
        msg += f" ({errors} errors)"
    if imported:
        await _write_audit("import", "init", "standards-batch", msg, detail=str(src_path))
    return msg


@mcp.tool()
async def standards_reload() -> str:
    """Re-import all standards from MORI_STANDARDS_DIR.

    Only trusted dreamers can call this. After reload, call
    memory_list with type_filter=standard to see what's available.
    """
    return await import_standards()


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
        return await _a(memory_store.read(name))

    return await _a(
        memory_store.search(
            query=query if query else None,
            type_filter=type_filter,
            tag=tag,
            client=client,
            since=since,
            limit=min(limit, 50),
        )
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


def _safe_skill_path(sk: str) -> Path | None:
    """Return the resolved SKILL.md path for *sk*, or None if the name is unsafe.

    Validates that:
      1. ``sk`` is a single safe path component (no ``/``, ``\\``, ``..``, etc.).
      2. The resolved path is contained within SKILLS_DIR (prevents any escape
         that might still occur if SKILLS_DIR itself is a symlink).
    Returns None (caller must skip / return "not found") if either check fails.
    """
    if not _is_safe_skill_name(sk):
        logger.warning("Skill name rejected (unsafe characters or traversal): %r", sk)
        return None
    if not SKILLS_DIR:
        return None
    skills_root = Path(SKILLS_DIR).resolve()
    candidate = (skills_root / sk / "SKILL.md").resolve()
    try:
        candidate.relative_to(skills_root)
    except ValueError:
        logger.warning("Skill path escaped SKILLS_DIR (after resolve): %r → %s", sk, candidate)
        return None
    return candidate


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
            skill_path = _safe_skill_path(sk)
            if skill_path is None or not skill_path.exists():
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
        skill_path = _safe_skill_path(sk)
        if skill_path is None or not skill_path.exists():
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
        device: Target device hostname (e.g. laptop, workstation).
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
        # Validate skill name before constructing a path — prevents directory
        # traversal via skill names like "../../etc/passwd" or "../secret".
        skill_path = _safe_skill_path(skill)
        if skill_path is None:
            return (
                f"Invalid or unsafe skill name '{skill}'. Skill names must match ^[a-zA-Z0-9_-]+$."
            )
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
        subject: NATS subject (e.g. cc.<hostname>). Auto-derived if empty.
    """
    try:
        import json
        import socket

        import nats

        hostname = socket.gethostname().split(".")[0]
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
async def nats_sub(replay: bool = True, wait: int = 2) -> str:
    """Check the NATS message bus for recent cross-device messages.

    Defaults to ``replay=True`` — the RELIABLE JetStream tail of recent
    messages. A non-replay sub is **live-tail-only**: it opens a subscription,
    waits ``wait`` seconds, and returns ONLY what arrives in that window — it
    SILENTLY MISSES anything published between polls. Always leave
    ``replay=True`` for coordination / "did I miss something?" polling; pass
    ``replay=False`` only to block-wait for a message arriving live right now.

    Args:
        replay: Reliable JetStream tail of recent messages (default True).
                False = live-tail-only (misses between-poll messages — do not
                use for catch-up polling).
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

            # Show the most recent N messages (the stream TAIL), not the oldest.
            # A fresh ephemeral consumer with DeliverPolicy.ALL re-delivered the
            # *first* N retained messages on every call, so newly published
            # messages never appeared (#32). Start from last_seq - (N-1) via
            # BY_START_SEQUENCE so the tail — including a just-published message —
            # is always returned, and the cursor advances as new messages land.
            replay_n = 10
            info = await js.stream_info("cc")
            last_seq = (info.state.last_seq or 0) if info.state else 0
            start_seq = max(1, last_seq - (replay_n - 1))
            config = ConsumerConfig(
                deliver_policy=DeliverPolicy.BY_START_SEQUENCE,
                opt_start_seq=start_seq,
                ack_policy=AckPolicy.NONE,
                max_deliver=1,
            )
            psub = await js.pull_subscribe("cc.>", stream="cc", config=config)
            msgs: list[str] = []
            try:
                batch = await psub.fetch(replay_n, timeout=min(wait, 10))
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
        to: Target hostname (e.g. "laptop") or "broadcast" for all agents.
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
        # Persist the sent message locally so msg_thread can find it by id.
        # Without this, the sender's msg_log has no record and msg_thread returns
        # "No message found" even though the message was successfully published.
        try:
            await _a(store.log_message(msg, status="sent"))
        except Exception as log_err:
            logger.warning("msg_send: failed to persist locally: %s", log_err)
        return f"Sent [{type}] to {to} (id={msg.id})"
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

        hostname = socket.gethostname().split(".")[0]
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
        thread = await _a(store.get_message_thread(id))
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
    try:
        require_role("write")
    except PermissionDenied as exc:
        return str(exc)
    actor = current_actor.get()
    actor_name = actor.key_name if actor else "mcp"
    result = await _a(
        memory_store.write(
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
    )
    await _write_audit("mcp_write", actor_name, name or title or "", body)
    return result


@mcp.tool()
async def memory_read(name: str) -> str:
    """Read a memory entry by its kebab-case name identifier.

    Args:
        name: The unique kebab-case name of the memory.
    """
    return await _a(memory_store.read(name))


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
    return await _a(
        memory_store.list(
            type_filter=type_filter,
            tag=tag,
            session=session,
            client=client,
            limit=limit,
        )
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
    try:
        rows = await _a(
            store.get_requirements(project=project, status=status, tag=tag, limit=min(limit, 100))
        )
    except Exception as e:
        return f"Database error: {e}"

    if not rows:
        return "No requirements found."

    lines = [
        "| Requirement | Title | Status | Project | Pri | FR/NFR | Preview |\n|---|---|---|---|---|---|---|\n"
    ]
    status_counts: dict[str, int] = {}
    for r in rows:
        name = r["name"]
        title = r["title"]
        tags_raw = r["tags"]
        desc = r["description"]
        body = r["body"]
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
    return await _a(
        memory_store.search(
            query=query,
            type_filter=type_filter,
            tag=tag,
            client=client,
            since=since,
            limit=min(limit, 50),
        )
    )


@mcp.tool()
async def memory_delete(name: str) -> str:
    """Soft-delete a memory entry (sets deleted_at; reversible via restore).

    Args:
        name: The unique kebab-case name of the memory to delete.
    """
    try:
        require_role("write")
    except PermissionDenied as exc:
        return str(exc)
    return await _a(store.soft_delete(name))


@mcp.tool()
async def export_canon(
    project: str = "",
    type: str = "",
    format: str = "standard",
    limit: int = 200,
    include_working: bool = False,
) -> str:
    """Export canonical memories as ONE structured Markdown doc for external review.

    Distinct from memory_export (single memory) and memory_export_all (per-file backup):
    this bundles the canon into a grouped, reproducible document for upload to an external
    model (e.g. via /consult) or dashboard download. Internal provenance/PII is stripped by
    default.

    Args:
        project: Filter to memories tagged with this project (e.g. 'mori', 'bifrost').
        type: Filter by memory type (e.g. 'decision', 'pattern', 'standard').
        format: 'standard' (readable), 'consult' (coherence-review rubric for an external
                LLM), or 'json' (raw structured).
        limit: Maximum memories to export (default 200).
        include_working: Include working-tier memories as well as canonical (default False).
    """
    import datetime as _dt

    tiers = ("canonical", "working") if include_working else ("canonical",)
    rows = await _a(memory_store.export_rows(tiers=tiers, type_filter=type, limit=limit))
    meta = {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "instance": os.environ.get("MORI_INSTANCE_URL", socket.gethostname().split(".")[0]),
        "version": _mori_version(),
        "scope": _export_scope(project, type, include_working),
    }
    return canon_export.build(rows, fmt=format, meta=meta, project=project)


@mcp.tool()
async def memory_export(name: str, output_path: str | None = None) -> str:
    """Export a memory entry to a markdown file with YAML frontmatter.

    Args:
        name: The unique kebab-case name of the memory to export.
        output_path: Absolute path for the output file (optional).
    """
    return await _a(memory_store.export(name, output_path=output_path))


# ── Versioning tools ─────────────────────────────────────────────────────


@mcp.tool()
async def memory_history(name: str, limit: int = 10) -> str:
    """List version history for a memory.

    Args:
        name: The kebab-case name of the memory.
        limit: Maximum versions to return (default 10).
    """
    return await _a(memory_store.history(name, limit=limit))


@mcp.tool()
async def memory_diff(name: str, from_version: int, to_version: int) -> str:
    """Show unified diff of body between two versions.

    Args:
        name: The kebab-case name of the memory.
        from_version: Version ID of the older version.
        to_version: Version ID of the newer version.
    """
    return await _a(memory_store.diff(name, from_version, to_version))


@mcp.tool()
async def memory_rollback(name: str, version_id: int) -> str:
    """Restore a memory to a previous version.

    Args:
        name: The kebab-case name of the memory.
        version_id: The version_id from memory_history to restore to.
    """
    try:
        require_role("write")
    except PermissionDenied as exc:
        return str(exc)
    return await _a(memory_store.rollback(name, version_id))


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
        orphan_result = await _a(memory_store.scan_orphans(days=orphan_days, dry_run=dry_run))
        parts.append(orphan_result)
    except Exception as e:
        parts.append(f"Orphan scan failed: {e}")

    # 2. Stale canonical
    parts.append("\n## Stale Canonical Memories")
    try:
        rows = await _a(store.get_stale_canonical_memories())
        if rows:
            for r in rows:
                parts.append(
                    f"- **{r['name']}**: {r['title']} ({r['freshness_status']}) — checked {r['freshness_checked_at']}"
                )
        else:
            parts.append("No stale canonical memories.")
    except Exception as e:
        parts.append(f"Error: {e}")

    # 3. Superseded
    parts.append("\n## Superseded Memories")
    try:
        superseded_rows = await _a(store.get_superseded_memories())
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
        rows = await _a(store.get_eviction_queue_summary())
        if rows:
            for r in rows:
                parts.append(f"- **{r['reason']}**: {r['total']} total, {r['resolved']} resolved")
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
    return await _a(memory_store.session_summary(session_id))


# ── Portability tools ────────────────────────────────────────────────────


@mcp.tool()
async def memory_export_all(output_dir: str | None = None) -> str:
    """Export all memories to CC auto-memory .md files with YAML frontmatter.

    Args:
        output_dir: Absolute path to write .md files (default: /data/mori-advisor/exports/).
    """
    if output_dir is None:
        output_dir = str(DATA_DIR / "exports")
    return await _a(memory_store.export_all(output_dir))


@mcp.tool()
async def memory_import(source_dir: str) -> str:
    """Import .md files with YAML frontmatter from a directory.

    Args:
        source_dir: Absolute path to directory containing .md files.
    """
    try:
        require_role("write")
    except PermissionDenied as exc:
        return str(exc)
    return await _a(memory_store.import_memories(source_dir))


# ── Trusted Dreamer tools ────────────────────────────────────────────────


@mcp.tool()
async def memory_pending_list(status: str = "pending") -> str:
    """List pending writes awaiting approval from a trusted dreamer.

    Args:
        status: Filter by status: pending, approved, or rejected.
    """
    return await _a(memory_store.pending_list(status=status))


@mcp.tool()
async def memory_approve(
    write_id: int, note: str = "", reviewer: str = "", reason: str = ""
) -> str:
    """Approve a pending write. Applies the change and records reviewer.

    Args:
        write_id: ID of the pending write from memory_pending_list.
        note: Optional review note.
        reviewer: Hostname of the reviewer (auto-detected if empty).
        reason: Optional TD decision code (too-granular|duplicate|stale|low-value|other)
                for the measurement layer; omitted → not counted toward coverage.
    """
    try:
        require_role("dreamer")
    except PermissionDenied as exc:
        return str(exc)
    result = await _a(memory_store.approve(write_id, note=note, reviewer=reviewer))
    await _write_audit(
        "approve",
        reviewer or "unknown",
        f"write:{write_id}",
        note,
        reason_code=_normalise_reason(reason),
    )
    return result


@mcp.tool()
async def memory_reject(write_id: int, note: str = "", reviewer: str = "", reason: str = "") -> str:
    """Reject a pending write without applying.

    Args:
        write_id: ID of the pending write from memory_pending_list.
        note: Optional review note.
        reviewer: Hostname of the reviewer (auto-detected if empty).
        reason: Optional TD decision code (too-granular|duplicate|stale|low-value|other)
                for the measurement layer; omitted → not counted toward coverage.
    """
    try:
        require_role("dreamer")
    except PermissionDenied as exc:
        return str(exc)
    result = await _a(memory_store.reject(write_id, note=note, reviewer=reviewer))
    await _write_audit(
        "reject",
        reviewer or "unknown",
        f"write:{write_id}",
        note,
        reason_code=_normalise_reason(reason),
    )
    return result


@mcp.tool()
async def memory_protect(name: str, domains: list[str] | None = None) -> str:
    """Toggle protection on a memory. Trusted dreamers only.

    Args:
        name: The kebab-case name of the memory to protect/unprotect.
        domains: Tag prefixes that trigger auto-protection.
    """
    try:
        require_role("dreamer")
    except PermissionDenied as exc:
        return str(exc)
    return await _a(memory_store.protect(name, domains=domains))


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
    assistant_text: str | None = None


def _get_client_from_request(request: Request) -> str:
    return request.query_params.get("client", "")


# Include the assistant's extended-thinking blocks when capturing reasoning.
# Off by default: thinking is large and frequently redacted in stored transcripts.
CAPTURE_THINKING = os.environ.get("MORI_CAPTURE_THINKING", "false").lower() == "true"

# Bounds on captured assistant reasoning (chars). The stored cap keeps the event
# row small; the dream-format cap (in dream.py) bounds the distillation prompt.
_ASSISTANT_TEXT_CAP = 4000
_ASSISTANT_TEXT_MIN = 50


def _extract_assistant_text(transcript_tail: str, capture_thinking: bool = CAPTURE_THINKING) -> str:
    """Extract the completed turn's assistant reasoning from a transcript tail.

    `transcript_tail` is the last bytes of a Claude Code .jsonl transcript (one
    turn lives at the tail). Native schema: each line has `type` and a nested
    `message.content[]` of typed blocks.

    Walks the tail to the last *user-text* line (a real prompt — `tool_result`-only
    user lines are NOT turn boundaries, so post-tool reasoning is kept), then joins
    every `assistant` `text` block after it (+ `thinking` blocks iff capture_thinking).
    Sidechain (subagent) lines are skipped. Tolerant of a truncated first line.

    Returns "" for trivial/empty turns.
    """
    if not transcript_tail:
        return ""

    # Parse lines, dropping malformed ones (the first tail line is usually partial).
    events: list[dict] = []
    for line in transcript_tail.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue

    # Find the index of the last real user prompt (a user line with a text block).
    last_user_text_idx = -1
    for i, e in enumerate(events):
        if e.get("isSidechain"):
            continue
        if e.get("type") != "user":
            continue
        content = (e.get("message") or {}).get("content")
        blocks = content if isinstance(content, list) else []
        if any(isinstance(b, dict) and b.get("type") == "text" for b in blocks):
            last_user_text_idx = i

    pieces: list[str] = []
    for e in events[last_user_text_idx + 1 :]:
        if e.get("isSidechain"):
            continue
        if e.get("type") != "assistant":
            continue
        content = (e.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text" and b.get("text"):
                pieces.append(b["text"])
            elif t == "thinking" and capture_thinking and b.get("thinking"):
                pieces.append(f"[thinking] {b['thinking']}")

    text = "\n\n".join(p.strip() for p in pieces if p and p.strip()).strip()
    if len(text) < _ASSISTANT_TEXT_MIN:
        return ""
    return text[:_ASSISTANT_TEXT_CAP]


def _map_hook_payload(raw: dict, client_override: str = "") -> EventLogEntry:
    hook_event = raw.get("hook_event_name", "")
    session_id = raw.get("session_id", "")

    client = client_override or raw.get("client", "")

    tool_input = raw.get("tool_input")
    if tool_input is not None and not isinstance(tool_input, str):
        tool_input = json.dumps(tool_input)

    # Capture the turn's assistant reasoning from a Stop event's transcript tail.
    assistant_text = None
    if hook_event == "Stop" and raw.get("transcript_tail_b64"):
        try:
            import base64

            tail = base64.b64decode(raw["transcript_tail_b64"]).decode("utf-8", errors="replace")
            assistant_text = _extract_assistant_text(tail) or None
            logger.info(
                "Stop reasoning capture: session=%s chars=%s",
                session_id,
                len(assistant_text) if assistant_text else 0,
            )
        except Exception as e:
            logger.warning("assistant-text extraction failed: %s", e)

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
        assistant_text=assistant_text,
    )


# ── Read API (REST) ──────────────────────────────────────────────────────────
# Auth-gated JSON read endpoints for NON-MCP consumers (web dashboard, monitoring,
# CI, curl) that can't easily speak the MCP protocol. ApiKeyMiddleware guards these
# automatically; CORS is handled by the middleware outside auth (see http_app build).


def _read_api_params(request: Request) -> dict:
    """Parse + clamp the common read-API query params. Pure — unit-tested directly
    (HTTP contract tests are too coarse for param edge cases)."""
    qp = request.query_params
    try:
        limit = int(qp.get("limit", 50))
    except (ValueError, TypeError):
        limit = 50
    return {
        "query": qp.get("query") or None,
        "type_filter": qp.get("type") or None,
        "tag": qp.get("tag") or None,
        "client": qp.get("client") or None,
        "since": qp.get("since") or None,
        "limit": max(1, min(limit, 200)),  # clamp to [1, 200]
    }


def _json_safe_rows(rows: list) -> list:
    """Convert datetime/date values to ISO strings so JSONResponse can serialize them.
    Postgres returns TIMESTAMPTZ as datetime; SQLite returns plain strings."""
    from datetime import date, datetime

    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, (datetime, date)):
                r[k] = v.isoformat()
    return rows


@mcp.custom_route("/", methods=["GET"])
async def dashboard_index(request: Request) -> Response:
    """Serve the bundled memory-browser dashboard at the root.

    Open path (no key — see OPEN_PATHS), so the page itself loads in a browser; its
    `/api/*` calls remain key-gated. Served same-origin, so the page targets this very
    server (the dashboard defaults its base URL to `window.location.origin`) — no
    separate static server, no cross-origin config. The file is bundled into the image
    (`COPY dashboard/` in the Dockerfile)."""
    import os

    path = os.path.join(os.path.dirname(__file__), "..", "dashboard", "index.html")
    try:
        with open(path, encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return JSONResponse({"service": "mori-advisor", "dashboard": "not bundled"})


@mcp.custom_route("/review", methods=["GET"])
async def review_page(request: Request) -> Response:
    """Serve the TD review admin page.

    Open path (no key required to load the HTML — the page's /api/* calls
    are still dreamer-key-gated). Bundled at dashboard/review.html."""
    import os

    path = os.path.join(os.path.dirname(__file__), "..", "dashboard", "review.html")
    try:
        with open(path, encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return JSONResponse({"error": "review.html not bundled"}, status_code=404)


@mcp.custom_route("/api/memories", methods=["GET"])
async def get_memories(request: Request) -> JSONResponse:
    """Ranked full-text (or recency) search over the shared memory store → JSON.

    `?query=&type=&tag=&client=&since=&limit=` → {"memories": [...], "count": N}.
    """
    try:
        p = _read_api_params(request)
        memories = await _a(
            memory_store.search_json(
                query=p["query"],
                type_filter=p["type_filter"],
                tag=p["tag"],
                client=p["client"],
                since=p["since"],
                limit=p["limit"],
            )
        )
        return JSONResponse({"memories": memories, "count": len(memories)})
    except Exception as e:
        logger.error("GET /api/memories failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/memories/{name}", methods=["GET"])
async def get_memory_detail(request: Request) -> JSONResponse:
    """Return full detail (including body + provenance) for a single memory → JSON.

    Companion lazy-load for GET /api/memories (list). Does NOT bump retrieval_count.
    Response shape: name, title, type, tier, tags, description, body, created_at,
    updated_at, origin_clients, retrieval_count, freshness_status.
    """
    name = request.path_params["name"]
    try:
        mem = await _a(memory_store.get_memory(name))
        if mem is None:
            return JSONResponse({"error": "not found", "name": name}, status_code=404)
        return JSONResponse(_json_safe_rows([mem])[0])
    except Exception as e:
        logger.error("GET /api/memories/%s failed: %s", name, e)
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/export", methods=["GET"])
async def export_canon_route(request: Request) -> Response:
    """Structured canon export for external review / dashboard download → text/markdown.

    Query: format=standard|consult|json, project, type, limit, include_working=true|false.
    Read-role (X-Api-Key enforced by middleware). Internal provenance/PII stripped by default.
    """
    import datetime as _dt

    q = request.query_params
    fmt = q.get("format", "standard")
    project = q.get("project", "")
    type_filter = q.get("type", "")
    try:
        limit = int(q.get("limit", "200"))
    except (ValueError, TypeError):
        limit = 200
    include_working = q.get("include_working", "false").lower() == "true"
    tiers = ("canonical", "working") if include_working else ("canonical",)
    try:
        rows = await _a(memory_store.export_rows(tiers=tiers, type_filter=type_filter, limit=limit))
        meta = {
            "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "instance": os.environ.get("MORI_INSTANCE_URL", socket.gethostname().split(".")[0]),
            "version": _mori_version(),
            "scope": _export_scope(project, type_filter, include_working),
        }
        out = canon_export.build(rows, fmt=fmt, meta=meta, project=project)
    except Exception as e:
        logger.error("GET /api/export failed: %s", e)
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)
    media = "application/json" if fmt == "json" else "text/markdown"
    return Response(out, media_type=media)


@mcp.custom_route("/api/events", methods=["GET"])
async def get_events(request: Request) -> JSONResponse:
    """Read session events, newest first → JSON. Append-only log — use
    `since_event_id` as a stable cursor for pagination.

    `?session_id=&since=&since_event_id=&client=&limit=` → {"events": [...], "count": N}.
    """
    try:
        qp = request.query_params
        try:
            limit = max(1, min(int(qp.get("limit", 50)), 200))
        except (ValueError, TypeError):
            limit = 50
        sid = qp.get("since_event_id")
        since_event_id = int(sid) if sid and sid.lstrip("-").isdigit() else None
        events = await _a(
            session_log.read_events(
                session_id=qp.get("session_id") or None,
                since_event_id=since_event_id,
                since=qp.get("since") or None,
                client=qp.get("client") or None,
                limit=limit,
            )
        )
        return JSONResponse({"events": _json_safe_rows(events), "count": len(events)})
    except Exception as e:
        logger.error("GET /api/events failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


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

        row_id = await _a(
            session_log.append_event(
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
                assistant_text=entry.assistant_text,
            )
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

        row_id = await _a(
            session_log.append_event(
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
                assistant_text=event.assistant_text,
            )
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
            "total_events": await _a(session_log.count_events()),
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
        await asyncio.to_thread(_urllib.urlopen, "http://localhost:8969/health", None, 3)
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
        row_id = await _a(
            session_log.append_event(
                session_id=event.session_id,
                event_name=event.event_name,
                client=event.client,
                tool_name=event.tool_name,
                tool_input=event.tool_input,
                tool_response=event.tool_output
                if hasattr(event, "tool_output")
                else event.tool_response,
                tool_error=event.tool_error,
                model=event.model,
                cwd=event.cwd,
                transcript_path=event.transcript_path,
                prompt=event.prompt,
                stop_reason=event.stop_reason,
                assistant_text=event.assistant_text,
            )
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


@mcp.custom_route("/api/git/watermark", methods=["GET"])
async def git_watermark_get(request: Request) -> JSONResponse:
    """Return the last ingested commit SHA for a given repo and ref.

    Used by post-push hooks to compute the commit range before sending
    a batch to /api/ingest/git. Scoped to (repo, ref) so pushes to
    different branches maintain independent watermarks.

    Query params:
        repo (str): Repository name.
        ref (str): Branch or ref name (e.g. "main").
    """
    repo = request.query_params.get("repo", "").strip()
    ref = request.query_params.get("ref", "").strip()
    if not repo or not ref:
        return JSONResponse({"error": "repo and ref parameters required"}, status_code=400)
    try:
        key = f"git_watermark_{repo}_{ref}"
        value = await _a(store.get_dream_state(key, default=None))
        return JSONResponse({"repo": repo, "ref": ref, "watermark": value})
    except Exception as e:
        logger.error("git_watermark_get failed: %s", e)
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@mcp.custom_route("/api/git/ingest", methods=["POST"])
async def ingest_git(request: Request) -> JSONResponse:
    """Ingest git commit messages from a post-push hook.

    Each commit is written as a working-tier project memory tagged
    project:<repo> and pusher:<client>, and logged in the ingestion_log
    for idempotency. A per-ref git watermark is stored in dream_state so
    subsequent pushes only send new commits.

    Body:
        repo (str): Repository name — used for project tag and watermark key.
        ref (str): Branch or ref name that was pushed.
        commits (list): Ordered list (oldest-first) of commit objects, each with:
            sha, short_sha, subject, author, timestamp, and optional body.
        pusher (str, optional): Client hostname.

    Response:
        status, ingested (int), skipped (int), watermark (str|null)
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    repo = (payload.get("repo") or "").strip()
    ref = (payload.get("ref") or payload.get("branch") or "main").strip()
    commits = payload.get("commits", [])
    pusher = (payload.get("pusher") or _get_client_from_request(request) or "unknown").strip()

    if not repo:
        return JSONResponse({"error": "repo field required"}, status_code=400)
    if not isinstance(commits, list):
        return JSONResponse({"error": "commits must be a list"}, status_code=400)

    ingested = 0
    skipped = 0
    watermark = None
    watermark_key = f"git_watermark_{repo}_{ref}"

    for commit in commits:
        sha = (commit.get("sha") or "").strip()
        short_sha = (commit.get("short_sha") or sha[:7]).strip()
        subject = (commit.get("subject") or "").strip()
        author = (commit.get("author") or "").strip()
        timestamp = (commit.get("timestamp") or "").strip()
        commit_body = (commit.get("body") or "").strip()

        if not sha or not subject:
            skipped += 1
            continue

        already_ingested = await _a(store.is_ingested_by_hash(sha))
        if already_ingested:
            skipped += 1
            continue

        mem_body = f"[{repo}/{ref}] {subject}"
        if commit_body:
            mem_body += f"\n\n{commit_body}"
        mem_body += f"\n\nCommit {short_sha} by {author} at {timestamp}"

        mem_name = f"commit-{repo}-{short_sha}"
        await _a(
            store.write(
                name=mem_name,
                title=subject,
                body=mem_body,
                type="project",
                tier="working",
                tags=["commit", f"project:{repo}", f"pusher:{pusher}"],
                origin_clients=[pusher],
            )
        )
        await _a(
            store.log_ingestion(
                source_path=f"git://{repo}/{ref}/{sha}",
                source_hash=sha,
                memories_written=1,
                model="none",
                focus="commit",
                tier="working",
                tags=["commit", f"project:{repo}"],
            )
        )
        ingested += 1
        watermark = sha

    if watermark:
        await _a(store.set_dream_state(watermark_key, watermark))

    logger.info(
        "Git ingestion: repo=%s ref=%s ingested=%d skipped=%d watermark=%s",
        repo,
        ref,
        ingested,
        skipped,
        watermark,
    )
    return JSONResponse(
        {"status": "ok", "ingested": ingested, "skipped": skipped, "watermark": watermark}
    )


# ── Write REST API (governed) ─────────────────────────────────────────────────
# Auth-gated write/approve/reject/delete endpoints for non-MCP consumers.
# All routes are behind ApiKeyMiddleware (automatic) + the #13 Policy:
#   POST /api/memories       → require_role("write")
#   GET  /api/pending        → require_role("write")
#   POST /api/memories/{name}/approve  → require_role("dreamer")
#   POST /api/memories/{name}/reject   → require_role("dreamer")
#   DELETE /api/memories/{name}        → require_role("dreamer")
#
# Deferred to a follow-up (#16): per-key token-bucket rate-limiting + 1 MB body
# cap in middleware, Idempotency-Key header + TTL replay cache, soft-delete
# (deleted_at), and a structured audit table.

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
_BODY_MAX_BYTES = 64 * 1024  # 64 KB


# TD decision taxonomy (measurement layer b). A reason is OPTIONAL on approve/reject
# (non-breaking): omitted → stored NULL and counts against td_reason_coverage; provided
# → normalised to the taxonomy ("other" for anything unrecognised, aligned to intake's
# rejection_reason). Coverage tells us adoption; the distribution tells us why the TD rejects.
_TD_REASONS = {"too-granular", "duplicate", "stale", "low-value", "other"}


def _normalise_reason(value) -> str:
    r = (value or "").strip().lower()
    if not r:
        return ""  # omitted → NULL → counts against coverage
    return r if r in _TD_REASONS else "other"


async def _write_audit(
    op: str, actor_name: str, name: str, body: str, *, detail: str = "", reason_code: str = ""
) -> None:
    """Emit a structured audit log line AND insert a row into write_audit.

    Fires on every governed write/approve/reject/delete after a successful store
    op.  Audit insert failure is logged but never raised — it must not roll back
    the primary operation.  reason_code carries the TD decision taxonomy on
    approve/reject (measurement layer).
    """
    content_hash = hashlib.sha256(body.strip().encode("utf-8", errors="replace")).hexdigest()[:16]
    logger.info(
        "AUDIT op=%s actor=%s name=%s content_hash=%s reason=%s",
        op,
        actor_name,
        name,
        content_hash,
        reason_code or "-",
    )
    try:
        await _a(store.insert_audit(op, actor_name, name, content_hash, detail or "", reason_code))
    except Exception as exc:
        logger.warning("audit insert failed (op=%s name=%s): %s", op, name, exc)


def _validate_write_payload(payload: dict) -> tuple[str | None, int]:
    """Validate a POST /api/memories payload.

    Returns (error_message, status_code) on failure, or (None, 0) on success.
    Accepted fields: name, title, description, type, tier, body, tags, origin_clients.
    All other fields are rejected (unexpected-field guard).
    """
    allowed = {"name", "title", "description", "type", "tier", "body", "tags", "origin_clients"}
    unexpected = set(payload.keys()) - allowed
    if unexpected:
        return f"Unexpected fields: {', '.join(sorted(unexpected))}", 400

    name = payload.get("name", "")
    if not name or not isinstance(name, str):
        return "Field 'name' is required and must be a non-empty string", 400
    if not _NAME_RE.match(name):
        return (
            "Field 'name' must match ^[a-zA-Z0-9_-]{1,128}$ "
            "(alphanumeric, hyphens, underscores; 1–128 chars)",
            400,
        )

    body = payload.get("body", "")
    if isinstance(body, str) and len(body.encode("utf-8", errors="replace")) > _BODY_MAX_BYTES:
        return f"Field 'body' exceeds maximum size of {_BODY_MAX_BYTES // 1024} KB", 400

    tags = payload.get("tags")
    if tags is not None and not isinstance(tags, list):
        return "Field 'tags' must be a list of strings", 400
    if tags and not all(isinstance(t, str) for t in tags):
        return "Field 'tags' must contain only strings", 400

    origin_clients = payload.get("origin_clients")
    if origin_clients is not None and not isinstance(origin_clients, list):
        return "Field 'origin_clients' must be a list of strings", 400

    return None, 0


@mcp.custom_route("/api/memories", methods=["POST"])
async def post_memory(request: Request) -> JSONResponse:
    """Propose or write a memory entry (governed, tier-aware).

    Behaviour (propose-not-overwrite):
      - Name does not exist → insert as **working** tier directly.
      - Name exists and is **canonical or protected** → create a **pending
        proposal** linked to that name; the canonical row is unchanged.
      - Name exists and is **working** → idempotent update if the same actor
        owns it (origin_clients); otherwise a pending proposal.

    Required role: write.

    Body fields: name (required), title, description, type, tier, body, tags,
    origin_clients.  Unexpected fields are rejected (400).  Body max 64 KB.
    Name must match ^[a-zA-Z0-9_-]{1,128}$.
    """
    from mori_advisor.policy import PermissionDenied, current_actor, require_role

    try:
        require_role("write")
    except PermissionDenied as exc:
        return JSONResponse({"error": "Forbidden", "detail": str(exc)}, status_code=403)

    actor = current_actor.get()
    actor_name = actor.key_name if actor else "unknown"

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    if not isinstance(payload, dict):
        return JSONResponse({"error": "Body must be a JSON object"}, status_code=400)

    err, status = _validate_write_payload(payload)
    if err:
        return JSONResponse({"error": err}, status_code=status)

    async def _do_write() -> JSONResponse:
        name: str = payload["name"]
        title: str = payload.get("title", "") or ""
        description: str = payload.get("description", "") or ""
        mem_type: str = payload.get("type", "project") or "project"
        tier: str = payload.get("tier", "working") or "working"
        body: str = payload.get("body", "") or ""
        tags: list[str] = payload.get("tags") or []
        origin_clients: list[str] = payload.get("origin_clients") or [actor_name]

        try:
            # Fetch the existing memory (if any) to decide branch.
            existing = await _a(memory_store.get_memory(name))

            if existing is None:
                # New name — insert as working directly.
                result = await _a(
                    memory_store.write(
                        name=name,
                        title=title,
                        description=description,
                        type=mem_type,
                        tier=tier,
                        body=body,
                        tags=tags,
                        origin_clients=origin_clients,
                        client=actor_name,
                    )
                )
                await _write_audit("propose_new", actor_name, name, body)
                return JSONResponse(
                    {"status": "created", "name": name, "detail": result},
                    status_code=201,
                )

            existing_tier = existing.get("tier", "working")
            existing_protected = existing.get("protected", False)

            is_canonical_or_protected = existing_tier == "canonical" or existing_protected

            if is_canonical_or_protected:
                # Propose a pending write — do NOT touch the canonical row.
                # Use queue_pending_write to bypass write() which would overwrite
                # canonical rows that are not flagged protected=1.
                result = await _a(
                    memory_store.queue_pending_write(
                        name=name,
                        title=title,
                        description=description,
                        type=mem_type,
                        body=body,
                        tags=tags,
                        origin_clients=origin_clients,
                        proposed_by=actor_name,
                    )
                )
                await _write_audit("propose_pending", actor_name, name, body)
                return JSONResponse(
                    {"status": "pending", "name": name, "detail": result},
                    status_code=202,
                )

            # Working, non-protected — idempotent update if same actor owns it.
            existing_clients: list[str] = existing.get("origin_clients") or []
            if actor_name in existing_clients or not existing_clients:
                result = await _a(
                    memory_store.write(
                        name=name,
                        title=title,
                        description=description,
                        type=mem_type,
                        tier=tier,
                        body=body,
                        tags=tags,
                        origin_clients=origin_clients,
                        client=actor_name,
                    )
                )
                await _write_audit("update_working", actor_name, name, body)
                return JSONResponse(
                    {"status": "updated", "name": name, "detail": result},
                    status_code=200,
                )

            # Working memory owned by a different actor — queue as pending.
            result = await _a(
                memory_store.queue_pending_write(
                    name=name,
                    title=title,
                    description=description,
                    type=mem_type,
                    body=body,
                    tags=tags,
                    origin_clients=origin_clients,
                    proposed_by=actor_name,
                )
            )
            await _write_audit("propose_pending", actor_name, name, body)
            return JSONResponse(
                {"status": "pending", "name": name, "detail": result},
                status_code=202,
            )

        except Exception as e:
            logger.error("POST /api/memories failed: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    # ── Idempotency-Key handling (#23 C) ──────────────────────────────────────
    # No key → behave exactly as before. With a key → propose-once: a replay of
    # the same key+body returns the cached response; the write runs only once.
    idem_key = request.headers.get("Idempotency-Key")
    if not idem_key:
        return await _do_write()

    # Scope per actor; digest the validated payload so a key reused with a
    # different body is a 422, not a silent replay.
    store_key = f"{actor_name}:{idem_key}"
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    claim_ttl, cache_ttl = idempotency_ttls()
    async with idempotency_store.reserve(
        store_key, digest, claim_ttl=claim_ttl, cache_ttl=cache_ttl
    ) as reservation:
        if reservation.state is IdempotencyState.REPLAY:
            rec = reservation.record
            return JSONResponse(
                json.loads(rec.response_body),
                status_code=rec.status_code or 200,
                headers={"Idempotency-Replay": "true"},
            )
        if reservation.state is IdempotencyState.MISMATCH:
            return JSONResponse(
                {"error": "Idempotency-Key reused with a different request body"},
                status_code=422,
            )
        if reservation.state is IdempotencyState.IN_PROGRESS:
            return JSONResponse(
                {"error": "a request with this Idempotency-Key is already in progress"},
                status_code=409,
                headers={"Retry-After": str(claim_ttl)},
            )
        resp = await _do_write()
        # Cache deterministic outcomes (2xx/4xx) for replay; never cache a
        # transient 5xx — let a retry re-attempt the write.
        if resp.status_code < 500:
            await reservation.complete(resp.status_code, resp.body.decode())
        return resp


@mcp.custom_route("/api/pending", methods=["GET"])
async def get_pending(request: Request) -> JSONResponse:
    """List pending writes awaiting dreamer approval → JSON.

    Unapproved agent output is not for read-only eyes — requires write role.
    Query params: status (default "pending" — also accepts "approved", "rejected").
    """
    from mori_advisor.policy import PermissionDenied, require_role

    try:
        require_role("write")
    except PermissionDenied as exc:
        return JSONResponse({"error": "Forbidden", "detail": str(exc)}, status_code=403)

    status_filter = request.query_params.get("status", "pending")
    try:
        raw = await _a(memory_store.pending_list(status=status_filter))
        # pending_list returns a formatted string; wrap it for REST consumers.
        return JSONResponse({"status": status_filter, "summary": raw})
    except Exception as e:
        logger.error("GET /api/pending failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/pending/json", methods=["GET"])
async def get_pending_json(request: Request) -> JSONResponse:
    """List pending writes as structured JSON for the TD review UI.

    Returns a list of enriched pending-write dicts (see Deliverable 4 — #15).
    Each item includes: name, title, description, body, tier, type, tags,
    source, provenance, confidence, focus_mode, existing_body, created_at, status.

    Requires dreamer role (TD review is privileged — only trusted dreamers
    can see the full diff and approve/reject).

    Query params: status (default "pending" — also accepts "approved", "rejected").
    """
    from mori_advisor.policy import PermissionDenied, require_role

    try:
        require_role("dreamer")
    except PermissionDenied as exc:
        return JSONResponse({"error": "Forbidden", "detail": str(exc)}, status_code=403)

    status_filter = request.query_params.get("status", "pending")
    try:
        items = await _a(store.pending_list_json(status=status_filter))
        items = _json_safe_rows(items)
        # Review roll-up: group near-duplicate candidates by convention (the memory
        # name) so the TD disposes of a convention once, not N times. Additive —
        # `clusters` lists only multi-member groups (member names); `items` is unchanged.
        from mori_advisor.clustering import cluster_index

        clusters = cluster_index(items, id_field="name", name_field="name")
        return JSONResponse(
            {"status": status_filter, "count": len(items), "items": items, "clusters": clusters}
        )
    except Exception as e:
        logger.error("GET /api/pending/json failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/pending/mine", methods=["GET"])
async def get_pending_mine(request: Request) -> JSONResponse:
    """List the authenticated caller's own pending proposals.

    Scoped to the actor identified by the API key — an autonomous agent can
    read back what it proposed (and see approved/rejected outcomes) without
    ever seeing another actor's rows.  The dreamer review queue
    (``GET /api/pending/json``) remains separate and dreamer-role only.

    Requires write role.  A missing actor (no API key in api mode) returns an
    empty list rather than a 500 — the agent simply has nothing to inspect.

    Query params:
        status: ``pending`` | ``approved`` | ``rejected``.  Omit (or pass an
                empty string) to return proposals across ALL statuses so the
                agent can see outcomes, not just the live queue.
    """
    from mori_advisor.policy import PermissionDenied, current_actor, require_role

    try:
        require_role("write")
    except PermissionDenied as exc:
        return JSONResponse({"error": "Forbidden", "detail": str(exc)}, status_code=403)

    actor = current_actor.get()
    proposer: str = actor.key_name if actor else ""

    if not proposer:
        # No authenticated actor — return an empty result rather than a 500.
        return JSONResponse({"proposer": "", "status": "all", "count": 0, "items": []})

    # An empty string means "all statuses"; a named value filters to that status.
    status_filter: str = request.query_params.get("status", "")
    label = status_filter if status_filter else "all"

    try:
        items = await _a(store.pending_list_json(status=status_filter, proposed_by=proposer))
        items = _json_safe_rows(items)
        return JSONResponse(
            {"proposer": proposer, "status": label, "count": len(items), "items": items}
        )
    except Exception as e:
        logger.error("GET /api/pending/mine failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/memories/{name}/approve", methods=["POST"])
async def approve_memory(request: Request) -> JSONResponse:
    """Approve a pending write and apply it to the memory store.

    Race-safe: the underlying store uses a single connection / transaction so
    concurrent approvals cannot duplicate canonical rows.  Requires dreamer role.

    Body fields (all optional): write_id (int — approve a specific pending write),
    note (str), reviewer (str).  If write_id is omitted, the most-recent pending
    write for this name is approved.
    """
    from mori_advisor.policy import PermissionDenied, current_actor, require_role

    try:
        require_role("dreamer")
    except PermissionDenied as exc:
        return JSONResponse({"error": "Forbidden", "detail": str(exc)}, status_code=403)

    actor = current_actor.get()
    actor_name = actor.key_name if actor else "unknown"
    mem_name: str = request.path_params["name"]

    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    write_id: int | None = payload.get("write_id")
    note: str = payload.get("note", "") or ""
    reviewer: str = payload.get("reviewer", "") or actor_name

    if write_id is not None and not isinstance(write_id, int):
        return JSONResponse({"error": "'write_id' must be an integer"}, status_code=400)

    # If write_id not provided, look up the most-recent pending write for this name.
    if write_id is None:
        return JSONResponse(
            {"error": "write_id is required — fetch from GET /api/pending and provide it"},
            status_code=400,
        )

    try:
        result = await _a(
            memory_store.approve(
                write_id=write_id,
                note=note,
                reviewer=reviewer,
            )
        )
        if "not found" in result.lower() or "already processed" in result.lower():
            return JSONResponse({"error": result}, status_code=404)
        await _write_audit(
            "approve",
            actor_name,
            mem_name,
            note,
            reason_code=_normalise_reason(payload.get("reason")),
        )
        return JSONResponse({"status": "approved", "name": mem_name, "detail": result})
    except Exception as e:
        logger.error("POST /api/memories/%s/approve failed: %s", mem_name, e)
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/memories/{name}/reject", methods=["POST"])
async def reject_memory(request: Request) -> JSONResponse:
    """Reject a pending write without applying it.

    Requires dreamer role.

    Body fields (all optional): write_id (int), note (str), reviewer (str).
    """
    from mori_advisor.policy import PermissionDenied, current_actor, require_role

    try:
        require_role("dreamer")
    except PermissionDenied as exc:
        return JSONResponse({"error": "Forbidden", "detail": str(exc)}, status_code=403)

    actor = current_actor.get()
    actor_name = actor.key_name if actor else "unknown"
    mem_name: str = request.path_params["name"]

    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    write_id: int | None = payload.get("write_id")
    note: str = payload.get("note", "") or ""
    reviewer: str = payload.get("reviewer", "") or actor_name

    if write_id is not None and not isinstance(write_id, int):
        return JSONResponse({"error": "'write_id' must be an integer"}, status_code=400)

    if write_id is None:
        return JSONResponse(
            {"error": "write_id is required — fetch from GET /api/pending and provide it"},
            status_code=400,
        )

    try:
        result = await _a(
            memory_store.reject(
                write_id=write_id,
                note=note,
                reviewer=reviewer,
            )
        )
        if "not found" in result.lower() or "already processed" in result.lower():
            return JSONResponse({"error": result}, status_code=404)
        await _write_audit(
            "reject",
            actor_name,
            mem_name,
            note,
            reason_code=_normalise_reason(payload.get("reason")),
        )
        return JSONResponse({"status": "rejected", "name": mem_name, "detail": result})
    except Exception as e:
        logger.error("POST /api/memories/%s/reject failed: %s", mem_name, e)
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/memories/{name}", methods=["DELETE"])
async def delete_memory_rest(request: Request) -> JSONResponse:
    """Soft-delete a memory entry by name (sets deleted_at).

    Add ?hard=true to permanently purge the row.  Both require dreamer role.
    Soft-deleted memories are invisible to all reads but can be restored via
    POST /api/memories/{name}/restore.
    """
    from mori_advisor.policy import PermissionDenied, current_actor, require_role

    try:
        require_role("dreamer")
    except PermissionDenied as exc:
        return JSONResponse({"error": "Forbidden", "detail": str(exc)}, status_code=403)

    actor = current_actor.get()
    actor_name = actor.key_name if actor else "unknown"
    mem_name: str = request.path_params["name"]
    hard = request.query_params.get("hard", "").lower() in ("true", "1", "yes")

    try:
        if hard:
            result = await _a(store.hard_delete(mem_name))
            op = "hard_delete"
        else:
            result = await _a(store.soft_delete(mem_name))
            op = "soft_delete"

        if "not found" in result.lower():
            return JSONResponse({"error": "not found", "name": mem_name}, status_code=404)
        await _write_audit(op, actor_name, mem_name, "")
        status_word = "hard_deleted" if hard else "soft_deleted"
        return JSONResponse({"status": status_word, "name": mem_name, "detail": result})
    except Exception as e:
        logger.error("DELETE /api/memories/%s failed: %s", mem_name, e)
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/memories/{name}/restore", methods=["POST"])
async def restore_memory_rest(request: Request) -> JSONResponse:
    """Restore a soft-deleted memory.

    If an active memory already holds the same name, the restored row is
    renamed to {name}_restored_{ts}.  Requires dreamer role.
    """
    from mori_advisor.policy import PermissionDenied, current_actor, require_role

    try:
        require_role("dreamer")
    except PermissionDenied as exc:
        return JSONResponse({"error": "Forbidden", "detail": str(exc)}, status_code=403)

    actor = current_actor.get()
    actor_name = actor.key_name if actor else "unknown"
    mem_name: str = request.path_params["name"]

    try:
        final_name, msg = await _a(store.restore_memory(mem_name))
        if "not found" in msg.lower() or "not deleted" in msg.lower():
            return JSONResponse({"error": msg, "name": mem_name}, status_code=404)
        if "failed" in msg.lower():
            return JSONResponse({"error": msg, "name": mem_name}, status_code=409)
        op = "restore_renamed" if final_name != mem_name else "restore"
        await _write_audit(op, actor_name, final_name, "", detail=f"original={mem_name}")
        return JSONResponse({"status": "restored", "name": final_name, "detail": msg})
    except Exception as e:
        logger.error("POST /api/memories/%s/restore failed: %s", mem_name, e)
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/audit", methods=["GET"])
async def get_audit_log_rest(request: Request) -> JSONResponse:
    """Return recent write_audit entries.

    Query params:
        memory_name — filter by memory name (optional)
        actor       — filter by actor key name (optional)
        limit       — max rows, default 100, max 500

    Requires dreamer role.
    """
    from mori_advisor.policy import PermissionDenied, require_role

    try:
        require_role("dreamer")
    except PermissionDenied as exc:
        return JSONResponse({"error": "Forbidden", "detail": str(exc)}, status_code=403)

    memory_name = request.query_params.get("memory_name", "")
    actor_filter = request.query_params.get("actor", "")
    try:
        limit = min(int(request.query_params.get("limit", "100")), 500)
    except ValueError:
        limit = 100

    try:
        rows = await _a(
            store.get_audit_log(memory_name=memory_name, actor=actor_filter, limit=limit)
        )
        rows = _json_safe_rows(rows)
        return JSONResponse({"count": len(rows), "items": rows})
    except Exception as e:
        logger.error("GET /api/audit failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Observability endpoints ──────────────────────────────────────────────


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    """Liveness probe. Returns 200 if the server is running."""
    return JSONResponse({"status": "ok", "service": "mori-advisor"})


@mcp.custom_route("/ready", methods=["GET"])
async def readiness(request: Request) -> JSONResponse:
    """Readiness probe. Returns 200 if the database is accessible."""
    try:
        await _a(store.ping())
        return JSONResponse({"status": "ok", "db": "connected"})
    except Exception as e:
        return JSONResponse({"status": "error", "db": str(e)}, status_code=503)


@mcp.custom_route("/metrics", methods=["GET"])
async def metrics(request: Request) -> Response:
    """Prometheus metrics endpoint.

    Serves metrics collected directly from the memory store, message store,
    session events, and NATS in standard Prometheus text exposition format.
    """
    try:
        # Push current DB values onto the global OTel instruments if they exist
        # to preserve backwards-compatible OTel metric reporting
        if memories_gauge is not None:
            try:
                memories_gauge.set(await _a(memory_store.count()))
            except Exception:
                pass
        if events_counter is not None:
            try:
                events_counter.set(await _a(session_log.count_events()))
            except Exception:
                pass
        if pending_writes_gauge is not None:
            try:
                pending_writes_gauge.set(await _a(memory_store.pending_count()))
            except Exception:
                pass
        if eviction_queue_gauge is not None:
            try:
                eviction_queue_gauge.set(await _a(memory_store.eviction_count()))
            except Exception:
                pass
    except Exception as e:
        logger.debug("Failed to update OTel instruments: %s", e)

    try:
        data = await collect_metrics(store=store, nats_url=os.environ.get("MORI_NATS_URL"))
        return Response(content=data, media_type=metrics_content_type())
    except Exception as e:
        logger.exception("Failed to collect Prometheus metrics")
        return PlainTextResponse(f"# Error collecting metrics: {e}\n", status_code=500)


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
    import uvicorn
    from starlette.middleware import Middleware
    from starlette.middleware.cors import CORSMiddleware

    from mori_advisor.middleware import ApiKeyMiddleware

    # CORS must sit OUTSIDE the auth middleware: a browser dashboard sends an OPTIONS
    # preflight with the custom X-Api-Key header, which ApiKeyMiddleware would 401.
    # CORSMiddleware answers preflight before auth runs. Origins via MORI_CORS_ORIGINS
    # (comma-separated; default "*" — the routes are still API-key gated; no cookies).
    _cors = os.environ.get("MORI_CORS_ORIGINS", "*").strip()
    _cors_origins = ["*"] if _cors == "*" else [o.strip() for o in _cors.split(",") if o.strip()]

    # Build the ASGI app explicitly so custom_route registrations are always
    # included regardless of FastMCP version or Python version.
    app = mcp.http_app(
        transport="streamable-http",
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=_cors_origins,
                allow_methods=["GET", "OPTIONS"],
                allow_headers=["x-api-key", "content-type"],
            ),
            Middleware(ApiKeyMiddleware),
        ],
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("APP_PORT") or os.environ.get("PORT", "8968")),
        log_level="info",
        lifespan="on",
    )
