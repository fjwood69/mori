"""Dream pipeline — distill session events into durable memories.

Extracted from the homelab's moku-dream CLI into a library class
that can be called from MCP tools or scheduled jobs. Operates
entirely inside the container — no host filesystem access needed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

from moku_advisor.bifrost_client import BifrostClient
from moku_advisor.session_log import SessionLog

logger = logging.getLogger(__name__)

DREAM_SYSTEM_PROMPT = """You are the Dreamer. Distill session noise into durable memory for the team. If you suspect a pattern but can't confirm it, record it as LOW confidence rather than silently dropping it.

Output a JSON array of memory objects:
- path: hierarchical kebab-case (e.g., "project/api/auth-decision" or "profile/dev-workflow")
- action: "CREATE" | "MERGE" | "DELETE"
- confidence: "HIGH" (fact/decision), "MEDIUM" (pattern), "LOW" (hypothesis)
- reason: one-line rationale
- body: 2-6 line markdown with context, implications, and any unresolved tension

Capture: architecture, conventions, preferences, gotchas, recurring patterns, deferred decisions, cross-tool context.
Ignore: one-off bugs, standard fixes, noise, anything recoverable from docs or git.

Return ONLY the JSON array. No prose outside it."""

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


class DreamPipeline:
    """Pipeline that reads session events, calls a model to distill them,
    and writes the resulting memories.

    All state is stored in the shared SQLite database (session_events +
    dream_state tables).
    """

    def __init__(
        self,
        db_path: str | Path,
        bifrost_client: BifrostClient,
        trusted_dreamers: list[str] | None = None,
        retention_buffer: int = 5000,
        nats_url: str | None = None,
    ):
        self.db_path = Path(db_path)
        self.client = bifrost_client
        self.session_log = SessionLog(db_path)
        self.trusted_dreamers = trusted_dreamers or []
        self.retention_buffer = retention_buffer
        self.nats_url = nats_url
        self._txn_conn = None  # transaction connection for BEGIN IMMEDIATE

    # ── Transaction support ──────────────────────────────────────────────

    def _begin_immediate(self) -> sqlite3.Connection:
        """Open a dedicated connection and begin IMMEDIATE transaction.

        Prevents other writers (other container instances, concurrent
        dream runs) from interfering during the dream pipeline.
        Returns the connection, which the caller must commit/rollback.
        """
        import sqlite3 as _sqlite3

        conn = _sqlite3.connect(str(self.db_path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN IMMEDIATE")
        return conn

    # ── Public API ───────────────────────────────────────────────────────

    def get_status(self) -> str:
        """Return dream state as formatted text (same output as --status)."""
        total = self.session_log.count_events()
        sessions = self.session_log.list_sessions()
        last_id_str = self.session_log.get_dream_state("last_dreamed_event_id", "0")
        last_at = self.session_log.get_dream_state("last_dreamed_at", "never")
        last_id = int(last_id_str) if last_id_str and last_id_str != "never" else 0
        undreamed = len(self.session_log.read_events(since_event_id=last_id, limit=0))

        now = datetime.now(timezone.utc)
        hour = now.hour
        next_hour = ((hour // 4) + 1) * 4
        if next_hour >= 24:
            next_dt = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        else:
            next_dt = now.replace(hour=next_hour, minute=0, second=0, microsecond=0)
        minutes_until = int((next_dt - now).total_seconds() / 60)
        if minutes_until < 1:
            next_str = "now"
        elif minutes_until < 60:
            next_str = f"{minutes_until}m"
        else:
            next_str = f"{minutes_until // 60}h{minutes_until % 60:02d}m"

        lines = [
            "**Dream State**",
            f"  Events total:     {total}",
            f"  Sessions:         {len(sessions)}",
            f"  Last dreamed ID:  {last_id}",
            f"  Last dreamed at:  {last_at}",
            f"  Undreamed events: {undreamed}",
            f"  Next dream:       {next_str}",
        ]
        return "\n".join(lines)

    def run(self, dry_run: bool = False) -> list[dict]:
        """Execute the full dream pipeline.

        Args:
            dry_run: If True, preview without writing any memories or
                     updating the watermark.

        Returns:
            List of memory dicts that were (or would be) written.
        """
        last_id = self._get_watermark()
        events = self.session_log.read_events(since_event_id=last_id, limit=500)
        if not events:
            logger.info("No new events since id %s. Nothing to do.", last_id)
            return []

        logger.info("Found %s new events since event id %s", len(events), last_id)

        events_text = self._format_events(events)

        logger.info("Calling dream model…")
        response = self._call_dream_model(events_text)
        logger.info("Dream model responded (%s chars)", len(response))

        memories = self._parse_response(response)
        if memories is None:
            logger.error("Failed to parse dream model response as JSON")
            return []
        if not memories:
            logger.info("No new memories to write.")
            return []

        if dry_run:
            return memories

        batch_session_ids = list(dict.fromkeys(
            e.get("session_id") for e in events if e.get("session_id")
        ))
        batch_clients = list(dict.fromkeys(
            e.get("client") for e in events if e.get("client")
        ))

        # Begin IMMEDIATE transaction — if we crash between writing memories
        # and advancing the watermark, the transaction rolls back and the
        # next run re-processes events cleanly (no duplicates).
        txn_conn = self._begin_immediate()
        try:
            written = 0
            errors = 0
            for mem in memories:
                if not isinstance(mem, dict) or "path" not in mem:
                    logger.warning("Skipping invalid memory entry: %s", mem)
                    errors += 1
                    continue

                path = mem["path"]
                action = mem.get("action", "CREATE")
                body = mem.get("body", "")
                if not body:
                    logger.warning("Skipping memory with empty body: %s", path)
                    errors += 1
                    continue

                name = self._path_to_name(path)
                try:
                    self._write_memory(mem, name, action, batch_session_ids, batch_clients, _conn=txn_conn)
                    logger.info("  ✓ %s %s", action, name)
                    written += 1
                except Exception as e:
                    logger.error("  ✗ %s %s — %s", action, name, e)
                    errors += 1

            max_id = max(e["id"] for e in events)
            self._set_watermark(max_id, _conn=txn_conn)

            # Contradiction scan: check new memories against existing canonical ones
            superseded = 0
            if written > 0:
                try:
                    superseded = self._contradiction_scan(memories, _conn=txn_conn)
                    if superseded > 0:
                        logger.info("Contradiction scan: %s existing memories superseded", superseded)
                except Exception as e:
                    logger.warning("Contradiction scan failed: %s", e)

            pruned = self.session_log.prune_events(max(0, max_id - self.retention_buffer))
            logger.info("Pruned %s events older than id %s", pruned, max(0, max_id - self.retention_buffer))

            txn_conn.commit()
        except Exception:
            txn_conn.rollback()
            raise
        finally:
            txn_conn.close()

        # NATS publish happens outside the transaction — fire-and-forget
        if superseded > 0 and self.nats_url:
            try:
                self._publish_eviction_notice(superseded)
            except Exception as e:
                logger.warning("NATS eviction notice failed: %s", e)

        logger.info("Done: %s written, %s errors, watermark at id %s", written, errors, max_id)
        return memories

    # ── Internal helpers ─────────────────────────────────────────────────

    def _get_watermark(self) -> int:
        val = self.session_log.get_dream_state("last_dreamed_event_id", "0")
        return int(val) if val else 0

    def _set_watermark(self, event_id: int, _conn: sqlite3.Connection | None = None) -> None:
        self.session_log.set_dream_state("last_dreamed_event_id", str(event_id), _conn=_conn)
        self.session_log.set_dream_state(
            "last_dreamed_at", datetime.now(timezone.utc).isoformat(), _conn=_conn
        )

    def _format_events(self, events: list[dict]) -> str:
        """Format events into a clean, signal-focused summary grouped by session."""
        if not events:
            return "(no new events)"

        sessions: dict[str, list[dict]] = {}
        for e in events:
            sid = e.get("session_id", "unknown")
            sessions.setdefault(sid, []).append(e)

        parts = []
        for sid, session_events in sessions.items():
            start_ts = session_events[0].get("timestamp", "?")[:19]
            client = session_events[0].get("client", "?")

            items = []
            for e in session_events:
                name = e.get("event_name", "")
                tool = e.get("tool_name", "")
                prompt = e.get("prompt", "")
                err = e.get("tool_error", "")

                if name == "UserPromptSubmit" and prompt:
                    p_text = prompt[:200].replace("\n", " ")
                    items.append(f"  Prompt: {p_text}")
                elif name == "PostToolUse" and tool:
                    items.append(f"  Tool: {tool}")
                elif name == "PostToolUseFailure" and err:
                    items.append(f"  FAILURE ({tool}): {err[:150]}")
                elif name == "SessionStart":
                    cwd = e.get("cwd", "?")
                    items.insert(0, f"  CWD: {cwd}")
                elif name == "Stop":
                    reason = e.get("stop_reason", "end_turn")
                    items.append(f"  Stopped: {reason}")

            session_block = f"Session: {sid} ({start_ts}, {client})"
            if items:
                session_block += "\n" + "\n".join(items)
            parts.append(session_block)

        return "\n\n".join(parts)

    def _call_dream_model(self, events_text: str) -> str:
        """Send formatted events to the dream model and return the response."""
        return self.client.consult(
            system=DREAM_SYSTEM_PROMPT,
            user=events_text,
            vk="dream",
            max_tokens=4096,
            temperature=0.3,
        )

    def _parse_response(self, text: str) -> list[dict] | None:
        """Parse model response into a list of memory dicts."""
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

        return None

    def _path_to_name(self, path: str) -> str:
        return path.replace(".md", "").replace("/", "-").replace("_", "-")

    def _infer_type(self, path: str) -> str:
        if path.startswith("profile/"):
            return "profile"
        if path.startswith("gotchas/"):
            return "pattern"
        if path.startswith("project/"):
            return "project"
        return "decision"

    def _write_memory(
        self,
        mem: dict,
        name: str,
        action: str,
        batch_session_ids: list[str],
        batch_clients: list[str],
        _conn: sqlite3.Connection | None = None,
    ) -> str:
        from moku_advisor.memory_store import MemoryStore

        store = MemoryStore(db_path=self.db_path)

        path = mem.get("path", name)
        body = mem.get("body", "")

        store.write(
            name=name,
            title=path.replace(".md", "").replace("/", " — "),
            description=mem.get("reason", ""),
            type=self._infer_type(path),
            tier="working",
            body=body,
            tags=["dream-phase", action.lower()],
            origin_session_ids=batch_session_ids,
            origin_clients=batch_clients,
            _conn=_conn,
        )
        return f"{action} {name}"

    # ── NATS eviction notice ──────────────────────────────────────────

    def _publish_eviction_notice(self, superseded_count: int) -> None:
        """Publish a short NATS message about eviction events from this dream run.

        Fire-and-forget — errors are logged, never propagated.
        """
        import json
        import socket
        import nats

        hostname = socket.gethostname()
        message = (
            f"Dream pipeline superseded {superseded_count} memory(ies) on {hostname}. "
            f"Run `/pensieve --eviction-queue` or `memory_review` to review."
        )
        payload = json.dumps({
            "from": hostname,
            "text": message,
            "ts": __import__("time").time(),
            "type": "eviction-notice",
        })

        try:
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            nc = loop.run_until_complete(nats.connect(self.nats_url))
            loop.run_until_complete(nc.publish("cc.moku", payload.encode()))
            loop.run_until_complete(nc.flush())
            loop.run_until_complete(nc.drain())
            loop.close()
        except Exception as e:
            logger.debug("NATS publish failed: %s", e)

    # ── Contradiction scan ────────────────────────────────────────────

    def _contradiction_scan(self, new_memories: list[dict], _conn: sqlite3.Connection | None = None) -> int:
        """Check new memories against existing canonical ones for contradictions.

        For each new memory, searches for existing canonical memories with
        overlapping tags/name prefixes and runs a lightweight LLM check.
        Returns count of supersessions detected.
        """
        from moku_advisor.memory_store import MemoryStore
        store = MemoryStore(db_path=self.db_path)
        # Use the transaction connection for writes, store's own connection for reads
        write_conn = _conn or store._conn

        superseded_count = 0

        for mem in new_memories:
            path = mem.get("path", "")
            if not path:
                continue

            # Derive candidate search terms from the new memory
            name = self._path_to_name(path)
            prefix = name.split("-")[0] if "-" in name else name

            try:
                cur = store._conn.execute(
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
                        new_title=path.replace("/", " — "),
                        new_body=mem.get("body", "")[:2000],
                        existing_title=cand_title,
                        existing_body=cand_body[:2000],
                    )
                    response = self.client.consult(
                        system=prompt,
                        user=f"new: {name}\nexisting: {cand_name}",
                        vk="advisor",
                        max_tokens=16,
                        temperature=0.0,
                    )
                    verdict = (response or "").strip().upper()
                    if verdict == "SUPERSEDES":
                        write_conn.execute(
                            "UPDATE memories SET superseded_by = ?, updated_at = datetime('now') WHERE name = ?",
                            (name, cand_name),
                        )
                        write_conn.execute(
                            "INSERT INTO eviction_queue (memory_name, reason, detail) VALUES (?, 'superseded', ?)",
                            (cand_name, f"Superseded by '{name}' in dream run"),
                        )
                        superseded_count += 1
                        logger.info("Superseded %s with %s", cand_name, name)
                except Exception as e:
                    logger.debug("Contradiction check failed %s vs %s: %s", name, cand_name, e)

        return superseded_count