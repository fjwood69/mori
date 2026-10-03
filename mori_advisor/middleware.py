"""ASGI middleware for mori-advisor API key authentication.

Intercepts all requests at the transport layer. Open paths (health/ready/metrics)
are always allowed. All other paths require a valid X-Api-Key header.

v2.3.12:
  * Keys are accepted ONLY in the X-Api-Key header. A key in the query string
    (``?api-key=`` / ``?api_key=``) is refused with 400 on every path, open ones
    included, before anything else runs — URLs end up in access logs.
  * An MCP session is BOUND to the key that opened it. Every request carrying an
    ``mcp-session-id`` must also carry that key; the actor is set per request and
    the rate limiter runs, exactly as for any other request. Sessions expire
    (idle / absolute TTL) and the registry is capped. An unknown or expired
    session gets 404, the MCP spec's signal for "start a new session".

Failed auth attempts are logged with the client IP for audit purposes.
"""

import hashlib
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from mori_advisor.auth import check_key
from mori_advisor.metrics import (
    record_session_rejection,
    record_url_key_rejection,
    set_mcp_sessions,
)
from mori_advisor.policy import Actor, current_actor, role_for
from mori_advisor.throttle import (
    RateLimitConfig,
    make_rate_limit_store,
    rate_limit_config,
    should_limit,
)

logger = logging.getLogger(__name__)

OPEN_PATHS = {"/health", "/ready", "/metrics", "/", "/review"}

# Rate limiting (#23 D) — config read once at import (fail-loud on a bad spec);
# in-memory store is the single-instance default. Both are module-level so tests
# can monkeypatch them. Disabled unless MORI_RATE_LIMIT is configured to enable.
_rate_cfg: RateLimitConfig = rate_limit_config()
rate_limit_store = make_rate_limit_store()

# Return 404 for OAuth discovery so CC stops treating mori as an OAuth server
# and falls back to using the X-Api-Key header directly
OAUTH_PATHS = {
    "/.well-known/oauth-authorization-server",
    "/.well-known/openid-configuration",
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
    "/register",
}


def _positive_int_env(name: str, default: int) -> int:
    """An exact positive integer from the environment, or *default*. Fail loud on anything else."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    if not raw.isdigit() or int(raw) <= 0:
        raise ValueError(
            f"{name} must be a positive integer number of seconds/entries, got {raw!r}"
        )
    return int(raw)


# Session lifetime (v2.3.12, board R2). Env-tunable; read once at import, fail-loud.
SESSION_IDLE_TTL_S = _positive_int_env("MORI_SESSION_IDLE_TTL_S", 12 * 3600)
SESSION_MAX_AGE_S = _positive_int_env("MORI_SESSION_MAX_AGE_S", 7 * 24 * 3600)
SESSION_CAP = _positive_int_env("MORI_SESSION_CAP", 10_000)

_URL_KEY_PARAMS = ("api-key", "api_key")


class RedactUrlKeys(logging.Filter):
    """Redact ``api-key``/``api_key`` query values from log records (installed on uvicorn's
    access logger at startup). The middleware refuses such a request with 400, but the access log
    line is written regardless — this keeps the key out of it."""

    _PATTERN = re.compile(r"((?:\?|&|%3F|%26)api[-_]key=)[^&\s\"']*", re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = tuple(
                self._PATTERN.sub(r"\1[REDACTED]", a) if isinstance(a, str) else a
                for a in (record.args if isinstance(record.args, tuple) else (record.args,))
            )
        if isinstance(record.msg, str):
            record.msg = self._PATTERN.sub(r"\1[REDACTED]", record.msg)
        return True


def install_access_log_redaction() -> None:
    """Attach :class:`RedactUrlKeys` to uvicorn's access logger (idempotent)."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactUrlKeys) for f in access.filters):
        access.addFilter(RedactUrlKeys())


@dataclass
class _Session:
    key_name: str
    created_at: float
    last_seen: float


# session id -> binding, ordered by last_seen (least recent first) so eviction is O(1).
_SESSIONS: "OrderedDict[str, _Session]" = OrderedDict()


def _sid_tag(session_id: str) -> str:
    """A short, stable hash of a session id for logs — the id itself is a bearer credential."""
    return hashlib.sha256(session_id.encode("utf-8", "replace")).hexdigest()[:12]


def _expired(sess: _Session, now: float) -> bool:
    return now - sess.last_seen > SESSION_IDLE_TTL_S or now - sess.created_at > SESSION_MAX_AGE_S


def _register_session(session_id: str, key_name: str, now: float) -> None:
    _SESSIONS[session_id] = _Session(key_name=key_name, created_at=now, last_seen=now)
    _SESSIONS.move_to_end(session_id)
    while len(_SESSIONS) > SESSION_CAP:
        evicted, _ = _SESSIONS.popitem(last=False)
        logger.info("Session %s evicted (registry cap %d)", _sid_tag(evicted), SESSION_CAP)
    set_mcp_sessions(len(_SESSIONS))


