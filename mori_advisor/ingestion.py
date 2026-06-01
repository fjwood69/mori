"""Ingestion pipeline — feed source material through Kimi K2.6 to extract
durable memories. Follows the same distillation pattern as the dream pipeline
but operates on files instead of session events.

Three tiers of execution:
  1. Preview  (preview=True):  parse-only, zero-cost. Chunk stats only.
  2. Dry run  (dry_run=True):  full pipeline with LLM calls, but no DB writes.
  3. Ingest   (both False):   full pipeline, commits everything.

Content-based ingestion (v0.1.4):
  `ingest_content()` accepts base64-encoded file bytes sent over the wire,
  for remote clients where the server can't access the filesystem.
  Both `ingest()` and `ingest_content()` build IngestionJob lists and
  pass them to the shared `_run_pipeline()` execution engine.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from mori_advisor.bifrost_client import BifrostClient
from mori_advisor.memory_store import MemoryStore
from mori_advisor.parsers import (
    CONTENT_SIZE_CEILING,
    Chunk,
    get_parser,
    is_binary,
)
from mori_advisor.parsers.exceptions import (
    ParserDependencyError,
)
from mori_advisor.parsers.text_parser import parse_directory as parse_text_directory
from mori_advisor.utils import parse_model_json_response, run_contradiction_scan

logger = logging.getLogger(__name__)

# Token estimation: text uses ~4 chars/token, images use a flat overhead
CHARS_PER_TOKEN = 4
TOKEN_OVERHEAD_PER_IMAGE = 85  # rough — varies by resolution, model specifics

# Default pricing (Kimi K2.6 via Moonshot, approximate USD)
DEFAULT_INPUT_PRICE_PER_1K = 0.00015
DEFAULT_OUTPUT_PRICE_PER_1K = 0.00060
ESTIMATED_OUTPUT_TOKENS_PER_CHUNK = 4096  # conservative estimate for JSON response

INGESTION_SYSTEM_PROMPT = """You are the Archivist. Extract durable knowledge, decisions, patterns, and conventions from the provided source material into structured memories.

Output a JSON array of memory objects:
- name: unique kebab-case identifier (lowercase, hyphens)
- title: human-readable title (one line)
- description: one-line summary
- body: 2-6 lines of markdown with context, implications, and rationale
- tier: \"canonical\" or \"working\"
- tags: list of relevant categories (architecture, decisions, conventions, gotchas, patterns, etc.)
- confidence: 0.0 to 1.0

Capture: architectural decisions, coding conventions, project patterns, gotchas, configuration rationale, domain knowledge, deferred decisions.

Ignore: one-off comments, noise, boilerplate, personal information, anything recoverable from docs or git without distillation.

If nothing meaningful is found, return an empty array [].

