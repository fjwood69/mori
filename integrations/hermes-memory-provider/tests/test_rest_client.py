"""Tests for MoriRestClient.

Uses an injectable opener seam (the ``_opener`` constructor argument) so no
live network is required.  All HTTP calls are intercepted by fake openers.

Verifies:
  * X-Api-Key header is sent on every request.
  * Idempotency-Key header is sent when provided.
  * Status codes are parsed and returned correctly (201, 202, 400, 429).
  * 5xx response raises MoriTransportError.
  * Transport failure (connection refused) raises MoriTransportError.
  * list_pending status param is forwarded correctly.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from hermes_mori_provider.rest_client import MoriRestClient, MoriTransportError

# ── Fake opener helpers ───────────────────────────────────────────────────────


def _make_response(status: int, body: dict) -> Any:
    """Create a fake response object that urllib.request.urlopen would return."""
    body_bytes = json.dumps(body).encode()
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = body_bytes
    resp.__enter__ = lambda self: self
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _opener_returning(status: int, body: dict) -> Any:
    """Return a callable that acts like urlopen and returns the given response."""
    resp = _make_response(status, body)

    def opener(req: Any, timeout: int = 15) -> Any:
        return resp

    return opener, resp


def _opener_http_error(status: int, body: dict) -> Any:
    """Return a callable that raises an HTTPError with the given status."""
    body_bytes = json.dumps(body).encode()

    def opener(req: Any, timeout: int = 15) -> Any:
        raise urllib.error.HTTPError(
            url="http://test/",
            code=status,
            msg="error",
            hdrs={},  # type: ignore[arg-type]
            fp=io.BytesIO(body_bytes),
        )

    return opener


def _opener_connection_error() -> Any:
    """Return a callable that raises an OSError (connection refused)."""

    def opener(req: Any, timeout: int = 15) -> Any:
        raise OSError("connection refused")

    return opener


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _client(opener: Any = None, api_key: str = "test-key-abc") -> MoriRestClient:
    return MoriRestClient(
        base_url="http://localhost:8968",
        api_key=api_key,
        timeout=5,
        _opener=opener,
    )


# ── Header tests ──────────────────────────────────────────────────────────────


class TestHeaders:
    """X-Api-Key and Idempotency-Key are sent correctly."""

    def test_x_api_key_sent_on_search(self) -> None:
        captured: list[Any] = []

        def opener(req: Any, timeout: int = 15) -> Any:
            captured.append(req)
            resp = _make_response(200, {"memories": []})
            return resp

        c = _client(opener=opener)
        c.search("test query")
        assert len(captured) == 1
        assert captured[0].get_header("X-api-key") == "test-key-abc"

    def test_x_api_key_sent_on_propose(self) -> None:
        captured: list[Any] = []

        def opener(req: Any, timeout: int = 15) -> Any:
            captured.append(req)
            resp = _make_response(201, {"status": "created", "name": "x"})
            return resp

        c = _client(opener=opener)
        c.propose(name="hermes.test", body="body")
        assert captured[0].get_header("X-api-key") == "test-key-abc"

    def test_idempotency_key_sent_when_provided(self) -> None:
        captured: list[Any] = []

        def opener(req: Any, timeout: int = 15) -> Any:
            captured.append(req)
            return _make_response(201, {"status": "created", "name": "x"})

        c = _client(opener=opener)
        c.propose(name="hermes.idem", body="body", idempotency_key="idem-xyz-123")
        assert captured[0].get_header("Idempotency-key") == "idem-xyz-123"

    def test_no_idempotency_key_when_empty(self) -> None:
        captured: list[Any] = []

        def opener(req: Any, timeout: int = 15) -> Any:
            captured.append(req)
            return _make_response(201, {"status": "created", "name": "x"})

        c = _client(opener=opener)
        c.propose(name="hermes.no-idem", body="body", idempotency_key="")
        # urllib capitalises the first letter; no key should be set.
        assert captured[0].get_header("Idempotency-key") is None

    def test_x_api_key_sent_on_list_pending(self) -> None:
        captured: list[Any] = []

        def opener(req: Any, timeout: int = 15) -> Any:
            captured.append(req)
            return _make_response(200, {"items": [], "count": 0})

        c = _client(opener=opener)
        c.list_pending()
        assert captured[0].get_header("X-api-key") == "test-key-abc"


# ── Status code tests ─────────────────────────────────────────────────────────


class TestStatusCodes:
    """propose() returns (status_code, body) for 2xx / 202 / 4xx / 429."""

    def test_201_created(self) -> None:
        opener, _ = _opener_returning(201, {"status": "created", "name": "x"})
        c = _client(opener=opener)
        status, body = c.propose(name="hermes.x", body="b")
        assert status == 201
        assert body["status"] == "created"

    def test_202_pending(self) -> None:
        opener, _ = _opener_returning(202, {"status": "pending", "name": "y"})
        c = _client(opener=opener)
        status, body = c.propose(name="hermes.y", body="b")
        assert status == 202
        assert body["status"] == "pending"

    def test_429_returned_not_raised(self) -> None:
        opener = _opener_http_error(429, {"error": "rate limited"})
        c = _client(opener=opener)
        status, body = c.propose(name="hermes.z", body="b")
        assert status == 429

    def test_400_returned_not_raised(self) -> None:
        opener = _opener_http_error(400, {"error": "bad request"})
        c = _client(opener=opener)
        status, body = c.propose(name="hermes.bad", body="b")
        assert status == 400

    def test_500_raises_transport_error(self) -> None:
        opener = _opener_http_error(500, {"error": "server error"})
        c = _client(opener=opener)
        with pytest.raises(MoriTransportError) as exc_info:
            c.propose(name="hermes.srv", body="b")
        assert exc_info.value.status_code == 500

    def test_connection_error_raises_transport_error(self) -> None:
        c = _client(opener=_opener_connection_error())
        with pytest.raises(MoriTransportError):
            c.propose(name="hermes.conn", body="b")


# ── Search tests ──────────────────────────────────────────────────────────────


class TestSearch:
    def test_returns_memories_list(self) -> None:
        mems = [{"name": "hermes.a", "title": "A", "body": "body a"}]
        opener, _ = _opener_returning(200, {"memories": mems, "count": 1})
        c = _client(opener=opener)
        results = c.search("test")
        assert results == mems

    def test_empty_on_404(self) -> None:
        opener = _opener_http_error(404, {"error": "not found"})
        c = _client(opener=opener)
        results = c.search("something")
        assert results == []

    def test_500_raises_transport_error(self) -> None:
        opener = _opener_http_error(500, {"error": "server error"})
        c = _client(opener=opener)
        with pytest.raises(MoriTransportError):
            c.search("query")

    def test_query_param_in_url(self) -> None:
        captured: list[Any] = []

        def opener(req: Any, timeout: int = 15) -> Any:
            captured.append(req)
            return _make_response(200, {"memories": []})

        c = _client(opener=opener)
        c.search("hello world", limit=5)
        url = captured[0].full_url
        assert "query=hello+world" in url or "query=hello%20world" in url
        assert "limit=5" in url


# ── list_pending tests ────────────────────────────────────────────────────────


class TestListPending:
    def test_returns_items(self) -> None:
        items = [{"name": "hermes.x", "status": "pending"}]
        opener, _ = _opener_returning(200, {"items": items, "count": 1})
        c = _client(opener=opener)
        result = c.list_pending()
        assert result == items

    def test_status_param_forwarded(self) -> None:
        captured: list[Any] = []

        def opener(req: Any, timeout: int = 15) -> Any:
            captured.append(req)
            return _make_response(200, {"items": [], "count": 0})

        c = _client(opener=opener)
        c.list_pending(status="approved")
        assert "status=approved" in captured[0].full_url

    def test_no_status_param_when_empty(self) -> None:
        captured: list[Any] = []

        def opener(req: Any, timeout: int = 15) -> Any:
            captured.append(req)
            return _make_response(200, {"items": [], "count": 0})

        c = _client(opener=opener)
        c.list_pending(status="")
        assert "status=" not in captured[0].full_url

    def test_empty_on_403(self) -> None:
        opener = _opener_http_error(403, {"error": "Forbidden"})
        c = _client(opener=opener)
        result = c.list_pending()
        assert result == []
