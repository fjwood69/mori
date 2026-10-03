"""Shared utilities for Mori's pipeline components.

Functions that are shared between the dream pipeline and ingestion pipeline:
- JSON response parsing (same extract-array-from-LLM-output logic)
- Contradiction scanning (same check-new-against-existing-canonical pattern)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mori_advisor import metrics as _metrics

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


# ── One-word classifier verdicts ──────────────────────────────────────────
# Freshness, the contradiction scan and the intake assessor ask a cheap model for ONE word. The
# served model is chosen by Bifrost routing, not here, so the parse must be correct for any model —
# including a reasoning model that spends its whole budget thinking and returns empty content.

CLASSIFIER_REASONING_EFFORT_ENV = "MORI_CLASSIFIER_REASONING_EFFORT"

_VERDICT_EDGE_CHARS = "`\"'*"


def parse_one_word_verdict(text: str | None, allowed: tuple[str, ...]) -> str | None:
    """Return the verdict iff *text* is exactly one allowed token; otherwise ``None``.

    ``None`` means "no answer" and every caller must treat it as an error — never as a default
    verdict. Positive shape only: a reasoning leak such as "We need answer exactly one word: YES,
    NO, or STALE" contains every token, so substring / first-word matching would accept garbage.

    Normalisation is end-only deletion, in this pinned order (the tests mirror it):
      1. strip surrounding whitespace;
      2. remove ONE trailing ".";
      3. strip surrounding backticks, double/single quotes and asterisks;
      4. reject if any non-ASCII character remains — ``str.upper`` would otherwise fold look-alikes
         such as "ſ" (long s), the "ﬆ" ligature or the Kelvin sign INTO an allowed token;
      5. upper-case and require exact equality with an allowed token.
    There is no second whitespace strip after step 3, so a fenced block stays unparseable.
    """
    if not text:
        return None
    candidate = text.strip()
    if candidate.endswith("."):
        candidate = candidate[:-1]
    candidate = candidate.strip(_VERDICT_EDGE_CHARS)
    if not candidate.isascii():
        return None
    candidate = candidate.upper()
    return candidate if candidate in allowed else None


def classifier_reasoning_effort() -> str | None:
    """``reasoning_effort`` for the one-word classifier calls, read per call.

    Tri-state on ``MORI_CLASSIFIER_REASONING_EFFORT``: unset → ``"none"`` (a reasoning model
    answers in 1–2 tokens instead of exhausting the budget); set but empty → ``None`` (omit the
    field — the escape hatch for a routed model that rejects it); anything else → that value.
    Read ONLY at the classifier call sites, never as a client default, so advisor / dream / vision
    calls keep their reasoning.
    """
    raw = os.environ.get(CLASSIFIER_REASONING_EFFORT_ENV)
    if raw is None:
        return "none"
    raw = raw.strip()
    return raw or None


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


# ── Supersession mode, pair keys and outcomes (v2.3.11) ─────────────────────

SUPERSESSION_MODE_ENV = "MORI_SUPERSESSION_MODE"
SUPERSEDED_DETAIL = "Superseded by '{}'"
PROPOSED_DETAIL = "Proposed: superseded by '{}'"
PAIR_REASONS = ("superseded", "supersession_proposed")


def supersession_mode() -> str:
    """``write`` iff ``$MORI_SUPERSESSION_MODE`` is exactly ``write`` (strip + lower); anything
    else — unset, ``report_only``, ``yes``, ``1``, ``true`` — is ``report_only`` (board S1/S4:
    fail closed on ambiguous config). Read per scan run."""
    raw = os.environ.get(SUPERSESSION_MODE_ENV, "")
    return "write" if raw.strip().lower() == "write" else "report_only"


@dataclass
class ScanOutcome:
    """What one contradiction scan did. ``applied`` = superseded_by written; ``proposed`` = a
    ``supersession_proposed`` queue row for review; ``failed`` = the write/insert failed;
    ``already_decided`` = the pair was skipped (no classifier call) under the decided-pair rule."""

    applied: int = 0
    proposed: int = 0
    failed: int = 0
    already_decided: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "applied": self.applied,
            "proposed": self.proposed,
            "failed": self.failed,
            "already_decided": self.already_decided,
        }


def pair_state(rows: list[tuple[bool, str | None]]) -> str:
    """Decided-pair rule (board R3/R4) over the pair's queue rows ``(resolved, resolution)``:
    ``open`` (never decided) | ``decided`` (an open row, or one applied/dismissed — skip, no
    classifier call) | ``propose_only`` (only undone pairs — a human undo means the pair may be
    re-classified but can only ever yield a proposal, in either mode)."""
    if not rows:
        return "open"
    if any((not resolved) or resolution != "unsuperseded" for resolved, resolution in rows):
        return "decided"
    return "propose_only"


def _record(mode: str, outcome: ScanOutcome, result: str) -> None:
    setattr(outcome, result, getattr(outcome, result) + 1)
    _metrics.record_supersession(mode, result)


async def run_contradiction_scan(
    new_memories: list[dict],
    db_path: str | Path | None = None,
    consult_fn=None,
    store=None,
) -> ScanOutcome:
    """Check new memories against existing canonical ones for contradictions.

    For each new memory, searches for existing canonical memories with overlapping name prefixes
    and runs a lightweight LLM check for SUPERSEDES/RELATED/UNRELATED.

    ``new_memories`` MUST be the memories actually WRITTEN (ACCEPTED) by the caller — never the
    parsed candidate list (v2.3.11, B-C2: a candidate routed to TD review, or skipped, must not be
    able to supersede canon).

    v2.3.11 — mode and pairs:
    * ``supersession_mode()`` decides what a ``SUPERSEDES`` verdict does. ``report_only`` (default)
      inserts a ``supersession_proposed`` queue row and writes NO ``superseded_by``; ``write``
      applies the supersession (UPDATE by id + a ``superseded`` queue row).
    * Pairs are keyed by ``(memory_name, counterpart)`` (migration 17). A decided pair is skipped
      BEFORE the classifier call; an undone (``unsuperseded``) pair can only produce a proposal.
    * Outcomes are counted in ``mori_supersessions_total{mode,result}``. A failed write is NOT a
      classifier error (the classifier answered) — M8.

    Fail-closed: only an exact ``SUPERSEDES`` (see :func:`parse_one_word_verdict`) acts; an
    unparseable reply or an exception writes nothing and is logged at WARNING and counted.
    A memory is never compared with itself: its own active row(s) are excluded by primary key
    (``memories.name`` is unique only among non-deleted rows), and the supersession UPDATE is
    keyed by id so a soft-deleted namesake is never touched.
    """
    from mori_advisor.store.postgres_store import PostgresStore

    mode = supersession_mode()
    outcome = ScanOutcome()
    pair_reasons_sql = "('superseded', 'supersession_proposed')"

    if isinstance(store, PostgresStore):
        async with store.begin_transaction() as conn:
            for mem in new_memories:
                raw_name = mem.get("name") or mem.get("path", "")
                if not raw_name:
                    continue
                name = _normalise_scan_name(raw_name)
                prefix = name.split("-")[0] if "-" in name else name

                try:
                    # Savepoint: a failed query must not abort the scan's outer transaction.
                    async with conn.transaction():
                        own_rows = await conn.fetch(
                            "SELECT id FROM memories WHERE name = ANY($1::text[]) AND deleted_at IS NULL",
                            sorted({raw_name, name}),
                        )
                        own_ids = [r["id"] for r in own_rows]
                        candidates = await conn.fetch(
                            """
                            SELECT id, name, title, body FROM memories
                            WHERE tier = 'canonical'
                              AND superseded_by IS NULL
                              AND deleted_at IS NULL
                              AND id <> ALL($4::bigint[])
                              AND (name LIKE $1 OR name LIKE $2 OR tags::text LIKE $3)
                            ORDER BY id
                            LIMIT 5
                            """,
                            f"{prefix}%",
                            f"%-{prefix}%",
                            f'%"{prefix}"%',
                            own_ids,
                        )
                except Exception as e:
                    logger.warning("Contradiction scan: candidate query failed for %s: %s", name, e)
                    _metrics.record_classifier_verdict("contradiction", "error")
                    continue

                for cand in candidates:
                    cand_id = cand["id"]
                    cand_name = cand["name"]
                    if cand_id in own_ids or not cand["body"]:
                        continue
                    try:
                        async with conn.transaction():
                            pair_rows = await conn.fetch(
                                "SELECT resolved, resolution FROM eviction_queue "
                                "WHERE memory_name = $1 AND counterpart = $2 "
                                f"AND reason IN {pair_reasons_sql}",
                                cand_name,
                                name,
                            )
                    except Exception as e:
                        logger.warning(
                            "Contradiction scan: pair lookup failed %s vs %s: %s",
                            name,
                            cand_name,
                            e,
                        )
                        _record(mode, outcome, "failed")
                        continue
                    state = pair_state([(r["resolved"], r["resolution"]) for r in pair_rows])
                    if state == "decided":
                        _record(mode, outcome, "already_decided")
                        continue
                    verdict = await _classify_pair(
                        consult_fn, mem, name, cand_name, cand["title"], cand["body"]
                    )
                    if verdict != "SUPERSEDES":
                        continue
                    apply = mode == "write" and state == "open"
                    inserted = True
                    try:
                        # Savepoint per pair: UPDATE + queue row land together or not at all, and a
                        # failure here does not abort the outer transaction.
                        async with conn.transaction():
                            if apply:
                                await conn.execute(
                                    "UPDATE memories SET superseded_by = $1, updated_at = NOW() WHERE id = $2",
                                    name,
                                    cand_id,
                                )
                                await conn.execute(
                                    "INSERT INTO eviction_queue "
                                    "(memory_name, reason, detail, counterpart) "
                                    "VALUES ($1, 'superseded', $2, $3)",
                                    cand_name,
                                    SUPERSEDED_DETAIL.format(name),
                                    name,
                                )
                            else:
                                inserted = (
                                    await conn.fetchval(
                                        "INSERT INTO eviction_queue "
                                        "(memory_name, reason, detail, counterpart) "
                                        "VALUES ($1, 'supersession_proposed', $2, $3) "
                                        "ON CONFLICT (memory_name, counterpart) "
                                        f"WHERE reason IN {pair_reasons_sql} AND resolved = FALSE "
                                        "DO NOTHING RETURNING id",
                                        cand_name,
                                        PROPOSED_DETAIL.format(name),
                                        name,
                                    )
                                    is not None
                                )
                    except Exception as e:
                        logger.warning(
                            "Contradiction scan: supersession write failed %s vs %s: %s",
                            name,
                            cand_name,
                            e,
                        )
                        _record(mode, outcome, "failed")
                    else:
                        _after_verdict(mode, outcome, apply, inserted, name, cand_name)
        return outcome

    # Fallback/SQLite path
    if store is not None:
        try:
            write_conn = store.get_conn()
        except NotImplementedError:
            write_conn = None
    else:
        write_conn = None

    own_conn = write_conn is None

    try:
        if own_conn:
            if db_path is None:
                from pathlib import Path as _Path

                data_dir = os.environ.get("MORI_ADVISOR_DATA", "/data/mori-advisor")
                db_path = _Path(data_dir) / "memories.db"
            write_conn = sqlite3.connect(str(db_path), timeout=30)
            write_conn.execute("PRAGMA journal_mode=WAL")
            write_conn.execute("PRAGMA synchronous=NORMAL")
            write_conn.execute("PRAGMA busy_timeout=30000")

        for mem in new_memories:
            raw_name = mem.get("name") or mem.get("path", "")
            if not raw_name:
                continue
            name = _normalise_scan_name(raw_name)
            prefix = name.split("-")[0] if "-" in name else name

            try:
                own_names = sorted({raw_name, name})
                own_ids = [
                    r[0]
                    for r in write_conn.execute(
                        f"SELECT id FROM memories WHERE name IN ({','.join('?' * len(own_names))}) "
                        "AND deleted_at IS NULL",
                        own_names,
                    ).fetchall()
                ]
                id_clause = f"AND id NOT IN ({','.join('?' * len(own_ids))})" if own_ids else ""
                cur = write_conn.execute(
                    f"""
                    SELECT id, name, title, body FROM memories
                    WHERE tier = 'canonical'
                      AND superseded_by IS NULL
                      AND deleted_at IS NULL
                      {id_clause}
                      AND (name LIKE ? OR name LIKE ? OR tags LIKE ?)
                    ORDER BY id
                    LIMIT 5
                    """,
                    (*own_ids, f"{prefix}%", f"%-{prefix}%", f'%"{prefix}"%'),
                )
                candidates = cur.fetchall()
            except sqlite3.Error as e:
                logger.warning("Contradiction scan: candidate query failed for %s: %s", name, e)
                _metrics.record_classifier_verdict("contradiction", "error")
                continue

            for cand_id, cand_name, cand_title, cand_body in candidates:
                if cand_id in own_ids or not cand_body:
                    continue
                try:
                    pair_rows = write_conn.execute(
                        "SELECT resolved, resolution FROM eviction_queue "
                        "WHERE memory_name = ? AND counterpart = ? "
                        f"AND reason IN {pair_reasons_sql}",
                        (cand_name, name),
                    ).fetchall()
                except sqlite3.Error as e:
                    logger.warning(
                        "Contradiction scan: pair lookup failed %s vs %s: %s", name, cand_name, e
                    )
                    _record(mode, outcome, "failed")
                    continue
                state = pair_state([(bool(r[0]), r[1]) for r in pair_rows])
                if state == "decided":
                    _record(mode, outcome, "already_decided")
                    continue
                verdict = await _classify_pair(
                    consult_fn, mem, name, cand_name, cand_title, cand_body
                )
                if verdict != "SUPERSEDES":
                    continue
                apply = mode == "write" and state == "open"
                inserted = True
                try:
                    if apply:
                        write_conn.execute(
                            "UPDATE memories SET superseded_by = ?, updated_at = datetime('now') WHERE id = ?",
                            (name, cand_id),
                        )
                        write_conn.execute(
                            "INSERT INTO eviction_queue (memory_name, reason, detail, counterpart) "
                            "VALUES (?, 'superseded', ?, ?)",
                            (cand_name, SUPERSEDED_DETAIL.format(name), name),
                        )
                    else:
                        # The partial unique index on open pairs makes a concurrent duplicate a no-op.
                        cur = write_conn.execute(
                            "INSERT OR IGNORE INTO eviction_queue "
                            "(memory_name, reason, detail, counterpart) "
                            "VALUES (?, 'supersession_proposed', ?, ?)",
                            (cand_name, PROPOSED_DETAIL.format(name), name),
                        )
                        inserted = cur.rowcount == 1
                    write_conn.commit()
                except Exception as e:
                    # Undo a half-applied pair (UPDATE done, INSERT failed); otherwise the next
                    # supersession's commit would land this UPDATE without its eviction-queue row.
                    try:
                        write_conn.rollback()
                    except sqlite3.Error:
                        pass
                    logger.warning(
                        "Contradiction scan: supersession write failed %s vs %s: %s",
                        name,
                        cand_name,
                        e,
                    )
                    _record(mode, outcome, "failed")
                else:
                    _after_verdict(mode, outcome, apply, inserted, name, cand_name)
    finally:
        if own_conn and write_conn:
            write_conn.close()

    return outcome


def _after_verdict(
    mode: str, outcome: ScanOutcome, apply: bool, inserted: bool, name: str, cand_name: str
) -> None:
    if apply:
        _record(mode, outcome, "applied")
        logger.info("Superseded %s with %s", cand_name, name)
    elif inserted:
        _record(mode, outcome, "proposed")
        logger.info("SUPERSESSION-PROPOSED %s by %s (mode=%s)", cand_name, name, mode)
    else:  # a concurrent run proposed this pair first
        _record(mode, outcome, "already_decided")


_SCAN_VERDICTS = ("SUPERSEDES", "RELATED", "UNRELATED")


def _normalise_scan_name(name: str) -> str:
    """Path-style names (from dream) vs kebab names (from ingestion)."""
    if "/" in name:
        return name.replace("/", "-").replace("_", "-")
    return name


async def _classify_pair(
    consult_fn: Callable[..., str],
    mem: dict[str, Any],
    name: str,
    cand_name: str,
    cand_title: str,
    cand_body: str,
) -> str | None:
    """One contradiction-scan LLM call. Returns the verdict, or ``None`` (unparseable / error)."""
    try:
        prompt = CONTRADICTION_SCAN_PROMPT.format(
            new_title=mem.get("title", name),
            new_body=mem.get("body", "")[:2000],
            existing_title=cand_title,
            existing_body=cand_body[:2000],
        )
        response = await asyncio.to_thread(
            consult_fn,
            system=prompt,
            user=f"new: {name}\nexisting: {cand_name}",
            vk="fast",
            max_tokens=16,
            temperature=0.0,
            reasoning_effort=classifier_reasoning_effort(),
        )
    except Exception as e:
        logger.warning("Contradiction check failed %s vs %s: %s", name, cand_name, e)
        _metrics.record_classifier_verdict("contradiction", "error")
        return None
    verdict = parse_one_word_verdict(response, _SCAN_VERDICTS)
    if verdict is None:
        logger.warning(
            "Contradiction scan: unparseable verdict for %s vs %s (raw=%r)",
            name,
            cand_name,
            (response or "")[:160],
        )
        _metrics.record_classifier_verdict("contradiction", "unparseable")
        return None
    _metrics.record_classifier_verdict("contradiction", verdict.lower())
    return verdict