CRITICAL: Begin your response with [ and end with ]. No prose before or after the JSON."""


FOCUS_GUIDANCE: dict[str, str] = {
    "all": "",
    "decisions": "Focus on decisions: extract design choices, trade-offs discussed, and rationale for technical direction. Prioritise architectural decisions and rejected alternatives.",
    "architecture": "Focus on architecture: extract system design, component relationships, data flow patterns, and infrastructure decisions. Look for diagrams, design docs, and structural patterns.",
    "conventions": "Focus on conventions: extract coding standards, naming patterns, workflow conventions, and team practices. Look for style guides, linting rules, and established practices.",
    "gotchas": "Focus on gotchas: extract pitfalls, workarounds, edge cases, and things that surprised the team. Look for bug reports, troubleshooting notes, and hard-won lessons.",
}


# ---- Tiered MIME routing table ----
# Maps MIME types to parser source_type names registered with @register_parser.
# Order matters: more specific types listed first.
MIME_ROUTING_TABLE: list[tuple[str, str]] = [
    ("application/pdf", "pdf"),
    ("image/png", "image"),
    ("image/jpeg", "image"),
    ("image/jpg", "image"),
    ("image/webp", "image"),
    ("image/gif", "image"),
    ("text/x-git-log", "git"),
]

# text/* fallback types for explicit routing
TEXT_FALLBACK_PREFIXES = ["text/"]


class CostExceededError(Exception):
    """Estimated cost exceeds the configured maximum."""


@dataclass
class IngestionJob:
    """A unit of work for the shared ingestion pipeline.

    Both filesystem ingest() and wire ingest_content() build lists of
    these and pass them to _run_pipeline().
    """

    decoded_bytes: bytes
    source_uri: str  # "content:<name>" or filesystem path
    mime_hint: str | None  # From caller, None = auto-detect
    provenance: str  # "filesystem" | "content"
    file_hash: str  # SHA256 of decoded_bytes
    size_bytes: int  # Raw size


class IngestionPipeline:
    """Pipeline that feeds source material through the distillation model
    and writes the resulting structured memories.

    All state is in the shared SQLite database (memories.db).
    """

    def __init__(
        self,
        db_path: str | Path,
        bifrost_client: BifrostClient,
        memory_store: MemoryStore,
        max_cost: float = 5.00,
        model_override: str | None = None,
        store=None,
    ):
        self.db_path = Path(db_path)
        self.client = bifrost_client
        self.memory_store = memory_store
        self.max_cost = max_cost
        self.model_override = model_override
        if store is None:
            from mori_advisor.store.sqlite_store import SQLiteStore
            store = SQLiteStore(self.db_path)
        self._store = store

    # ── Public API ─────────────────────────────────────────────────────────

    def ingest(
        self,
        sources: list[str],
        source_type: str = "auto",
        focus: str = "all",
        tier: str = "working",
        tags: list[str] | None = None,
        since: str = "",
        dry_run: bool = False,
        preview: bool = False,
        force: bool = False,
    ) -> dict:
        """Run the ingestion pipeline on one or more source paths.

        Args:
            sources: File or directory paths to ingest.
            source_type: "auto" or explicit parser type.
            focus: What to extract — "all", "decisions", "architecture",
                   "conventions", "gotchas".
            tier: Memory tier — "working", "canonical", "ephemeral".
            tags: Extra tags to apply to produced memories.
            since: Time filter for transcripts/git (e.g. "30d").
            dry_run: If True, calls the LLM but does not write memories.
            preview: If True, parse-only — no LLM calls, no writes.
            force: If True, re-ingest even if previously ingested.

        Returns:
            Summary dict with keys: sources, chunks, memories_written,
            skipped, errors, cost_estimate, dry_run, preview.
        """
        all_tags = tags or []
        focus_guidance = FOCUS_GUIDANCE.get(focus, "")
        total_chunks = 0
        total_written = 0
        total_skipped = 0
        total_errors = 0
        total_cost = 0.0
        all_memories: list[dict] = []

        source_paths = self._expand_sources(sources)

        for path in source_paths:
            try:
                # Dedup check
                if not force and self._is_ingested_by_hash(
                    self._hash_file(path), status_filter="committed"
                ):
                    logger.info("Skipping already-ingested source: %s", path)
                    total_skipped += 1
                    continue

                # Parse
                parser = get_parser(
                    path, explicit_type=source_type if source_type != "auto" else None
                )
                if parser is None:
                    if path.is_dir():
                        chunks = parse_text_directory(path)
                    else:
                        logger.warning("No parser for: %s", path)
                        total_errors += 1
                        continue
                else:
                    chunks = parser.parse(path, since=since)

                if not chunks:
                    logger.info("No content extracted from: %s", path)
                    continue

                total_chunks += len(chunks)

                # Preview mode: stop here, just report stats
                if preview:
                    cost = self._estimate_cost(chunks)
                    total_cost += cost
                    continue

                # Cost check
                cost = self._estimate_cost(chunks)
                total_cost += cost
                if cost > self.max_cost:
                    raise CostExceededError(
                        f"Estimated cost ${cost:.2f} exceeds max ${self.max_cost:.2f}. "
                        f"Use --max-cost to raise the limit or split the source into smaller batches."
                    )

                # Distill each chunk batch
                batch_memories = self._distill_batch(chunks, focus_guidance, tier, all_tags)
                total_written += len(batch_memories)
                all_memories.extend(batch_memories)

                # Write memories (unless dry run)
                if not dry_run and batch_memories:
                    self._write_memories(batch_memories, tier, all_tags)
                    self._record_ingestion(
                        path,
                        len(batch_memories),
                        focus,
                        tier,
                        all_tags,
                        dry_run=False,
                        status="committed",
                    )

                # Contradiction scan (unless dry run)
                if not dry_run and batch_memories:
                    try:
                        self._contradiction_scan(batch_memories)
                    except Exception as e:
                        logger.warning("Contradiction scan failed: %s", e)

            except CostExceededError as e:
                logger.warning("Cost exceeded for %s: %s", path, e)
                total_errors += 1
            except ParserDependencyError as e:
                logger.warning("Dependency missing for %s: %s", path, e)
                total_errors += 1
            except Exception as e:
                logger.error("Failed to ingest %s: %s", path, e)
                total_errors += 1

        return {
            "sources": len(source_paths),
            "chunks": total_chunks,
            "memories_written": total_written if not dry_run and not preview else 0,
            "memories_candidates": len(all_memories) if dry_run else 0,
            "skipped": total_skipped,
            "errors": total_errors,
            "cost_estimate": round(total_cost, 4),
            "dry_run": dry_run,
            "preview": preview,
        }

    def ingest_content(
        self,
        files: list[dict],
        focus: str = "all",
        tier: str = "working",
        tags: list[str] | None = None,
        dry_run: bool = False,
        preview: bool = False,
        force: bool = False,
    ) -> dict:
        """Ingest source material from base64-encoded file bytes.

        For remote clients where the server can't access the filesystem.
        Files are decoded, parsed into chunks, then run through the shared
        _run_pipeline() execution engine.

        Args:
            files: List of dicts with keys: name, content_b64, mime_type.
            focus: What to extract — "all", "decisions", "architecture",
                   "conventions", "gotchas".
            tier: Memory tier — "working", "canonical", "ephemeral".
            tags: Extra tags to apply to produced memories.
            dry_run: If True, calls the LLM but does not write memories.
            preview: If True, parse-only — no LLM calls, no writes.
            force: If True, re-ingest even if previously ingested.

        Returns:
            Summary dict matching ingest() output format.
        """
        all_tags = tags or []

        # Decode and validate files
        jobs: list[IngestionJob] = []
        errors: list[str] = []

        for f in files:
            name = f.get("name", "unknown")
            content_b64 = f.get("content_b64", "")
            mime_type = f.get("mime_type", "")

            if not content_b64:
                errors.append(f"Empty content_b64 for {name}")
                continue

            # Pre-decode size check to prevent OOM from malicious payloads
            # Base64 expands input by ~1.37x, so raw length * 3/4 ≈ decoded bytes
            raw_len = len(content_b64)
            estimated_decoded = (raw_len * 3) // 4
            if estimated_decoded > CONTENT_SIZE_CEILING:
                errors.append(
                    f"Content too large for {name}: ~{estimated_decoded} bytes estimated "
                    f"(max {CONTENT_SIZE_CEILING})"
                )
                continue

            try:
                decoded = base64.b64decode(content_b64)
            except Exception as e:
                errors.append(f"Base64 decode failed for {name}: {e}")
                continue

            size = len(decoded)
            if size > CONTENT_SIZE_CEILING:
                errors.append(
                    f"Content too large for {name}: {size} bytes (max {CONTENT_SIZE_CEILING})"
                )
                continue

            file_hash = self._hash_bytes(decoded)
            uri = f"content:{name}"

            # Dedup check (committed entries only — preview/failed don't block)
            if not force and self._is_ingested_by_hash(file_hash, status_filter="committed"):
                logger.info("Skipping already-ingested content: %s", name)
                continue

            jobs.append(
                IngestionJob(
                    decoded_bytes=decoded,
                    source_uri=uri,
                    mime_hint=mime_type or None,
                    provenance="content",
                    file_hash=file_hash,
                    size_bytes=size,
                )
            )

        if not jobs:
            result = {
                "sources": 0,
                "chunks": 0,
                "memories_written": 0,
                "memories_candidates": 0,
                "skipped": 0,
                "errors": len(errors),
                "cost_estimate": 0.0,
                "dry_run": dry_run,
                "preview": preview,
            }
            if errors:
                result["error_details"] = errors
            return result

        # Run through shared pipeline
        all_tags = tags or []
        focus_guidance = FOCUS_GUIDANCE.get(focus, "")
        result = self._run_pipeline(jobs, focus_guidance, tier, all_tags, dry_run, preview, force)

        if errors:
            result["error_details"] = (result.get("error_details") or []) + errors
            result["errors"] = result.get("errors", 0) + len(errors)

        return result

    # ── Shared pipeline execution engine ─────────────────────────────────────

    def _run_pipeline(
        self,
        jobs: list[IngestionJob],
        focus_guidance: str,
        tier: str,
        tags: list[str],
        dry_run: bool,
        preview: bool,
        force: bool,
    ) -> dict:
        """Execute the ingestion pipeline on a list of IngestionJobs.

        Shared by ingest() (filesystem) and ingest_content() (wire).
        Handles: parse → cost check → distill → write → contradiction scan → record.
        """
        total_chunks = 0
        total_written = 0
        total_skipped = 0
        total_errors = 0
        total_cost = 0.0
        all_memories: list[dict] = []
        error_details: list[str] = []

        for job in jobs:
            try:
                # Dedup check
                if not force and self._is_ingested_by_hash(
                    job.file_hash, status_filter="committed"
                ):
                    logger.info("Skipping already-ingested: %s", job.source_uri)
                    total_skipped += 1
                    continue

                # Parse
                parser = self._parser_for_mime(job.mime_hint, job.decoded_bytes)
                if parser is None:
                    error_details.append(f"No parser for {job.source_uri} (mime: {job.mime_hint})")
                    total_errors += 1
                    continue

                chunks = parser.parse_content(
                    job.source_uri,
                    job.decoded_bytes,
                    job.mime_hint or "application/octet-stream",
                )
                if not chunks:
                    logger.info("No content extracted from: %s", job.source_uri)
                    continue

                total_chunks += len(chunks)

                # Preview mode: stop here, just report stats
                if preview:
                    cost = self._estimate_cost(chunks)
                    total_cost += cost
                    continue

                # Cost check
                cost = self._estimate_cost(chunks)
                total_cost += cost
                if cost > self.max_cost:
                    raise CostExceededError(
                        f"Estimated cost ${cost:.2f} exceeds max ${self.max_cost:.2f}. "
                        f"Use --max-cost to raise the limit."
                    )

                # Distill
                batch_memories = self._distill_batch(chunks, focus_guidance, tier, tags)
                total_written += len(batch_memories)
                all_memories.extend(batch_memories)

                # Write (unless dry run)
                if not dry_run and batch_memories:
                    self._write_memories(batch_memories, tier, tags)
                    self._record_ingestion_by_fields(
                        source_path=job.source_uri,
                        source_hash=job.file_hash,
                        memories_written=len(batch_memories),
                        focus=focus_guidance[:20] if focus_guidance else "all",
                        tier=tier,
                        tags=tags,
                        dry_run=False,
                        status="committed",
                    )

                # Contradiction scan (unless dry run)
                if not dry_run and batch_memories:
                    try:
                        self._contradiction_scan(batch_memories)
                    except Exception as e:
                        logger.warning("Contradiction scan failed: %s", e)

            except CostExceededError as e:
                logger.warning("Cost exceeded for %s: %s", job.source_uri, e)
                error_details.append(str(e))
                total_errors += 1
            except ParserDependencyError as e:
                logger.warning("Dependency missing for %s: %s", job.source_uri, e)
                error_details.append(str(e))
                total_errors += 1
            except Exception as e:
                logger.error("Failed to ingest %s: %s", job.source_uri, e)
                error_details.append(str(e))
                total_errors += 1

        result = {
            "sources": len(jobs),
            "chunks": total_chunks,
            "memories_written": total_written if not dry_run and not preview else 0,
            "memories_candidates": len(all_memories) if dry_run else 0,
            "skipped": total_skipped,
            "errors": total_errors,
            "cost_estimate": round(total_cost, 4),
            "dry_run": dry_run,
            "preview": preview,
        }
        if error_details:
            result["error_details"] = error_details
        return result

    # ── Tiered MIME routing ─────────────────────────────────────────────────

    @staticmethod
    def _parser_for_mime(mime_hint: str | None, raw_bytes: bytes) -> object | None:
        """Resolve a parser for the given MIME type and raw bytes.

        Tiered strategy:
        1. Explicit allowlist (MIME_ROUTING_TABLE)
        2. text/x-* fallback → TextParser
        3. text/plain with byte-sniffing → TextParser
        4. Unknown binary → skip with warning
        """
        from mori_advisor.parsers.git_parser import GitParser
        from mori_advisor.parsers.image_parser import ImageParser
        from mori_advisor.parsers.pdf_parser import PdfParser
        from mori_advisor.parsers.text_parser import TextParser
        from mori_advisor.parsers.transcript_parser import TranscriptParser

        # Map parser type names to classes for lookup
        PARSER_CLASSES = {
            "text": TextParser,
            "pdf": PdfParser,
            "image": ImageParser,
            "transcripts": TranscriptParser,
            "git": GitParser,
        }

        if mime_hint:
            mime_lower = mime_hint.lower().strip()

            # Tier 1: explicit allowlist
            for pattern, parser_type in MIME_ROUTING_TABLE:
                if mime_lower.startswith(pattern):
                    cls = PARSER_CLASSES.get(parser_type)
                    if cls:
                        return cls()

            # Tier 2: text/x-* fallback
            if mime_lower.startswith("text/") and mime_lower != "text/plain":
                return TextParser()

            # Tier 3: text/plain or octet-stream — check with byte-sniffing
            if mime_lower in ("text/plain", "application/octet-stream"):
                if not is_binary(raw_bytes):
                    return TextParser()
                logger.warning("Binary content detected for mime %s — skipping", mime_hint)
                return None

        # Tier 4: no MIME hint — try to detect
        if not is_binary(raw_bytes):
            return TextParser()

        logger.warning("No MIME hint and binary content detected — skipping")
        return None

    # ── Expand sources ──────────────────────────────────────────────────────

    def _expand_sources(self, sources: list[str]) -> list[Path]:
        """Expand source strings into Paths, expanding directories into files."""
        paths: list[Path] = []
        for s in sources:
            p = Path(s).expanduser().resolve()
            if not p.exists():
                logger.warning("Source not found: %s", p)
                continue
            if p.is_dir():
                for file_path in sorted(p.rglob("*")):
                    if file_path.is_file() and not any(
                        part.startswith(".") for part in file_path.parts if part != "."
                    ):
                        paths.append(file_path)
            else:
                paths.append(p)
        return paths

    # ── Cost estimation ────────────────────────────────────────────────────

    def _estimate_cost(self, chunks: list[Chunk]) -> float:
        """Estimate distillation cost for a set of chunks.

        Text: len(content) / 4 tokens.
        Images: flat overhead per image.
        Output: conservative fixed estimate per chunk.
        """
        input_tokens = 0
        image_count = 0
        for chunk in chunks:
            if chunk.is_image:
                image_count += 1
                input_tokens += 255  # ~85 tokens * 3 for typical 3-tile image
            else:
                input_tokens += len(chunk.content) / CHARS_PER_TOKEN

        input_tokens += 500  # System prompt overhead

        input_cost = (input_tokens / 1000) * DEFAULT_INPUT_PRICE_PER_1K
        output_tokens = ESTIMATED_OUTPUT_TOKENS_PER_CHUNK * max(len(chunks), 1)
        output_cost = (output_tokens / 1000) * DEFAULT_OUTPUT_PRICE_PER_1K

        return input_cost + output_cost

    def _estimate_tokens(self, chunks: list[Chunk]) -> int:
        """Rough token count for display purposes."""
        total = 0
        for chunk in chunks:
            if chunk.is_image:
                total += 255
            else:
                total += len(chunk.content) // CHARS_PER_TOKEN
        return total + 500  # system prompt overhead

    # ── Dedup ──────────────────────────────────────────────────────────────

    def _is_ingested_by_hash(self, file_hash: str, status_filter: str | None = None) -> bool:
        """Check whether a source hash has already been ingested."""
        try:
            return self._store.is_ingested_by_hash(file_hash, status_filter=status_filter)
        except Exception:
            return False

    def _hash_bytes(self, data: bytes) -> str:
        """Compute SHA256 hash of raw bytes."""
        return hashlib.sha256(data).hexdigest()

    def _hash_file(self, path: Path) -> str:
        """Compute SHA256 hash of a file's contents."""
        if path.is_dir():
            sha = hashlib.sha256()
            for fp in sorted(path.rglob("*")):
                if fp.is_file():
                    sha.update(str(fp.relative_to(path)).encode())
                    with open(fp, "rb") as f:
                        for chunk in iter(lambda: f.read(65536), b""):
                            sha.update(chunk)
            return sha.hexdigest()
        sha = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha.update(chunk)
        return sha.hexdigest()

    # ── Distillation ───────────────────────────────────────────────────────

    def _distill_batch(
        self, chunks: list[Chunk], focus_guidance: str, tier: str, tags: list[str]
    ) -> list[dict]:
        """Send a batch of chunks to Kimi and parse the memory candidates."""
        system = INGESTION_SYSTEM_PROMPT
        if focus_guidance:
            system += "\n\n" + focus_guidance

        if tier != "working":
            system += f'\n\nSet tier to "{tier}" for all memories.'
        if tags:
            system += f"\n\nAdd these tags to every memory: {', '.join(tags)}"

        # Separate text and image chunks
        text_parts = []
        image_uris = []
        for i, chunk in enumerate(chunks):
            if chunk.is_image:
                image_uris.append(chunk.content)
            else:
                header = self._chunk_header(chunk, i + 1, len(chunks))
                text_parts.append(header + "\n" + chunk.content)

        user_text = (
            "\n\n".join(text_parts)
            if text_parts
            else "Analyse the attached image(s) and extract durable memories."
        )

        try:
            if image_uris:
                response = self.client.consult_vision(
                    system=system,
                    user_text=user_text,
                    images=image_uris,
                    vk="dream",
                    max_tokens=16384,
                    temperature=0.3,
                )
            else:
                response = self.client.consult(
                    system=system,
                    user=user_text,
                    vk="dream",
                    max_tokens=16384,
                    temperature=0.3,
                )
        except Exception as e:
            logger.error("Distillation call failed: %s", e)
            raise

        return self._parse_response(response)

    def _chunk_header(self, chunk: Chunk, idx: int, total: int) -> str:
        """Build a context header for a chunk in the user prompt."""
        meta = chunk.metadata
        source = meta.get("source_path", "?")
        parts = [f"## Source {idx}/{total}: {source}"]

        if meta.get("type") == "pdf" and meta.get("pages"):
            parts.append(f"Pages {meta['pages']} of {meta.get('total_pages', '?')}")
        elif meta.get("type") == "git" and meta.get("date_range"):
            parts.append(f"Commits: {meta.get('commit_count', '?')} ({meta['date_range']})")
        elif meta.get("session_id"):
            parts.append(f"Session: {meta['session_id']} ({meta.get('event_count', '?')} events)")
        elif meta.get("language"):
            parts.append(f"Language: {meta['language']}")

        if meta.get("part") and meta.get("part", 1) > 1:
            parts.append(f"(part {meta['part']})")

        return "\n".join(parts)

    def _parse_response(self, text: str) -> list[dict]:
        return parse_model_json_response(text)

    # ── Memory writing ─────────────────────────────────────────────────────

    def _write_memories(self, memories: list[dict], tier: str, tags: list[str]) -> None:
        """Write memory candidates to the store."""
        for mem in memories:
            if not isinstance(mem, dict):
                continue

            name = mem.get("name") or self._derive_name(mem)
            confidence = mem.get("confidence", 1.0)

            if confidence < 0.5:
                logger.debug("Skipping low-confidence memory: %s (%.2f)", name, confidence)
                continue

            mem_tags = list(tags or [])
            mem_tags.append("ingestion-phase")
            if mem.get("tags"):
                mem_tags.extend(mem["tags"])

            self.memory_store.write(
                name=name,
                title=mem.get("title", name),
                description=mem.get("description", ""),
                type=self._infer_type(name, mem),
                tier=mem.get("tier", tier),
                body=mem.get("body", ""),
                tags=mem_tags,
            )

    def _derive_name(self, mem: dict) -> str:
        title = mem.get("title", "")
        if not title:
            import time

            return f"ingested-memory-{int(time.time())}"
        return title.lower().replace(" ", "-").replace("_", "-")

    def _infer_type(self, name: str, mem: dict) -> str:
        tags = mem.get("tags", [])
        tag_set = {t.lower() for t in tags}

        if "architecture" in tag_set or "design" in tag_set:
            return "decision"
        if "convention" in tag_set or "style" in tag_set:
            return "pattern"
        if "gotcha" in tag_set:
            return "pattern"

        if name.startswith("architecture-") or name.startswith("design-"):
            return "decision"
        if name.startswith("convention-") or name.startswith("pattern-"):
            return "pattern"
        if name.startswith("gotcha-"):
            return "pattern"

        return "project"

    # ── Ingestion log ──────────────────────────────────────────────────────

    def _record_ingestion(
        self,
        source_path: Path,
        memories_written: int,
        focus: str,
        tier: str,
        tags: list[str],
        dry_run: bool,
        status: str = "committed",
    ) -> None:
        """Record an ingestion run in the log."""
        file_hash = self._hash_file(source_path)
        self._record_ingestion_by_fields(
            source_path=str(source_path),
            source_hash=file_hash,
            memories_written=memories_written,
            focus=focus,
            tier=tier,
            tags=tags,
            dry_run=dry_run,
            status=status,
        )

    def _record_ingestion_by_fields(
        self,
        source_path: str,
        source_hash: str,
        memories_written: int,
        focus: str,
        tier: str,
        tags: list[str],
        dry_run: bool,
        status: str = "committed",
    ) -> None:
        """Record ingestion — shared by filesystem and content modes."""
        try:
            self._store.log_ingestion(
                source_path=source_path,
                source_hash=source_hash,
                memories_written=memories_written,
                model="kimi-k2.6",
                focus=focus,
                tier=tier,
                tags=tags,
                dry_run=dry_run,
                status=status,
            )
        except Exception as e:
            logger.warning("Failed to record ingestion: %s", e)

    # ── Contradiction scan ─────────────────────────────────────────────────

    def _contradiction_scan(self, new_memories: list[dict]) -> int:
        def consult_fn(system, user, vk, max_tokens, temperature):
            return self.client.consult(
                system=system,
                user=user,
                vk=vk,
                max_tokens=max_tokens,
                temperature=temperature,
            )

        return run_contradiction_scan(
            new_memories=new_memories,
            db_path=self.db_path,
            consult_fn=consult_fn,
        )

    # ── Status / Preview ───────────────────────────────────────────────────

    @staticmethod
    def get_status(db_path: Path, limit: int = 20) -> str:
        """Query ingestion_log and return a formatted status table.

        DEPRECATED: use store.get_ingestion_status(limit) directly.
        """
        from mori_advisor.store.sqlite_store import SQLiteStore
        return SQLiteStore(db_path).get_ingestion_status(limit=min(limit, 100))

    @staticmethod
    def preview(
        sources: list[str],
        source_type: str = "auto",
        since: str = "",
    ) -> str:
        """Parse sources and show what would be ingested — zero-cost, no LLM.

        Returns a human-readable preview: source breakdown, chunk counts,
        token estimates, expected memory ranges.
        """
        from mori_advisor.parsers.text_parser import parse_directory as _parse_dir

        parts = ["## Ingestion Preview\n"]
        total_chunks = 0
        total_tokens = 0
        total_cost = 0.0

        for s in sources:
            path = Path(s).expanduser().resolve()
            if not path.exists():
                parts.append(f"- **{s}**: not found")
                continue

            try:
                parser = get_parser(
                    path, explicit_type=source_type if source_type != "auto" else None
                )
                if parser is None:
                    if path.is_dir():
                        chunks = _parse_dir(path)
                    else:
                        parts.append(f"- **{s}**: no parser available")
                        continue
                else:
                    chunks = parser.parse(path, since=since)
            except ParserDependencyError as e:
                parts.append(f"- **{s}**: dependency missing — {e}")
                continue
            except Exception as e:
                parts.append(f"- **{s}**: error — {e}")
                continue

            if not chunks:
                parts.append(f"- **{s}**: no extractable content")
                continue

            tokens = sum(255 if c.is_image else len(c.content) // CHARS_PER_TOKEN for c in chunks)
            cost = (tokens / 1000) * DEFAULT_INPUT_PRICE_PER_1K
            cost += (
                ESTIMATED_OUTPUT_TOKENS_PER_CHUNK * len(chunks) / 1000 * DEFAULT_OUTPUT_PRICE_PER_1K
            )

            total_chunks += len(chunks)
            total_tokens += tokens
            total_cost += cost

            parts.append(
                f"- **{path.name}**: ~{tokens:,} tokens, {len(chunks)} chunks — est. ${cost:.4f}"
            )

        parts.append(
            f"\n**Total**: {total_chunks} chunks, ~{total_tokens:,} tokens, est. ${total_cost:.4f}"
        )
        if total_cost > 0.50:
            parts.append(
                "\n**Note**: Cost estimates are approximate — image-heavy sources may vary 2–3×."
            )

        return "\n".join(parts)
