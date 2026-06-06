"""Thin, idempotent REST client for the mori memory server.

Uses only the stdlib (urllib, json) — no external dependencies.
All requests carry an X-Api-Key header.  A typed MoriTransportError is
raised for genuine I/O failures so the outbox can detect and retry them.

HTTP status codes are returned to the caller so it can distinguish:
  * 2xx  — success (created/updated/pending)
  * 202  — proposal queued (pending governance review)
  * 429  — rate-limited → caller should back off
  * 4xx  — permanent failure → drop the payload
  * 5xx  — transient server error → retry (treated as transport error)
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
import urllib.response
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 15  # seconds


# ── SEC-003: SSRF-safe opener ─────────────────────────────────────────────────


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that REFUSES all HTTP redirects.

    urllib follows HTTP 3xx redirects by default.  An attacker-controlled
    MORI_SERVER_URL or MORI_INTAKE_URL could redirect requests to internal
    services (e.g. http://169.254.169.254/) and exfiltrate cloud credentials
    via search results.

    This handler replaces the default redirect handler in the custom opener
    used by ``MoriRestClient`` when no ``_opener`` injection seam is supplied.
    It overrides ``redirect_request`` to return ``None`` (no redirect request)
    and raises ``MoriTransportError`` so the caller handles it as a transport
    failure (retry-eligible at the outbox layer).
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: PLR0913
        raise MoriTransportError(
            f"HTTP redirect refused (SSRF guard): {code} → {newurl!r}",
            status_code=code,
        )


def _make_safe_opener() -> urllib.request.OpenerDirector:
    """Build an OpenerDirector that never follows HTTP redirects.

    Installs ``_NoRedirectHandler`` as the sole redirect handler, replacing
    urllib's default ``HTTPRedirectHandler`` which follows 3xx automatically.
    All other handlers (auth, error, etc.) are inherited from the default
    opener construction.
    """
    opener = urllib.request.OpenerDirector()
    # Add standard handlers EXCEPT the redirect handler.
    for cls in (
        urllib.request.UnknownHandler,
        urllib.request.HTTPHandler,
        urllib.request.HTTPDefaultErrorHandler,
        urllib.request.HTTPErrorProcessor,
        urllib.request.FileHandler,
        urllib.request.DataHandler,
        _NoRedirectHandler,  # replaces HTTPRedirectHandler — refuses all redirects
    ):
        opener.add_handler(cls())
    return opener


# Module-level singleton — shared across all MoriRestClient instances that
# don't inject a custom opener.  Thread-safe: openers are stateless.
_SAFE_OPENER: urllib.request.OpenerDirector = _make_safe_opener()


class MoriTransportError(Exception):
    """Raised when the HTTP request fails at the transport layer.

    This covers connection refused, DNS failure, read timeouts, and HTTP 5xx
    responses — all of which are candidates for retry by the outbox drainer.
    The underlying exception (if any) is available as ``.__cause__``.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class MoriRestClient:
    """Minimal REST client for mori's memory API.

    Instantiate once and reuse across requests.  Thread-safe (no mutable state
    after construction; urllib is threadsafe for independent requests).

    The ``_opener`` parameter is an injection seam for tests: pass a callable
    with the same signature as ``urllib.request.urlopen`` to intercept HTTP
    calls without a live server.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: int = _DEFAULT_TIMEOUT,
        _opener: Any = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        # Use the injected opener (test seam) or the module-level SSRF-safe opener
        # (production).  Never fall back to urllib.request.urlopen directly because
        # it follows redirects — see _SAFE_OPENER / _NoRedirectHandler (SEC-003).
        self._urlopen = _opener if _opener is not None else _SAFE_OPENER.open

    # ── Public methods ──────────────────────────────────────────────────────

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Full-text search over the memory store.

        GET /api/memories?query=<query>&limit=<limit>

        Returns a list of memory dicts (name, title, description, type, tags,
        body, …).  Returns an empty list on any failure so the caller can
        degrade gracefully.
        """
        params = urllib.parse.urlencode({"query": query, "limit": limit})
        url = f"{self._base_url}/api/memories?{params}"
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with self._urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode())
                return data.get("memories", [])
        except urllib.error.HTTPError as exc:
            if exc.code >= 500:
                raise MoriTransportError(
                    f"Server error on search: HTTP {exc.code}", status_code=exc.code
                ) from exc
            logger.warning("mori search HTTP %s — returning empty", exc.code)
            return []
        except Exception as exc:
            raise MoriTransportError(f"Transport failure on search: {exc}") from exc

    def propose(
        self,
        name: str,
        body: str,
        type: str = "project",
        tags: list[str] | None = None,
        title: str = "",
        description: str = "",
        idempotency_key: str = "",
    ) -> tuple[int, dict]:
        """POST a memory proposal to /api/memories.

        Returns ``(status_code, response_body)`` so the caller can branch on:
          * 201 / 200 / 202 — written / updated / pending proposal
          * 429 — rate-limited; caller should back off
          * 4xx — bad request; should be dropped
          * Raises MoriTransportError on connection failure or 5xx.
        """
        payload: dict[str, Any] = {
            "name": name,
            "title": title or name,
            "description": description,
            "type": type,
            "body": body,
            "tags": tags or [],
        }
        data = json.dumps(payload).encode()

        headers = self._headers()
        headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        req = urllib.request.Request(
            f"{self._base_url}/api/memories",
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with self._urlopen(req, timeout=self._timeout) as resp:
                body_bytes = resp.read().decode()
                return resp.status, json.loads(body_bytes) if body_bytes else {}
        except urllib.error.HTTPError as exc:
            body_bytes = exc.read().decode() if exc.fp else ""
            try:
                resp_body = json.loads(body_bytes) if body_bytes else {}
            except Exception:
                resp_body = {"raw": body_bytes}
            if exc.code >= 500:
                raise MoriTransportError(
                    f"Server error proposing {name!r}: HTTP {exc.code}",
                    status_code=exc.code,
                ) from exc
            # 4xx / 429 — return to caller for dispatch
            return exc.code, resp_body
        except Exception as exc:
            raise MoriTransportError(f"Transport failure proposing {name!r}: {exc}") from exc

    def list_pending(self, status: str = "") -> list[dict]:
        """List the caller's own pending proposals.

        GET /api/pending/mine?status=<status>

        ``status`` is one of "pending", "approved", "rejected", or "" (all).
        Returns a list of pending-write dicts on success, or an empty list on
        any failure.
        """
        params = {}
        if status:
            params["status"] = status
        qs = f"?{urllib.parse.urlencode(params)}" if params else ""
        url = f"{self._base_url}/api/pending/mine{qs}"
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with self._urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode())
                return data.get("items", [])
        except urllib.error.HTTPError as exc:
            if exc.code >= 500:
                raise MoriTransportError(
                    f"Server error listing pending: HTTP {exc.code}", status_code=exc.code
                ) from exc
            logger.warning("mori list_pending HTTP %s — returning empty", exc.code)
            return []
        except Exception as exc:
            raise MoriTransportError(f"Transport failure listing pending: {exc}") from exc

    def submit_intake(
        self,
        session_id: str,
        agent_id: str,
        target: str,
        action: str,
        stable_key: str,
        content: str,
        provenance: dict | None = None,
    ) -> tuple[int, dict]:
        """POST a memory proposal to the intake governance service.

        Target: ``{intake_base}/intake/submissions``

        Returns ``(status_code, response_body)`` so the drain loop can branch:
          * 202           — accepted (may include ``"duplicate": true``)
          * 422           — policy rejection — dead-letter, never retry
          * 429           — rate-limited — caller should back off + retry
          * other 4xx     — dead-letter (bad payload)
          * Raises MoriTransportError on connection failure or 5xx.
        """
        payload: dict = {
            "session_id": session_id,
            "agent_id": agent_id,
            "target": target,
            "action": action,
            "stable_key": stable_key,
            "content": content,
        }
        if provenance is not None:
            payload["provenance"] = provenance

        data = json.dumps(payload).encode()
        headers = self._headers()
        headers["Content-Type"] = "application/json"

        req = urllib.request.Request(
            f"{self._base_url}/intake/submissions",
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with self._urlopen(req, timeout=self._timeout) as resp:
                body_bytes = resp.read().decode()
                return resp.status, json.loads(body_bytes) if body_bytes else {}
        except urllib.error.HTTPError as exc:
            body_bytes = exc.read().decode() if exc.fp else ""
            try:
                resp_body = json.loads(body_bytes) if body_bytes else {}
            except Exception:
                resp_body = {"raw": body_bytes}
            if exc.code >= 500:
                raise MoriTransportError(
                    f"Server error submitting intake {stable_key!r}: HTTP {exc.code}",
                    status_code=exc.code,
                ) from exc
            # 4xx / 429 — return to caller for dispatch
            return exc.code, resp_body
        except Exception as exc:
            raise MoriTransportError(
                f"Transport failure submitting intake {stable_key!r}: {exc}"
            ) from exc

    def get_memory(self, name: str) -> dict | None:
        """Fetch a single canon memory by exact name.

        GET /api/memories/<name>

        Returns the memory dict on success, ``None`` on 404 (no such canon
        memory). Used by reconciliation to compare canon content-hash against
        the local working-memory entry. Raises ``MoriTransportError`` on 5xx /
        transport failure so the caller can degrade gracefully.

        NOTE: added during the v0.2.0 rewrite — ``search`` is fuzzy and cannot
        guarantee an exact-name hit, so reconciliation needs a direct lookup.
        """
        url = f"{self._base_url}/api/memories/{urllib.parse.quote(name, safe='')}"
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with self._urlopen(req, timeout=self._timeout) as resp:
                body_bytes = resp.read().decode()
                if not body_bytes:
                    return None
                data = json.loads(body_bytes)
                # Accept either a bare memory dict or {"memory": {...}}.
                if isinstance(data, dict) and "memory" in data:
                    return data["memory"]
                return data if isinstance(data, dict) else None
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code >= 500:
                raise MoriTransportError(
                    f"Server error fetching {name!r}: HTTP {exc.code}", status_code=exc.code
                ) from exc
            logger.warning("mori get_memory HTTP %s for %r — treating as absent", exc.code, name)
            return None
        except Exception as exc:
            raise MoriTransportError(f"Transport failure fetching {name!r}: {exc}") from exc

    # ── Internals ───────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self._api_key}
