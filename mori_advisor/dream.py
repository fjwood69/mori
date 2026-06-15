"""Dream pipeline — distill session events into durable memories.

Extracted from the mori-dream CLI into a library class
that can be called from MCP tools or scheduled jobs. Operates
entirely inside the container — no host filesystem access needed.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import sqlite3
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mori_advisor.bifrost_client import BifrostClient
from mori_advisor.prompt_loader import OUTPUT_REMINDER, load_prompt
from mori_advisor.utils import parse_model_json_response, run_contradiction_scan

logger = logging.getLogger(__name__)

# Max chars of captured assistant reasoning surfaced per Stop event in the dream
# prompt. Bounds distillation cost when sessions have many turns; tune from data.
_ASSISTANT_DREAM_CAP = 1500


async def _a(val):
    """Await val if it's a coroutine (Postgres), pass through if sync (SQLite)."""
    return await val if inspect.isawaitable(val) else val


@asynccontextmanager
async def _begin_txn(store):
    """Async-safe transaction wrapper for both SQLiteStore (sync) and PostgresStore (async)."""
    ctx = store.begin_transaction()
    if hasattr(ctx, "__aenter__"):
        async with ctx as conn:
            yield conn
    else:
        with ctx as conn:
            yield conn


# Emergency fallback only — the packaged mori_advisor/prompts/dreamer.txt is the real
# default and the operator-editable source of truth (see prompt_loader). This compact
# version is used only if that file is missing/unreadable, and the use is logged.
_DREAM_PROMPT_FALLBACK = """You are the Dreamer. Distill the session into a JSON array of memory objects, each with: reason (one line), confidence (HIGH/MEDIUM/LOW), path (kebab-case, naming the convention NOT the location), body (2-6 lines of markdown), evidence (array of file/symbol refs, may be empty). Emit ONE memory per convention, never one per occurrence. If the session was purely tactical, return []. Output raw JSON only, starting with [ and ending with ]."""

