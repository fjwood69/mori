"""Ingestion pipeline — feed source material through Kimi K2.6 to extract
durable memories. Follows the same distillation pattern as the dream pipeline
but operates on files instead of session events.

Three tiers of execution:
  1. Preview  (preview=True):  parse-only, zero-cost. Chunk stats only.
  2. Dry run  (dry_run=True):  full pipeline with LLM calls, but no DB writes.
  3. Ingest   (both False):   full pipeline, commits everything.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from pathlib import Path

from mori_advisor.bifrost_client import BifrostClient
from mori_advisor.memory_store import MemoryStore
from mori_advisor.parsers import Chunk, get_parser
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


class CostExceededError(Exception):
    """Estimated cost exceeds the configured maximum."""


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
    ):
        self.db_path = Path(db_path)
        self.client = bifrost_client
        self.memory_store = memory_store
        self.max_cost = max_cost
        self.model_override = model_override

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
                if not force and self._is_ingested(path):
                    logger.info("Skipping already-ingested source: %s", path)
                    total_skipped += 1
                    continue

                # Parse
                parser = get_parser(
                    path, explicit_type=source_type if source_type != "auto" else None
                )
                if parser is None:
                    # Fallback: try as a directory of text files
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
                        path, len(batch_memories), focus, tier, all_tags, dry_run=False
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
                # Image tokens depend on resolution; use a rough estimate
                input_tokens += 255  # ~85 tokens * 3 for typical 3-tile image
            else:
                input_tokens += len(chunk.content) / CHARS_PER_TOKEN

        # System prompt overhead (~500 tokens per batch)
        input_tokens += 500

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

    def _is_ingested(self, path: Path) -> bool:
        """Check whether a source file has already been ingested."""
        file_hash = self._hash_file(path)
        conn = self.memory_store._get_conn()
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM ingestion_log WHERE source_hash = ? AND dry_run = 0",
                (file_hash,),
            )
            return cur.fetchone()[0] > 0
        except sqlite3.Error:
            return False
        finally:
            conn.close()

    def _hash_file(self, path: Path) -> str:
        """Compute SHA256 hash of a file's contents."""
        if path.is_dir():
            # Hash directory contents collectively
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

        # Route to vision or text path
        try:
            if image_uris:
                # For now, handle pure-image or mixed batches via vision path
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
        """Parse model response into a list of memory dicts.

        Delegates to the shared parse_model_json_response utility.
        """
        return parse_model_json_response(text)

    # ── Memory writing ─────────────────────────────────────────────────────

    def _write_memories(self, memories: list[dict], tier: str, tags: list[str]) -> None:
        """Write memory candidates to the store."""
        for mem in memories:
            if not isinstance(mem, dict):
                continue

            name = mem.get("name") or self._derive_name(mem)
            confidence = mem.get("confidence", 1.0)

            # Skip low-confidence extractions
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
        """Derive a kebab-case name from memory dict."""
        title = mem.get("title", "")
        if not title:
            import time

            return f"ingested-memory-{int(time.time())}"
        return title.lower().replace(" ", "-").replace("_", "-")

    def _infer_type(self, name: str, mem: dict) -> str:
        """Infer memory type from tags or name pattern."""
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
    ) -> None:
        """Record an ingestion run in the log."""
        file_hash = self._hash_file(source_path)
        tags_json = json.dumps(tags or [])
        conn = self.memory_store._get_conn()
        try:
            conn.execute(
                "INSERT INTO ingestion_log "
                "(source_path, source_hash, memories_written, model, focus, tier, tags, dry_run) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(source_path),
                    file_hash,
                    memories_written,
                    "kimi-k2.6",
                    focus,
                    tier,
                    tags_json,
                    1 if dry_run else 0,
                ),
            )
            conn.commit()
        except sqlite3.Error as e:
            logger.warning("Failed to record ingestion: %s", e)
        finally:
            conn.close()

    # ── Contradiction scan ─────────────────────────────────────────────────

    def _contradiction_scan(self, new_memories: list[dict]) -> int:
        """Check new memories against existing canonical ones for contradictions.

        Delegates to the shared run_contradiction_scan utility.
        """

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
        """Query ingestion_log and return a formatted status table."""
        import sqlite3 as _sqlite3

        conn = _sqlite3.connect(str(db_path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            cur = conn.execute(
                "SELECT source_path, ingested_at, memories_written, model, focus, "
                "tier, dry_run, error_count FROM ingestion_log "
                "ORDER BY ingested_at DESC LIMIT ?",
                (min(limit, 100),),
            )
            rows = cur.fetchall()
        except _sqlite3.Error as e:
            return f"Ingestion log query failed: {e}"
        finally:
            conn.close()

        if not rows:
            return "No ingestion runs recorded."

        lines = [
            "| Source | Date | Memories | Model | Focus | Tier | Status |\n"
            "|---|---|---|---|---|---|---|"
        ]
        for source_path, dt, count, model, focus, tier, dry_run, errors in rows:
            status = "dry-run" if dry_run else ("errors" if errors else "committed")
            src_short = Path(source_path).name[:40]
            lines.append(
                f"| {src_short} | {dt[:16]} | {count} | {model} | {focus} | {tier} | {status} |"
            )

        return "\n".join(lines)

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

            image_chunks = sum(1 for c in chunks if c.is_image)
            text_chunks = len(chunks) - image_chunks
            tokens = sum(255 if c.is_image else len(c.content) // CHARS_PER_TOKEN for c in chunks)
            cost = (tokens / 1000) * DEFAULT_INPUT_PRICE_PER_1K
            cost += (
                ESTIMATED_OUTPUT_TOKENS_PER_CHUNK * len(chunks) / 1000 * DEFAULT_OUTPUT_PRICE_PER_1K
            )

            total_chunks += len(chunks)
            total_tokens += tokens
            total_cost += cost

            desc = f"{len(chunks)} chunks"
            if text_chunks:
                desc += f" ({text_chunks} text"
            if image_chunks:
                desc += f" + {image_chunks} image"
            if text_chunks and image_chunks:
                desc += ")"
            elif not text_chunks and not image_chunks:
                pass
            else:
                desc += ")"

            # Expected memory range (rough: 0.5–1.2 per chunk based on density)
            est_low = max(0, len(chunks) // 2)
            est_high = max(1, int(len(chunks) * 1.2))
            parts.append(
                f"- **{path.name}**: ~{tokens:,} tokens, {desc} — "
                f"est. ${cost:.4f}, typically {est_low}–{est_high} memories"
            )

        parts.append(
            f"\n**Total**: {total_chunks} chunks, ~{total_tokens:,} tokens, est. ${total_cost:.4f}"
        )
        if total_cost > 0.50:
            parts.append(
                "\n**Note**: Cost estimates are approximate — image-heavy sources may vary 2–3×."
            )

        return "\n".join(parts)
