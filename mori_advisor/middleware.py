"""ASGI middleware for mori-advisor API key authentication.

Intercepts all requests at the transport layer. Open paths (health/ready/metrics)
are always allowed. All other paths require a valid X-Api-Key header.

Failed auth attempts are logged with the client IP for audit purposes.
"""

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from mori_advisor.auth import check_key
from mori_advisor.policy import Actor, current_actor, role_for

logger = logging.getLogger(__name__)

OPEN_PATHS = {"/health", "/ready", "/metrics", "/"}

# Return 404 for OAuth discovery so CC stops treating mori as an OAuth server
# and falls back to using the X-Api-Key header directly
OAUTH_PATHS = {
    "/.well-known/oauth-authorization-server",
    "/.well-known/openid-configuration",
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
    "/register",
}

# Track session IDs that have successfully authenticated via API key
_AUTHENTICATED_SESSIONS: set[str] = set()


class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in OPEN_PATHS:
            return await call_next(request)

        if request.url.path in OAUTH_PATHS:
            return JSONResponse(
                {"error": "Not an OAuth server — use X-Api-Key header"},
                status_code=404,
            )

        # Allow session-based authentication bypass for active SSE connections (POST/DELETE)
        session_id = request.headers.get("mcp-session-id")
        if session_id and session_id in _AUTHENTICATED_SESSIONS:
            response = await call_next(request)
            if request.method == "DELETE" and request.url.path == "/mcp":
                _AUTHENTICATED_SESSIONS.discard(session_id)
                logger.info("Session %s terminated and removed from authenticated list", session_id)
            return response

        provided = (
            request.headers.get("x-api-key")
            or request.query_params.get("api-key")
            or request.query_params.get("api_key")
        )
        client_name = check_key(provided)

        if client_name is None:
            client_ip = request.client.host if request.client else "unknown"
            logger.warning(
                "Auth rejected: invalid or missing X-Api-Key from %s %s",
                client_ip,
                request.url.path,
            )
            return JSONResponse(
                {"error": "Unauthorized", "detail": "Valid X-Api-Key required"},
                status_code=401,
            )

        request.state.mori_client = client_name

        # Build the Actor for this request and attach it to both the request state
        # (for REST endpoints) and the ContextVar (for MCP tools, which have no
        # direct access to the request object).
        actor = Actor(key_name=client_name, role=role_for(client_name))
        request.state.actor = actor
        token = current_actor.set(actor)
        try:
            response = await call_next(request)
        finally:
            # Always reset after the request — prevents actor leaking across tasks.
            current_actor.reset(token)

        # Capture server-issued session ID for subsequent request bypass
        resp_session_id = response.headers.get("mcp-session-id")
        if resp_session_id:
            _AUTHENTICATED_SESSIONS.add(resp_session_id)
            logger.info(
                "Session %s successfully authenticated for client %s", resp_session_id, client_name
            )

        return response