DREAM_SYSTEM_PROMPT = load_prompt("dreamer", _DREAM_PROMPT_FALLBACK)


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
        store=None,
    ):
        self.db_path = Path(db_path)
        self.client = bifrost_client
        self.trusted_dreamers = trusted_dreamers or []
        self.retention_buffer = retention_buffer
        self.nats_url = nats_url

        if store is None:
            from mori_advisor.store.sqlite_store import SQLiteStore

            store = SQLiteStore(db_path)
        self.store = store

        # Keep legacy aliases for any callers that reference them directly
        self.session_log = store._log if hasattr(store, "_log") else store
        self.memory_store = store._mem if hasattr(store, "_mem") else store

        # Prevents concurrent dream runs from blocking the event loop simultaneously.
        # Lazily initialised so __init__ can be called outside a running loop.
        self._run_lock: asyncio.Lock | None = None

    # ── Public API ───────────────────────────────────────────────────────

    async def get_status(self) -> str:
        """Return dream state as formatted text (same output as --status)."""
        total = await _a(self.session_log.count_events())
        sessions = await _a(self.session_log.list_sessions())
        last_id_str = await _a(self.session_log.get_dream_state("last_dreamed_event_id", "0"))
        last_at = await _a(self.session_log.get_dream_state("last_dreamed_at", "never"))
        last_id = int(last_id_str) if last_id_str and last_id_str != "never" else 0
        undreamed = await _a(self.session_log.count_events_since(last_id))

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

    async def run(self, dry_run: bool = False) -> list[dict]:
        """Execute the full dream pipeline.

        Args:
            dry_run: If True, preview without writing any memories or
                     updating the watermark.

        Returns:
            List of memory dicts that were (or would be) written.
        """
        if self._run_lock is None:
            self._run_lock = asyncio.Lock()
        if self._run_lock.locked():
            logger.info("Dream run already in progress; skipping concurrent invocation.")
            return []
        async with self._run_lock:
            return await self._run_inner(dry_run=dry_run)

    async def _run_inner(self, dry_run: bool = False) -> list[dict]:
        # B3 — intake promotion (flag-gated, Postgres-only, additive).
        # Runs FIRST so it fires on every invocation, regardless of whether
        # this dream run produces any distilled memories (it must not be
        # skipped by the no-events or no-memories early-returns below).
        # Defence-in-depth: the outer try/except catches any catastrophic
        # failure that escapes the method's own internal guard (e.g. an
        # exception raised before the method's try block is entered).
        try:
            await self._run_intake_promotion()
        except Exception as _exc:
            logger.error(
                "run(): _run_intake_promotion raised unexpectedly (continuing): %s",
                _exc,
                exc_info=True,
            )

        last_id = await self._get_watermark()
        events = await _a(self.session_log.read_events(since_event_id=last_id, limit=500))
        if not events:
            logger.info("No new events since id %s. Nothing to do.", last_id)
            return []

        logger.info("Found %s new events since event id %s", len(events), last_id)

        events_text = self._format_events(events)

        logger.info("Calling dream model…")
        response = await asyncio.to_thread(self._call_dream_model, events_text)
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
        batch_project = self._extract_project_from_events(events)

        # Begin DEFERRED transaction — if we crash between writing memories
        # and advancing the watermark, the transaction rolls back and the
        # next run re-processes events cleanly (no duplicates).
        async with _begin_txn(self.store) as txn_conn:
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
                    if hasattr(txn_conn, "transaction"):
                        # asyncpg Connection (Postgres) supports nested transaction (savepoint)
                        async with txn_conn.transaction():
                            await self._write_memory(
                                mem,
                                name,
                                action,
                                batch_session_ids,
                                batch_clients,
                                project=batch_project,
                                _conn=txn_conn,
                            )
                    else:
                        await self._write_memory(
                            mem,
                            name,
                            action,
                            batch_session_ids,
                            batch_clients,
                            project=batch_project,
                            _conn=txn_conn,
                        )
                    logger.info("  ✓ %s %s", action, name)
                    written += 1
                except Exception as e:
                    logger.error("  ✗ %s %s — %s", action, name, e)
                    errors += 1

            max_id = max(e["id"] for e in events)
            await self._set_watermark(max_id, _conn=txn_conn)

            pruned = await _a(
                self.store.prune_events(max(0, max_id - self.retention_buffer), _conn=txn_conn)
            )
            logger.info(
                "Pruned %s events older than id %s", pruned, max(0, max_id - self.retention_buffer)
            )

        # Contradiction scan runs AFTER the transaction commits.
        # It calls the LLM (potentially slow) and must not hold the DB lock.
        # If it fails, memories exist without contradiction markers until
        # the next dream run — acceptable vs rolling back the entire batch.
        superseded = 0
        if written > 0:
            try:
                superseded = await self._contradiction_scan(memories)
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

    # ── B3: intake promotion ──────────────────────────────────────────────

    async def _run_intake_promotion(self) -> None:
        """Run one assess + drain pass over the intake pipeline (B3).

        This method is a no-op unless ALL of the following hold:

        1. ``MORI_INTAKE_PROMOTION_ENABLED`` env var is ``"true"`` (case-
           insensitive).  Default: ``false``.  The flag is checked first so
           that cold-start import cost is zero when the feature is off.
        2. ``MORI_INTAKE_DATABASE_URL`` env var is set (the intake Postgres
           DSN).  Without it there is nothing to connect to.
        3. The mori canon store is a ``PostgresStore`` (detected via
           ``hasattr(self.store, "pool")``).  On a SQLite canon store the
           feature is UNAVAILABLE by design — SQLite is the dev/UAT backend
           and does not support the async ``canon_reader()`` interface.

        When the flag is off OR the store is SQLite: logs once at debug/info
        level and returns immediately.  No import of ``mori_intake.db``; no
        connection attempt.

        On any exception: logs at ERROR level and returns silently.  This
        method MUST NOT raise into ``run()`` — a broken intake Postgres must
        never abort a dream run.
        """
        flag = os.environ.get("MORI_INTAKE_PROMOTION_ENABLED", "false").strip().lower()
        if flag != "true":
            logger.debug("_run_intake_promotion: flag off — skipping")
            return

        intake_dsn = os.environ.get("MORI_INTAKE_DATABASE_URL", "").strip()
        if not intake_dsn:
            logger.info("_run_intake_promotion: MORI_INTAKE_DATABASE_URL not set — skipping")
            return

        # Postgres-only guard: SQLiteStore does not have an async pool.
        if not hasattr(self.store, "pool"):
            logger.info(
                "_run_intake_promotion: mori canon store is not a PostgresStore "
                "(no 'pool' attribute) — feature UNAVAILABLE on SQLite backend; skipping"
            )
            return

        try:
            import asyncpg

            from mori_intake.assess_model import make_canon_assessor, make_canon_reader_from_store
            from mori_intake.assessor import assess_once
            from mori_intake.canon_writer import drain_once

            # Create a fresh intake pool for this promotion pass.
            # We do NOT use the module-level singleton (intake_db._pool) to
            # avoid lifecycle coupling with the intake HTTP server; instead we
            # create and close our own pool here.
            intake_pool = await asyncpg.create_pool(
                intake_dsn,
                min_size=1,
                max_size=3,
                statement_cache_size=0,
                ssl=False,
            )
            try:
                # Build the read-only canon reader from the mori Postgres store.
                reader = make_canon_reader_from_store(self.store)
                assessor_fn = make_canon_assessor(reader, self.client)

                # This method only runs when MORI_INTAKE_PROMOTION_ENABLED=true
                # (guarded above), so this is the legacy auto-promotion path:
                # enqueue promotion_queue, then drain to canon.  The human-review
                # gate (flag off) is driven by the intake CLI, not the dream.
                assessed = await assess_once(
                    intake_pool, assess=assessor_fn, promotion_enabled=True
                )
                committed = await drain_once(intake_pool, self.store)

                logger.info(
                    "_run_intake_promotion: assessed=%d committed=%d",
                    assessed,
                    committed,
                )
            finally:
                await intake_pool.close()

        except Exception as exc:
            # NEVER propagate into run() — a broken intake must not abort dream.
            logger.error(
                "_run_intake_promotion: error during intake promotion (skipped): %s",
                exc,
                exc_info=True,
            )

    # ── Internal helpers ─────────────────────────────────────────────────

    async def _get_watermark(self) -> int:
        val = await _a(self.store.get_dream_state("last_dreamed_event_id", "0"))
        return int(val) if val else 0

    async def _set_watermark(self, event_id: int, _conn=None) -> None:
        await _a(self.store.set_dream_state("last_dreamed_event_id", str(event_id), _conn=_conn))
        await _a(
            self.store.set_dream_state(
                "last_dreamed_at", datetime.now(timezone.utc).isoformat(), _conn=_conn
            )
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
            _ts = session_events[0].get("timestamp", "?")
            start_ts = (_ts.isoformat() if hasattr(_ts, "isoformat") else str(_ts))[:19]
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
                    # The turn's assistant reasoning (plans/decisions) captured from
                    # the transcript at Stop. Capped to bound the distillation prompt.
                    assistant_text = e.get("assistant_text")
                    if assistant_text:
                        a_text = assistant_text[:_ASSISTANT_DREAM_CAP].replace("\n", " ")
                        items.append(f"  Assistant: {a_text}")

            session_block = f"Session: {sid} ({start_ts}, {client})"
            if items:
                session_block += "\n" + "\n".join(items)
            parts.append(session_block)

        return "\n\n".join(parts)

    def _call_dream_model(self, events_text: str) -> str:
        """Send formatted events to the dream model and return the response.

        The output contract is appended to the BOTTOM of the user payload (after the
        transcript) so it occupies the recency-most position the model reads last.
        """
        return self.client.consult(
            system=DREAM_SYSTEM_PROMPT,
            user=events_text + "\n\n" + OUTPUT_REMINDER,
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

    def _resolve_project(self, cwd: str) -> str | None:
        """Resolve project name from CWD via resolver chain.

        Chain: .mori-project file → MORI_PROJECT env var → git root name.
        """
        if not cwd:
            return None
        p = Path(cwd)
        for candidate in [p, *p.parents]:
            marker = candidate / ".mori-project"
            if marker.exists():
                return marker.read_text().strip().lower() or None
        env_project = os.environ.get("MORI_PROJECT")
        if env_project:
            return env_project.lower()
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode == 0:
                return Path(result.stdout.strip()).name.lower()
        except Exception:
            pass
        return None

    def _extract_project_from_events(self, events: list[dict]) -> str | None:
        """Extract project name from the first SessionStart event's CWD."""
        for ev in events:
            if ev.get("event_name") == "SessionStart":
                name = self._resolve_project(ev.get("cwd", ""))
                if name:
                    return name
        return None

    async def _write_memory(
        self,
        mem: dict,
        name: str,
        action: str,
        batch_session_ids: list[str],
        batch_clients: list[str],
        project: str | None = None,
        _conn: sqlite3.Connection | None = None,
    ) -> str:
        path = mem.get("path", name)
        body = mem.get("body", "")
        tags = ["dream-phase", action.lower()]
        if project:
            tags.append(f"project:{project}")

        await _a(
            self.store.write(
                name=name,
                title=path.replace(".md", "").replace("/", " — "),
                description=mem.get("reason", ""),
                type=self._infer_type(path),
                tier="working",
                body=body,
                tags=tags,
                origin_session_ids=batch_session_ids,
                origin_clients=batch_clients,
                _skip_protection=True,
                _conn=_conn,
            )
        )
        return f"{action} {name}"

    # ── NATS eviction notice ──────────────────────────────────────────

    def _publish_eviction_notice(self, superseded_count: int) -> None:
        """Publish a short NATS message about eviction events from this dream run.

        Fire-and-forget — errors are logged, never propagated.
        """
        import socket

        import nats

        hostname = socket.gethostname().split(".")[0]
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

    async def _contradiction_scan(self, new_memories: list[dict]) -> int:
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

        return await run_contradiction_scan(
            new_memories=new_memories,
            db_path=self.db_path,
            consult_fn=consult_fn,
            store=self.store,
        )
