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

logger = logging.getLogger(__name__)

OPEN_PATHS = {"/health", "/ready", "/metrics", "/"}


class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in OPEN_PATHS:
            return await call_next(request)

        provided = request.headers.get("x-api-key")
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
        return await call_next(request)
