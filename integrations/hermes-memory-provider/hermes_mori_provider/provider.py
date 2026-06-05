"""MoriMemoryProvider — hermes-agent MemoryProvider plugin.

Wires together MoriRestClient, GovernedWriteOutbox, and HermesEventNormalizer
to mirror the agent's durable learnings to a self-hosted mori server as
governed proposals, and to let the agent recall from mori.

hermes-agent discovers providers via the ``register`` entry point and calls
them duck-typed — this module does NOT import any hermes-agent ABC.

Plugin entry point
------------------
    from hermes_mori_provider import register

    register(ctx)  # ctx.register_memory_provider(provider_instance)

MemoryProvider interface (duck-typed)
--------------------------------------
Required:
  name                                         → str
  is_available()                               → bool
  initialize(session_id, **kwargs)             → None
  get_tool_schemas()                           → list[dict]
  handle_tool_call(tool_name, args, **kwargs)  → Any
  get_config_schema()                          → list[dict]
  save_config(values, hermes_home)             → None

Optional hooks (all no-ops safe to omit):
  prefetch(query, *, session_id="")            → str
  sync_turn(user_content, assistant_content, *, session_id="", messages=None) → None
  on_session_end(messages)                     → None
  system_prompt_block()                        → str
  on_pre_compress(messages)                    → None
  on_memory_write(action, target, content)     → None
  shutdown()                                   → None
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
    """hermes-agent MemoryProvider that mirrors durable learnings to mori.

    The provider is intentionally defensive: every method that calls the
    network is wrapped so that failures are logged but never raised into
    the agent — hermes-agent must never be disrupted by a mori outage.
    """

    # ── Identity ──────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "mori"

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """Return True iff MORI_API_KEY is set in the environment.

        Deliberately performs NO network call — hermes-agent may call this
        frequently to check provider readiness.
        """
        return bool(os.environ.get("MORI_API_KEY", "").strip())

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        """Set up the client, normaliser, and outbox.

        ``kwargs`` contains at minimum ``hermes_home`` (a ``Path``-like value
        pointing to the agent's data directory).  Config is read from the
        persisted config file written by ``save_config``.
        """
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
        """Return the list of configuration fields for this provider.

        hermes-agent uses this to render a setup wizard.  Secrets are stored
        in .env; non-secrets go to the plain config file.
        """
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
        """Return READ-ONLY tool schemas the agent may call.

        The agent is intentionally limited to reading back its own proposals
        and searching the store.  It cannot approve its own proposals — that
        requires a human reviewer with the ``dreamer`` role.
        """
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
                            "description": "The proposal name (e.g. 'hermes.my-learning')",
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
        """Search mori for relevant context and return a formatted block.

        Called by hermes-agent before each turn.  NEVER raises — failures
        return an empty string so the agent proceeds normally.
        """
        try:
            results = self._client.search(query=query, limit=_SEARCH_RESULT_LIMIT)
            if not results:
                return ""
            return _format_search_results(results)
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
        """No-op — turn-level syncing is not required by this provider."""
        return

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        """Mirror a durable memory write to mori as a governed proposal.

        NON-BLOCKING — normalisation is cheap, and the outbox enqueue is a
        single SQLite INSERT that returns immediately.  The network call
        happens in the background drainer thread.
        """
        try:
            payload = self._normalizer.normalize(action=action, target=target, content=content)
            if payload is None:
                logger.debug("mori: dropping ephemeral write action=%s target=%r", action, target)
                return
            enqueued = self._outbox.enqueue(payload)
            if not enqueued:
                logger.warning(
                    "mori: outbox backpressure — proposal dropped for %r", payload.get("name")
                )
        except Exception as exc:
            # Never propagate into the agent.
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
            # Indent body for readability; truncate very long bodies.
            snippet = body[:500].rstrip()
            if len(body) > 500:
                snippet += " …"
            for line in snippet.splitlines():
                lines.append(f"   {line}")
    return "\n".join(lines)