def _forget_session(session_id: str) -> None:
    if _SESSIONS.pop(session_id, None) is not None:
        set_mcp_sessions(len(_SESSIONS))


def _session_gone(detail: str) -> JSONResponse:
    # 404 + a JSON-RPC error, as the MCP SDK answers an unknown session: the client's cue to
    # send a fresh InitializeRequest without a session id.
    return JSONResponse(
        {"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": detail}},
        status_code=404,
    )


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse({"error": "Unauthorized", "detail": detail}, status_code=401)


class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # D2: refuse a key in the URL on EVERY path — before the open-path return, so a key
        # pasted into a /metrics or /health URL is refused too. The value is never logged.
        if any(p in request.query_params for p in _URL_KEY_PARAMS):
            record_url_key_rejection()
            client_ip = request.client.host if request.client else "unknown"
            logger.warning(
                "Refused an API key in the query string from %s %s", client_ip, request.url.path
            )
            return JSONResponse(
                {
                    "error": "Bad Request",
                    "detail": "API keys are not accepted in the URL — send the X-Api-Key header",
                },
                status_code=400,
            )

        if request.url.path in OPEN_PATHS:
            return await call_next(request)

        if request.url.path in OAUTH_PATHS:
            return JSONResponse(
                {"error": "Not an OAuth server — use X-Api-Key header"},
                status_code=404,
            )

        provided = request.headers.get("x-api-key")
        client_name = check_key(provided)
        session_id = request.headers.get("mcp-session-id")

        if client_name is None:
            client_ip = request.client.host if request.client else "unknown"
            if session_id:
                record_session_rejection("no_key" if not provided else "wrong_key")
            logger.warning(
                "Auth rejected: invalid or missing X-Api-Key from %s %s%s",
                client_ip,
                request.url.path,
                f" (session {_sid_tag(session_id)})" if session_id else "",
            )
            return _unauthorized("Valid X-Api-Key required")

        # D4: a request on a session must come from the key that opened it, within its lifetime.
        # Order (board, build ruling B1): the key is validated above, BEFORE any session lookup, so
        # a missing or wrong key is always 401; ownership is checked BEFORE expiry, so a key that
        # does not own the session is always 401 too. Only the owning key learns that its session
        # is gone (404 — the client's cue to re-initialise). An id this process has never seen (or
        # has already forgotten) has no owner to check, so it is 404 for any valid key.
        now = time.time()
        if session_id:
            sess = _SESSIONS.get(session_id)
            if sess is None:
                return _session_gone("Session not found — re-initialise")
            if sess.key_name != client_name:
                record_session_rejection("wrong_key")
                logger.warning(
                    "Session %s presented by key %s but bound to another key — rejected",
                    _sid_tag(session_id),
                    client_name,
                )
                return _unauthorized("This session belongs to a different key")
            if _expired(sess, now):
                _forget_session(session_id)
                record_session_rejection("expired")
                logger.info("Session %s expired (key %s)", _sid_tag(session_id), sess.key_name)
                return _session_gone("Session expired — re-initialise")
            sess.last_seen = now
            _SESSIONS.move_to_end(session_id)

        request.state.mori_client = client_name

        # Build the Actor for this request and attach it to both the request state
        # (for REST endpoints) and the ContextVar (for MCP tools, which have no
        # direct access to the request object).
        actor = Actor(key_name=client_name, role=role_for(client_name))
        request.state.actor = actor

        # Rate limit (#23 D) — keyed on the authenticated key name, applied after
        # auth so only known callers are counted. Scope decides which methods
        # count (writes-only by default). A denied request is rejected before the
        # handler runs; replays of idempotent requests still count (documented).
        # v2.3.12: session requests are no longer exempt.
        if _rate_cfg.enabled and should_limit(request.method, _rate_cfg.scope):
            verdict = await rate_limit_store.check(
                client_name, _rate_cfg.limit, _rate_cfg.window_seconds
            )
            if not verdict.allowed:
                return JSONResponse(
                    {
                        "error": "Too Many Requests",
                        "detail": (
                            f"rate limit exceeded "
                            f"({_rate_cfg.limit} per {_rate_cfg.window_seconds}s)"
                        ),
                    },
                    status_code=429,
                    headers={"Retry-After": str(max(1, int(verdict.retry_after) + 1))},
                )

        token = current_actor.set(actor)
        try:
            response = await call_next(request)
        finally:
            # Always reset after the request — prevents actor leaking across tasks.
            current_actor.reset(token)

        if session_id and request.method == "DELETE" and request.url.path == "/mcp":
            _forget_session(session_id)
            logger.info("Session %s terminated and removed", _sid_tag(session_id))
            return response

        # Bind a server-issued session id to the key that opened it.
        resp_session_id = response.headers.get("mcp-session-id")
        if resp_session_id and resp_session_id not in _SESSIONS:
            _register_session(resp_session_id, client_name, now)
            logger.info("Session %s opened for client %s", _sid_tag(resp_session_id), client_name)

        return response
