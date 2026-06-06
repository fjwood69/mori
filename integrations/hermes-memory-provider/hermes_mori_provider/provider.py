"""MoriMemoryProvider — hermes-agent MemoryProvider plugin.

Two-tier proxy over a GOVERNED mori store:

* **Local Working Memory (LWM)** — a strongly-consistent SQLite overlay so the
  agent's own writes are visible to ``prefetch`` immediately (read-your-writes),
  before any governance approval.
* **Async governed-proposal pipeline** — the existing crash-durable outbox
  drains LWM writes to mori as PROPOSALS (held pending until a human "dreamer"
  approves them into canon).

hermes-agent calls this provider duck-typed via the real MemoryProvider
contract; this module does NOT import any hermes-agent ABC.

Hook choices (validated architecture)
-------------------------------------
* ``on_memory_write`` is the ONLY hook that drives mori proposals. It fires only
  when the agent's built-in memory tool edits MEMORY.md / USER.md.
* ``sync_turn`` is an explicit **no-op** — mirroring every turn would flood the
  dreamer queue with noise.
* ``prefetch`` merges the LWM overlay with mori canon (LWM wins on collision),
  and opportunistically reconciles LWM against canon. It NEVER raises into the
  agent.

Real ``on_memory_write`` contract
---------------------------------
``action`` in {"add", "replace", "remove"}; ``target`` in {"memory", "user"};
plus ``content`` and optional ``metadata``. There is NO durability/ephemeral
concept.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_SERVER_URL = "http://localhost:8968"
_SEARCH_RESULT_LIMIT = 8
_FLUSH_TIMEOUT = 8.0


class MoriMemoryProvider:
    """hermes-agent MemoryProvider mirroring durable learnings to mori.

    Every method that touches the network is wrapped so failures are logged but
    never raised into the agent — hermes must never be disrupted by a mori
    outage.
    """

    # ── Identity ──────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "mori"

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """Return True iff MORI_API_KEY is set. Performs NO network call."""
        return bool(os.environ.get("MORI_API_KEY", "").strip())

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        """Set up the client, normaliser, and outbox (with LWM table)."""
        from .normalizer import HermesEventNormalizer
        from .outbox import GovernedWriteOutbox
        from .rest_client import MoriRestClient

        self._session_id = session_id
        hermes_home = Path(kwargs.get("hermes_home", Path.home() / ".hermes"))

        cfg = self._load_config(hermes_home)
        server_url: str = (
            cfg.get("server_url") or os.environ.get("MORI_SERVER_URL") or _DEFAULT_SERVER_URL
        )
        api_key: str = cfg.get("api_key") or os.environ.get("MORI_API_KEY", "")

        self._client = MoriRestClient(base_url=server_url, api_key=api_key)
        self._normalizer = HermesEventNormalizer()
        self._outbox = GovernedWriteOutbox(
            client=self._client,
            db_path=hermes_home / "mori_outbox.db",
        )
        logger.info("mori provider initialised (server=%s, session=%s)", server_url, session_id)

    # ── Config schema ──────────────────────────────────────────────────────

    def get_config_schema(self) -> list[dict[str, Any]]:
        """Declare configuration fields so hermes can manage them standardly."""
        return [
            {
                "key": "server_url",
                "description": (
                    "URL of your mori server "
                    "(e.g. http://localhost:8968 or https://mori.example.com)"
                ),
                "secret": False,
                "required": False,
                "env_var": "MORI_SERVER_URL",
                "default": _DEFAULT_SERVER_URL,
            },
            {
                "key": "api_key",
                "description": (
                    "mori API key (X-Api-Key header). "
                    'Generate one with: python -c "import secrets; print(secrets.token_hex(32))"'
                ),
                "secret": True,
                "required": True,
                "env_var": "MORI_API_KEY",
            },
        ]

    def save_config(self, values: dict[str, Any], hermes_home: Path | str) -> None:
        """Persist non-secret config values to ``hermes_home/mori_config.json``."""
        import json

        hermes_home = Path(hermes_home)
        hermes_home.mkdir(parents=True, exist_ok=True)
        cfg_path = hermes_home / "mori_config.json"
        safe = {k: v for k, v in values.items() if k != "api_key"}
        cfg_path.write_text(json.dumps(safe, indent=2))
        logger.debug("mori config saved to %s", cfg_path)

    # ── Tool schemas ───────────────────────────────────────────────────────

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Return READ-ONLY tool schemas. The agent cannot approve its own work."""
        return [
            {
                "name": "mori_search",
                "description": (
                    "Search the shared mori memory store for relevant context. "
                    "Returns a list of matching memories as formatted text."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Natural-language search query",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of results (default 8)",
                            "default": _SEARCH_RESULT_LIMIT,
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "mori_list_pending",
                "description": (
                    "List this agent's own pending proposals awaiting governance review. "
                    "Use this to check which learnings are awaiting approval."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "description": (
                                "Filter by status: 'pending', 'approved', 'rejected', "
                                "or '' for all."
                            ),
                            "default": "",
                        }
                    },
                    "required": [],
                },
            },
            {
                "name": "mori_proposal_status",
                "description": (
                    "Check the governance status of a specific proposal by name. "
                    "Returns the proposal detail from the pending queue."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "The proposal name (e.g. 'hermes-memory-my-learning')",
                        }
                    },
                    "required": ["name"],
                },
            },
        ]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> Any:
        """Dispatch a tool call from hermes-agent to the mori REST API."""
        if tool_name == "mori_search":
            return self._handle_search(args)
        if tool_name == "mori_list_pending":
            return self._handle_list_pending(args)
        if tool_name == "mori_proposal_status":
            return self._handle_proposal_status(args)
        return {"error": f"Unknown tool: {tool_name!r}"}

    # ── Memory hooks ───────────────────────────────────────────────────────

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall: merge LWM overlay + mori canon, LWM winning on collision.

        Called before each turn. NEVER raises — returns "" on any failure so
        the agent proceeds normally. ``session_id`` is KEYWORD-ONLY (a
        positional signature would raise TypeError in hermes and silently fail).
        """
        try:
            # Opportunistic, best-effort reconciliation of the LWM overlay.
            self._reconcile_safe()

            lwm_rows = self._safe_lwm_all()
            canon = self._safe_search(query, _SEARCH_RESULT_LIMIT)

            merged = self._merge(lwm_rows, canon)
            if not merged:
                return ""
            return _format_search_results(merged)
        except Exception as exc:
            logger.warning("mori prefetch failed (query=%r): %s", query, exc)
            return ""

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict] | None = None,
    ) -> None:
        """Explicit NO-OP.

        Mirroring every turn would flood the dreamer governance queue with
        noise. Only ``on_memory_write`` drives mori proposals.
        """
        return

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Bridge hook: mirror a memory-tool edit to LWM + the outbox.

        Synchronous LWM write (read-your-writes) + non-blocking outbox enqueue.
        ``action`` in {"add","replace","remove"}; ``target`` in {"memory","user"}.
        Never raises into the agent.
        """
        try:
            desc = self._normalizer.normalize(
                action=action, target=target, content=content, metadata=metadata
            )
            op = desc["op"]
            name = desc["name"]

            payload = {
                "op": op,
                "name": name,
                "title": desc["title"],
                "description": desc["description"],
                "type": desc["type"],
                "body": desc["content"],
                "tags": desc["tags"],
                "idempotency_key": desc["content_hash"],
            }

            if op == "retract":
                self._handle_retract(desc, payload)
                return

            # add (propose) / replace (supersede): LWM overlay first (sync),
            # then enqueue the governed proposal (non-blocking).
            self._outbox.lwm_upsert(
                name=name,
                target=desc["target"],
                content=desc["content"],
                content_hash=desc["content_hash"],
                session_id=self._session_id,
                status="pending",
            )
            enqueued = self._outbox.enqueue(payload)
            if not enqueued:
                logger.warning("mori: outbox backpressure — proposal dropped for %r", name)
        except Exception as exc:
            logger.error(
                "mori on_memory_write failed (action=%s target=%r): %s", action, target, exc
            )

    def on_session_end(self, messages: list[dict]) -> None:
        """Best-effort drain the outbox before the session terminates."""
        try:
            if hasattr(self, "_outbox"):
                drained = self._outbox.flush(timeout=_FLUSH_TIMEOUT)
                if not drained:
                    logger.warning("mori: outbox flush timed out at session end")
        except Exception as exc:
            logger.warning("mori on_session_end flush failed: %s", exc)

    def system_prompt_block(self) -> str:
        """Return a short static note for the agent's system prompt."""
        return (
            "[mori memory] Durable learnings from this session are mirrored "
            "to a governed shared memory store as proposals. A human reviewer "
            "must approve them before they become canon. You may use "
            "mori_search to recall past context, and mori_list_pending to see "
            "your outstanding proposals."
        )

    def on_pre_compress(self, messages: list[dict]) -> None:
        """No-op hook — called before context compression."""
        return

    def shutdown(self) -> None:
        """Shut down the background drainer thread cleanly."""
        try:
            if hasattr(self, "_outbox"):
                self._outbox.shutdown()
        except Exception as exc:
            logger.warning("mori shutdown error: %s", exc)

    # ── on_memory_write helpers ──────────────────────────────────────────────

    def _handle_retract(self, desc: dict[str, Any], payload: dict[str, Any]) -> None:
        """Handle a ``remove`` action.

        If the prior proposal is still unsent in the outbox, ``enqueue`` cancels
        it (add-then-remove while local = no-op) and we drop the LWM row too.
        Otherwise we emit a retraction proposal (mori never hard-deletes canon)
        and leave the LWM row visible until governance acts.
        """
        name = payload["name"]
        had_unsent = self._outbox.pending_count() > 0 and self._unsent_exists(name)

        # enqueue() handles both cases: cancels an unsent row, else emits retract.
        self._outbox.enqueue(payload)

        if had_unsent:
            # Nothing was ever sent — remove the optimistic overlay too.
            self._outbox.lwm_delete(name)
            logger.info("mori: retract cancelled never-sent proposal %r (LWM cleared)", name)
        else:
            # A retraction proposal is now in flight; keep the LWM row but mark
            # it so prefetch can de-emphasise it. We leave content intact for
            # the reviewer; status stays pending until the dreamer decides.
            logger.info("mori: retraction proposal enqueued for %r", name)

    def _unsent_exists(self, name: str) -> bool:
        """True if an unsent outbox row exists for *name* (best-effort)."""
        try:
            return self._outbox._unsent_row_for(name) is not None
        except Exception:
            return False

    # ── Reconciliation ───────────────────────────────────────────────────────

    def _reconcile_safe(self) -> None:
        """Reconcile LWM pending rows against mori. Never raises."""
        try:
            self._reconcile()
        except Exception as exc:
            logger.debug("mori reconciliation skipped: %s", exc)

    def _reconcile(self) -> None:
        """Promote/evict LWM rows by comparing against mori canon + pending.

        For each LWM row still ``pending``:
          * If mori has a canon memory with the same name:
              - content-hash matches  -> promote LWM row to ``canon``.
              - content-hash diverges -> a dreamer edited it before approval;
                overwrite the LWM row with the canon version (canon wins).
          * Else if the proposal appears in the pending queue with a
            rejected/declined status -> mark the LWM row ``rejected``.
          * Else -> still pending; leave as-is.
        """
        from .normalizer import content_hash
        from .outbox import LWM_CANON, LWM_PENDING, LWM_REJECTED

        rows = [
            r for r in self._outbox.lwm_all(exclude_rejected=True) if r["status"] == LWM_PENDING
        ]
        if not rows:
            return

        # Build a name -> status map from the pending queue (rejected detection).
        try:
            pending_items = self._client.list_pending(status="")
        except Exception:
            pending_items = []
        pending_status: dict[str, str] = {}
        for item in pending_items:
            iname = item.get("name")
            if iname:
                pending_status[iname] = str(item.get("status", "")).lower()

        for row in rows:
            name = row["name"]
            # Direct canon lookup (exact name).
            try:
                canon = self._client.get_memory(name)
            except Exception:
                canon = None

            if canon is not None:
                canon_body = canon.get("body", "")
                canon_hash = content_hash(canon_body)
                if canon_hash == row["content_hash"]:
                    self._outbox.lwm_mark(name, LWM_CANON)
                    logger.debug("mori reconcile: promoted %r to canon (hash match)", name)
                else:
                    # Dreamer edited before approving — canon wins.
                    self._outbox.lwm_set_content(name, canon_body, canon_hash, LWM_CANON)
                    logger.info("mori reconcile: %r diverged — overwrote LWM with canon", name)
                continue

            # No canon yet — was it rejected?
            st = pending_status.get(name, "")
            if st in ("rejected", "declined", "denied"):
                self._outbox.lwm_mark(name, LWM_REJECTED)
                logger.info(
                    "mori reconcile: %r rejected by governance — evicted from overlay", name
                )

    # ── Merge / read helpers ─────────────────────────────────────────────────

    def _merge(
        self, lwm_rows: list[dict[str, Any]], canon: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Merge LWM overlay with canon search results, LWM winning by name.

        LWM rows are mapped to the search-result shape so formatting is uniform.
        On name collision the LWM (optimistic) entry replaces the canon entry.
        LWM-only rows are appended (most-recent first, since lwm_all is ordered).
        """
        from .outbox import LWM_CANON, LWM_PENDING

        by_name: dict[str, dict[str, Any]] = {}
        ordered: list[str] = []

        for mem in canon:
            nm = mem.get("name", "")
            if nm and nm not in by_name:
                ordered.append(nm)
            by_name[nm] = mem

        for row in lwm_rows:
            nm = row["name"]
            status = row.get("status", LWM_PENDING)
            overlay = {
                "name": nm,
                "title": nm,
                "description": (
                    "(pending governance review)" if status == LWM_PENDING else "(local)"
                ),
                "body": row.get("content", ""),
                "_lwm_status": status,
            }
            if nm not in by_name:
                ordered.append(nm)
            # LWM wins on collision UNLESS it is already canon and identical;
            # either way the optimistic/local copy is acceptable to show.
            if status == LWM_CANON and nm in by_name:
                # Prefer the richer canon record but keep it ordered.
                continue
            by_name[nm] = overlay

        return [by_name[nm] for nm in ordered if nm in by_name]

    def _safe_lwm_all(self) -> list[dict[str, Any]]:
        try:
            return self._outbox.lwm_all(exclude_rejected=True)
        except Exception as exc:
            logger.debug("mori: lwm_all failed: %s", exc)
            return []

    def _safe_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        try:
            return self._client.search(query=query, limit=limit)
        except Exception as exc:
            logger.debug("mori: canon search failed: %s", exc)
            return []

    # ── Tool handlers ───────────────────────────────────────────────────────

    def _handle_search(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query", ""))
        limit = int(args.get("limit", _SEARCH_RESULT_LIMIT))
        try:
            results = self._client.search(query=query, limit=limit)
            return {
                "results": results,
                "count": len(results),
                "formatted": _format_search_results(results),
            }
        except Exception as exc:
            logger.warning("mori_search tool error: %s", exc)
            return {"error": str(exc), "results": [], "count": 0, "formatted": ""}

    def _handle_list_pending(self, args: dict[str, Any]) -> dict[str, Any]:
        status = str(args.get("status", ""))
        try:
            items = self._client.list_pending(status=status)
            return {"items": items, "count": len(items)}
        except Exception as exc:
            logger.warning("mori_list_pending tool error: %s", exc)
            return {"error": str(exc), "items": [], "count": 0}

    def _handle_proposal_status(self, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name", ""))
        try:
            items = self._client.list_pending(status="")
            match = [i for i in items if i.get("name") == name]
            if match:
                return {"found": True, "proposal": match[0]}
            return {"found": False, "name": name, "message": "No pending proposal with that name"}
        except Exception as exc:
            logger.warning("mori_proposal_status tool error: %s", exc)
            return {"error": str(exc), "found": False}

    # ── Config helpers ─────────────────────────────────────────────────────

    def _load_config(self, hermes_home: Path) -> dict[str, Any]:
        import json

        cfg_path = hermes_home / "mori_config.json"
        if cfg_path.exists():
            try:
                return json.loads(cfg_path.read_text())
            except Exception as exc:
                logger.warning("mori: could not read config from %s: %s", cfg_path, exc)
        return {}


# ── Formatting ─────────────────────────────────────────────────────────────


def _format_search_results(results: list[dict[str, Any]]) -> str:
    """Format a list of mori memory dicts as a readable context block."""
    if not results:
        return ""
    lines = ["[mori context]"]
    for i, mem in enumerate(results, 1):
        name = mem.get("name", "?")
        title = mem.get("title") or name
        description = mem.get("description", "")
        body = mem.get("body", "")
        lines.append(f"\n{i}. **{title}** (`{name}`)")
        if description:
            lines.append(f"   {description}")
        if body:
            snippet = body[:500].rstrip()
            if len(body) > 500:
                snippet += " …"
            for line in snippet.splitlines():
                lines.append(f"   {line}")
    return "\n".join(lines)
