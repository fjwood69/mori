"""Dream pipeline — distill session events into durable memories.

Extracted from the homelab's mori-dream CLI into a library class
that can be called from MCP tools or scheduled jobs. Operates
entirely inside the container — no host filesystem access needed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mori_advisor.bifrost_client import BifrostClient
from mori_advisor.memory_store import MemoryStore
from mori_advisor.session_log import SessionLog
from mori_advisor.utils import parse_model_json_response, run_contradiction_scan

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

CRITICAL: Begin your response must start with [ and end with ]. No prose before or after the JSON."""


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
        self.memory_store = MemoryStore(db_path)
        self.trusted_dreamers = trusted_dreamers or []
        self.retention_buffer = retention_buffer
        self.nats_url = nats_url

    # ── Transaction support ──────────────────────────────────────────────

    def _begin_transaction(self) -> sqlite3.Connection:
        """Open a dedicated connection and begin a transaction.

        Uses BEGIN DEFERRED so DDL-free operations (reads + writes
        against already-initialised schema) don't contend with other
        connections. Schema bootstrapping runs separately at startup.
        Returns the connection, which the caller must commit/rollback.
        """
        import sqlite3 as _sqlite3

        conn = _sqlite3.connect(str(self.db_path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN DEFERRED")
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

        batch_session_ids = list(
            dict.fromkeys(e.get("session_id") for e in events if e.get("session_id"))
        )
        batch_clients = list(dict.fromkeys(e.get("client") for e in events if e.get("client")))

        # Begin IMMEDIATE transaction — if we crash between writing memories
        # and advancing the watermark, the transaction rolls back and the
        # next run re-processes events cleanly (no duplicates).
        txn_conn = self._begin_transaction()
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
                    self._write_memory(
                        mem, name, action, batch_session_ids, batch_clients, _conn=txn_conn
                    )
                    logger.info("  ✓ %s %s", action, name)
                    written += 1
                except Exception as e:
                    logger.error("  ✗ %s %s — %s", action, name, e)
                    errors += 1

            max_id = max(e["id"] for e in events)
            self._set_watermark(max_id, _conn=txn_conn)

            pruned = self.session_log.prune_events(
                max(0, max_id - self.retention_buffer), _conn=txn_conn
            )
            logger.info(
                "Pruned %s events older than id %s", pruned, max(0, max_id - self.retention_buffer)
            )

            txn_conn.commit()
        except Exception:
            txn_conn.rollback()
            raise
        finally:
            txn_conn.close()

        # Contradiction scan runs AFTER the transaction commits.
        # It calls the LLM (potentially slow) and must not hold the DB lock.
        # If it fails, memories exist without contradiction markers until
        # the next dream run — acceptable vs rolling back the entire batch.
        superseded = 0
        if written > 0:
            try:
                superseded = self._contradiction_scan(memories)
                if superseded > 0:
                    logger.info("Contradiction scan: %s existing memories superseded", superseded)
            except Exception as e:
                logger.warning("Contradiction scan failed: %s", e)

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
            max_tokens=16384,
            temperature=0.3,
        )

    def _parse_response(self, text: str) -> list[dict]:
        """Parse model response into a list of memory dicts.

        Delegates to the shared parse_model_json_response utility.
        Returns empty list on failure (matches legacy None → [] behaviour).
        """
        return parse_model_json_response(text)

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
        path = mem.get("path", name)
        body = mem.get("body", "")

        self.memory_store.write(
            name=name,
            title=path.replace(".md", "").replace("/", " — "),
            description=mem.get("reason", ""),
            type=self._infer_type(path),
            tier="working",
            body=body,
            tags=["dream-phase", action.lower()],
            origin_session_ids=batch_session_ids,
            origin_clients=batch_clients,
            _skip_protection=True,
            _conn=_conn,
        )
        return f"{action} {name}"

    # ── NATS eviction notice ──────────────────────────────────────────

    def _publish_eviction_notice(self, superseded_count: int) -> None:
        """Publish a short NATS message about eviction events from this dream run.

        Fire-and-forget — errors are logged, never propagated.
        """
        import socket

        import nats

        hostname = socket.gethostname()
        message = (
            f"Dream pipeline superseded {superseded_count} memory(ies) on {hostname}. "
            f"Run `/pensieve --eviction-queue` or `memory_review` to review."
        )
        payload = json.dumps(
            {
                "from": hostname,
                "text": message,
                "ts": __import__("time").time(),
                "type": "eviction-notice",
            }
        )

        try:
            import asyncio

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            nc = loop.run_until_complete(nats.connect(self.nats_url))
            loop.run_until_complete(nc.publish("cc.mori", payload.encode()))
            loop.run_until_complete(nc.flush())
            loop.run_until_complete(nc.drain())
            loop.close()
        except Exception as e:
            logger.debug("NATS publish failed: %s", e)

    # ── Contradiction scan ────────────────────────────────────────────

    def _contradiction_scan(self, new_memories: list[dict]) -> int:
        """Check new memories against existing canonical ones for contradictions.

        Delegates to the shared run_contradiction_scan utility.
        """

        # Consult wrapper matching the expected callable signature
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
