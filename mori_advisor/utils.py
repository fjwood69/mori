"""Shared utilities for Mori's pipeline components.

Functions that are shared between the dream pipeline and ingestion pipeline:
- JSON response parsing (same extract-array-from-LLM-output logic)
- Contradiction scanning (same check-new-against-existing-canonical pattern)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# ── JSON response parsing ─────────────────────────────────────────────────


def parse_model_json_response(text: str) -> list[dict]:
    """Parse an LLM response that should be a JSON array of memory objects.

    Strategy 1: full response is valid JSON array.
    Strategy 2: extract JSON array from surrounding text.

    Used by both DreamPipeline and IngestionPipeline.
    """
    text = text.strip()

    # Strategy 1: full response is valid JSON array
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    # Strategy 2: extract JSON array from surrounding text
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        try:
            result = json.loads(text[start : end + 1])
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    logger.warning("Failed to parse model response as JSON array")
    return []


# ── Contradiction scan ────────────────────────────────────────────────────


CONTRADICTION_SCAN_PROMPT = """You are comparing two technical memories for logical contradictions.

New memory (just written):
Title: {new_title}
Body:
{new_body}

Existing memory (from the shared store):
Title: {existing_title}
Body:
{existing_body}

Does the new memory contradict or supersede the existing memory?
Answer with exactly one word: SUPERSEDES, RELATED, or UNRELATED.

SUPERSEDES = the new memory invalidates, replaces, or directly contradicts the existing one.
RELATED = they discuss related topics but don't contradict each other.
UNRELATED = they cover completely different topics."""


async def run_contradiction_scan(
    new_memories: list[dict],
    db_path: str | Path | None = None,
    consult_fn=None,
    store=None,
) -> int:
    """Check new memories against existing canonical ones for contradictions.

    For each new memory, searches for existing canonical memories with
    overlapping name prefixes and runs a lightweight LLM check for
    SUPERSEDES/RELATED/UNRELATED.

    Args:
        new_memories: List of memory dicts (must have 'name', 'title', 'body').
        db_path: Path to the SQLite database (legacy; use store= instead).
        consult_fn: Callable with signature (system, user, vk, max_tokens, temperature)
                    that returns the model's text response.
        store: BaseStore instance (SQLiteStore or PostgresStore).

    Returns:
        Count of supersessions detected.
    """
    from mori_advisor.store.postgres_store import PostgresStore

    if isinstance(store, PostgresStore):
        superseded_count = 0
        async with store.begin_transaction() as conn:
            for mem in new_memories:
                name = mem.get("name") or mem.get("path", "")
                if not name:
                    continue

                if "/" in name:
                    name = name.replace("/", "-").replace("_", "-")

                prefix = name.split("-")[0] if "-" in name else name

                try:
                    candidates = await conn.fetch(
                        """
                        SELECT name, title, body FROM memories
                        WHERE tier = 'canonical'
                          AND superseded_by IS NULL
                          AND (name LIKE $1 OR name LIKE $2 OR tags::text LIKE $3)
                        LIMIT 5
                        """,
                        f"{prefix}%",
                        f"%-{prefix}%",
                        f'%"{prefix}"%',
                    )
                except Exception as e:
                    logger.debug("Failed to query candidates in Postgres: %s", e)
                    continue

                for cand in candidates:
                    cand_name = cand["name"]
                    cand_title = cand["title"]
                    cand_body = cand["body"]
                    if not cand_body:
                        continue

                    try:
                        prompt = CONTRADICTION_SCAN_PROMPT.format(
                            new_title=mem.get("title", name),
                            new_body=mem.get("body", "")[:2000],
                            existing_title=cand_title,
                            existing_body=cand_body[:2000],
                        )
                        response = consult_fn(
                            system=prompt,
                            user=f"new: {name}\nexisting: {cand_name}",
                            vk="fast",
                            max_tokens=16,
                            temperature=0.0,
                        )
                        verdict = (response or "").strip().upper()
                        if verdict == "SUPERSEDES":
                            await conn.execute(
                                "UPDATE memories SET superseded_by = $1, updated_at = NOW() "
                                "WHERE name = $2",
                                name,
                                cand_name,
                            )
                            await conn.execute(
                                "INSERT INTO eviction_queue (memory_name, reason, detail) "
                                "VALUES ($1, 'superseded', $2)",
                                cand_name,
                                f"Superseded by '{name}'",
                            )
                            superseded_count += 1
                            logger.info("Superseded %s with %s (Postgres)", cand_name, name)
                    except Exception as e:
                        logger.debug("Contradiction check failed %s vs %s: %s", name, cand_name, e)
        return superseded_count

    # Fallback/SQLite path
    if store is not None:
        try:
            write_conn = store.get_conn()
        except NotImplementedError:
            write_conn = None
    else:
        write_conn = None

    own_conn = write_conn is None
    superseded_count = 0

    try:
        if own_conn:
            if db_path is None:
                import os
                from pathlib import Path as _Path

                data_dir = os.environ.get("MORI_ADVISOR_DATA", "/data/mori-advisor")
                db_path = _Path(data_dir) / "memories.db"
            write_conn = sqlite3.connect(str(db_path), timeout=30)
            write_conn.execute("PRAGMA journal_mode=WAL")
            write_conn.execute("PRAGMA synchronous=NORMAL")
            write_conn.execute("PRAGMA busy_timeout=30000")

        for mem in new_memories:
            name = mem.get("name") or mem.get("path", "")
            if not name:
                continue

            # Normalise: path-style names (from dream) vs kebab names (from ingestion)
            if "/" in name:
                name = name.replace("/", "-").replace("_", "-")

            prefix = name.split("-")[0] if "-" in name else name

            try:
                cur = write_conn.execute(
                    """
                    SELECT name, title, body FROM memories
                    WHERE tier = 'canonical'
                      AND superseded_by IS NULL
                      AND (name LIKE ? OR name LIKE ? OR tags LIKE ?)
                    LIMIT 5
                    """,
                    (f"{prefix}%", f"%-{prefix}%", f'%"{prefix}"%'),
                )
                candidates = cur.fetchall()
            except sqlite3.Error:
                continue

            for cand_name, cand_title, cand_body in candidates:
                if not cand_body:
                    continue

                try:
                    prompt = CONTRADICTION_SCAN_PROMPT.format(
                        new_title=mem.get("title", name),
                        new_body=mem.get("body", "")[:2000],
                        existing_title=cand_title,
                        existing_body=cand_body[:2000],
                    )
                    response = consult_fn(
                        system=prompt,
                        user=f"new: {name}\nexisting: {cand_name}",
                        vk="fast",
                        max_tokens=16,
                        temperature=0.0,
                    )
                    verdict = (response or "").strip().upper()
                    if verdict == "SUPERSEDES":
                        write_conn.execute(
                            "UPDATE memories SET superseded_by = ?, updated_at = datetime('now') "
                            "WHERE name = ?",
                            (name, cand_name),
                        )
                        write_conn.execute(
                            "INSERT INTO eviction_queue (memory_name, reason, detail) "
                            "VALUES (?, 'superseded', ?)",
                            (cand_name, f"Superseded by '{name}'"),
                        )
                        write_conn.commit()
                        superseded_count += 1
                        logger.info("Superseded %s with %s", cand_name, name)
                except Exception as e:
                    logger.debug("Contradiction check failed %s vs %s: %s", name, cand_name, e)
    finally:
        if own_conn and write_conn:
            write_conn.close()

    return superseded_count
