"""Tests for MoriMemoryProvider.

Verifies:
  * prefetch() formats results as a context block and returns "" on failure.
  * prefetch() never raises into the agent.
  * on_memory_write() is non-blocking for durable events → routes to outbox.
  * on_memory_write() for ephemeral events → dropped (outbox not called).
  * get_tool_schemas() returns only read-only tools (no approve/reject/delete).
  * is_available() returns bool of MORI_API_KEY env var (no network).
  * handle_tool_call() routes correctly.
  * system_prompt_block() returns a non-empty string.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from hermes_mori_provider.provider import MoriMemoryProvider, _format_search_results
from hermes_mori_provider.rest_client import MoriTransportError

# ── Fake dependencies ─────────────────────────────────────────────────────────


class FakeClient:
    def __init__(self, search_results: list[dict] | None = None, fail: bool = False) -> None:
        self._search_results = search_results or []
        self._fail = fail
        self.search_calls: list[dict] = []
        self.pending_calls: list[dict] = []

    def search(self, query: str, limit: int = 10) -> list[dict]:
        self.search_calls.append({"query": query, "limit": limit})
        if self._fail:
            raise MoriTransportError("fake transport error")
        return self._search_results

    def list_pending(self, status: str = "") -> list[dict]:
        self.pending_calls.append({"status": status})
        if self._fail:
            raise MoriTransportError("fake transport error")
        return []


class FakeOutbox:
    def __init__(self) -> None:
        self.enqueued: list[dict] = []
        self.flushed: bool = False

    def enqueue(self, payload: dict) -> bool:
        self.enqueued.append(payload)
        return True

    def flush(self, timeout: float = 10.0) -> bool:
        self.flushed = True
        return True

    def shutdown(self) -> None:
        pass


class FakeNormalizer:
    def __init__(self, result: dict | None = "PASS") -> None:
        # "PASS" is a sentinel meaning "return a default payload".
        self._result = result
        self.calls: list[dict] = []

    def normalize(self, action: str, target: str, content: str) -> dict | None:
        self.calls.append({"action": action, "target": target, "content": content})
        if self._result == "PASS":
            return {
                "name": "hermes.test",
                "title": "test",
                "description": "",
                "type": "project",
                "body": content,
                "tags": ["source:hermes"],
                "idempotency_key": "abc",
            }
        return self._result  # None → ephemeral drop


# ── Provider factory ─────────────────────────────────────────────────────────


def _make_provider(
    client: FakeClient | None = None,
    outbox: FakeOutbox | None = None,
    normalizer: FakeNormalizer | None = None,
) -> MoriMemoryProvider:
    """Create a provider with injected fakes, bypassing initialize()."""
    p = MoriMemoryProvider()
    p._client = client or FakeClient()
    p._outbox = outbox or FakeOutbox()
    p._normalizer = normalizer or FakeNormalizer()
    p._session_id = "test-session"
    return p


# ── prefetch tests ────────────────────────────────────────────────────────────


class TestPrefetch:
    def test_returns_formatted_string_with_results(self) -> None:
        mems = [
            {"name": "hermes.a", "title": "Alpha", "description": "An alpha", "body": "body a"},
            {"name": "hermes.b", "title": "Beta", "description": "", "body": "body b"},
        ]
        client = FakeClient(search_results=mems)
        p = _make_provider(client=client)
        result = p.prefetch("test query")
        assert "Alpha" in result
        assert "hermes.a" in result
        assert "body a" in result

    def test_returns_empty_string_on_empty_results(self) -> None:
        client = FakeClient(search_results=[])
        p = _make_provider(client=client)
        result = p.prefetch("query with no results")
        assert result == ""

    def test_returns_empty_string_on_client_failure(self) -> None:
        client = FakeClient(fail=True)
        p = _make_provider(client=client)
        result = p.prefetch("any query")
        assert result == ""

    def test_never_raises_on_failure(self) -> None:
        """Even an unexpected exception must not propagate."""
        client = MagicMock()
        client.search.side_effect = RuntimeError("unexpected!")
        p = _make_provider(client=client)
        # Must not raise.
        result = p.prefetch("dangerous query")
        assert result == ""

    def test_passes_query_to_client(self) -> None:
        client = FakeClient(search_results=[])
        p = _make_provider(client=client)
        p.prefetch("my specific query")
        assert client.search_calls[0]["query"] == "my specific query"


# ── on_memory_write tests ─────────────────────────────────────────────────────


class TestOnMemoryWrite:
    def test_durable_event_routes_to_outbox(self) -> None:
        outbox = FakeOutbox()
        normalizer = FakeNormalizer(result="PASS")  # returns a payload
        p = _make_provider(outbox=outbox, normalizer=normalizer)
        p.on_memory_write("add", "MEMORY.md", "---\nmemory_id: x\ndurability: durable\n---\nBody")
        assert len(outbox.enqueued) == 1

    def test_ephemeral_event_not_routed_to_outbox(self) -> None:
        outbox = FakeOutbox()
        normalizer = FakeNormalizer(result=None)  # normalizer drops it
        p = _make_provider(outbox=outbox, normalizer=normalizer)
        p.on_memory_write("add", "MEMORY.md", "---\ndurability: ephemeral\n---\nNope")
        assert len(outbox.enqueued) == 0

    def test_is_non_blocking(self) -> None:
        """on_memory_write must return well under 50 ms — SQLite insert only."""
        outbox = FakeOutbox()
        p = _make_provider(outbox=outbox)
        start = time.monotonic()
        p.on_memory_write("add", "MEMORY.md", "---\nmemory_id: t\ndurability: durable\n---\nB")
        elapsed = time.monotonic() - start
        assert elapsed < 0.05, f"on_memory_write took {elapsed:.3f}s — must be non-blocking"

    def test_never_raises_on_normalizer_failure(self) -> None:
        normalizer = MagicMock()
        normalizer.normalize.side_effect = RuntimeError("normalizer bug")
        p = _make_provider(normalizer=normalizer)
        # Must not raise.
        p.on_memory_write("add", "MEMORY.md", "content")

    def test_never_raises_on_outbox_failure(self) -> None:
        outbox = MagicMock()
        outbox.enqueue.side_effect = RuntimeError("outbox bug")
        normalizer = FakeNormalizer(result="PASS")
        p = _make_provider(outbox=outbox, normalizer=normalizer)
        # Must not raise.
        p.on_memory_write("add", "MEMORY.md", "content")

    def test_normalizer_receives_correct_args(self) -> None:
        normalizer = FakeNormalizer(result="PASS")
        p = _make_provider(normalizer=normalizer)
        p.on_memory_write("replace", "USER.md", "some content")
        call = normalizer.calls[0]
        assert call["action"] == "replace"
        assert call["target"] == "USER.md"
        assert call["content"] == "some content"


# ── get_tool_schemas tests ────────────────────────────────────────────────────


class TestToolSchemas:
    def test_returns_list_of_dicts(self) -> None:
        p = _make_provider()
        schemas = p.get_tool_schemas()
        assert isinstance(schemas, list)
        assert len(schemas) > 0

    def test_all_tools_are_read_only(self) -> None:
        """No tool should allow approving, rejecting, or deleting memories."""
        p = _make_provider()
        tool_names = [t["name"] for t in p.get_tool_schemas()]
        for name in tool_names:
            assert "approve" not in name
            assert "reject" not in name
            assert "delete" not in name
            assert "write" not in name

    def test_mori_search_present(self) -> None:
        p = _make_provider()
        names = [t["name"] for t in p.get_tool_schemas()]
        assert "mori_search" in names

    def test_mori_list_pending_present(self) -> None:
        p = _make_provider()
        names = [t["name"] for t in p.get_tool_schemas()]
        assert "mori_list_pending" in names

    def test_mori_proposal_status_present(self) -> None:
        p = _make_provider()
        names = [t["name"] for t in p.get_tool_schemas()]
        assert "mori_proposal_status" in names

    def test_each_schema_has_required_keys(self) -> None:
        p = _make_provider()
        for schema in p.get_tool_schemas():
            assert "name" in schema
            assert "description" in schema
            assert "parameters" in schema


# ── is_available tests ────────────────────────────────────────────────────────


class TestIsAvailable:
    def test_true_when_key_set(self) -> None:
        p = MoriMemoryProvider()
        with patch.dict(os.environ, {"MORI_API_KEY": "some-key"}):
            assert p.is_available() is True

    def test_false_when_key_absent(self) -> None:
        p = MoriMemoryProvider()
        env = {k: v for k, v in os.environ.items() if k != "MORI_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            assert p.is_available() is False

    def test_false_when_key_empty_string(self) -> None:
        p = MoriMemoryProvider()
        with patch.dict(os.environ, {"MORI_API_KEY": ""}):
            assert p.is_available() is False

    def test_no_network_call(self) -> None:
        """is_available must not open any connections."""
        p = MoriMemoryProvider()
        with patch("urllib.request.urlopen") as mock_urlopen:
            with patch.dict(os.environ, {"MORI_API_KEY": "key"}):
                p.is_available()
        mock_urlopen.assert_not_called()


# ── system_prompt_block tests ─────────────────────────────────────────────────


class TestSystemPromptBlock:
    def test_returns_non_empty_string(self) -> None:
        p = _make_provider()
        block = p.system_prompt_block()
        assert isinstance(block, str)
        assert len(block) > 0

    def test_mentions_proposals(self) -> None:
        p = _make_provider()
        block = p.system_prompt_block()
        assert "proposal" in block.lower()


# ── handle_tool_call tests ────────────────────────────────────────────────────


class TestHandleToolCall:
    def test_mori_search_routes_to_client(self) -> None:
        mems = [{"name": "hermes.x", "title": "X", "body": "body", "description": ""}]
        client = FakeClient(search_results=mems)
        p = _make_provider(client=client)
        result = p.handle_tool_call("mori_search", {"query": "find x"})
        assert result["count"] == 1
        assert len(client.search_calls) == 1

    def test_mori_list_pending_routes_to_client(self) -> None:
        client = FakeClient()
        p = _make_provider(client=client)
        result = p.handle_tool_call("mori_list_pending", {"status": "pending"})
        assert "items" in result
        assert len(client.pending_calls) == 1
        assert client.pending_calls[0]["status"] == "pending"

    def test_unknown_tool_returns_error_dict(self) -> None:
        p = _make_provider()
        result = p.handle_tool_call("mori_nuke_everything", {})
        assert "error" in result

    def test_mori_search_returns_empty_on_client_failure(self) -> None:
        client = FakeClient(fail=True)
        p = _make_provider(client=client)
        result = p.handle_tool_call("mori_search", {"query": "anything"})
        assert "error" in result
        assert result.get("count", 0) == 0


# ── _format_search_results unit tests ────────────────────────────────────────


class TestFormatSearchResults:
    def test_empty_returns_empty_string(self) -> None:
        assert _format_search_results([]) == ""

    def test_includes_title_and_name(self) -> None:
        mems = [{"name": "hermes.foo", "title": "Foo Learning", "description": "", "body": "abc"}]
        result = _format_search_results(mems)
        assert "Foo Learning" in result
        assert "hermes.foo" in result

    def test_includes_description_when_present(self) -> None:
        mems = [{"name": "n", "title": "T", "description": "some desc", "body": ""}]
        result = _format_search_results(mems)
        assert "some desc" in result

    def test_truncates_long_body(self) -> None:
        long_body = "x" * 1000
        mems = [{"name": "n", "title": "T", "description": "", "body": long_body}]
        result = _format_search_results(mems)
        # Result should be much shorter than the full body.
        assert len(result) < 800
