"""Tests for MoriMemoryProvider (v0.2.0: LWM overlay + governed proposals).

Covers:
  * LWM read-your-writes: on_memory_write then prefetch sees the entry.
  * Action mapping end-to-end (add/replace/remove × memory/user) routes to the
    right LWM + outbox ops.
  * sync_turn is an explicit no-op (no outbox/LWM interaction).
  * prefetch merges LWM + canon, LWM winning on collision; never raises.
  * Reconciliation: promote-on-hash-match, evict-on-reject, canon-wins-on-divergence.
  * is_available() is bool of MORI_API_KEY (no network); keyword-only session_id.
  * get_config_schema declares MORI_SERVER_URL / MORI_API_KEY.
  * Tool schemas are read-only.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from hermes_mori_provider.normalizer import HermesEventNormalizer, content_hash
from hermes_mori_provider.outbox import GovernedWriteOutbox
from hermes_mori_provider.provider import MoriMemoryProvider, _format_search_results
from hermes_mori_provider.rest_client import MoriTransportError

# ── Fakes ─────────────────────────────────────────────────────────────────────


class FakeClient:
    """Fake MoriRestClient with controllable search / canon / pending."""

    def __init__(
        self,
        search_results: list[dict] | None = None,
        canon: dict[str, dict] | None = None,
        pending: list[dict] | None = None,
        fail: bool = False,
    ) -> None:
        self._search_results = search_results or []
        self._canon = canon or {}
        self._pending = pending or []
        self._fail = fail
        self.propose_calls: list[dict] = []

    def search(self, query: str, limit: int = 10) -> list[dict]:
        if self._fail:
            raise MoriTransportError("fake transport error")
        return self._search_results

    def list_pending(self, status: str = "") -> list[dict]:
        if self._fail:
            raise MoriTransportError("fake transport error")
        return self._pending

    def get_memory(self, name: str) -> dict | None:
        if self._fail:
            raise MoriTransportError("fake transport error")
        return self._canon.get(name)

    def propose(self, **kwargs):  # pragma: no cover - drainer path not exercised
        self.propose_calls.append(dict(kwargs))
        return (201, {"status": "created"})


def _provider_with_real_outbox(
    tmp_db: Path,
    client: FakeClient | None = None,
) -> MoriMemoryProvider:
    """Provider wired to a REAL outbox (real SQLite LWM) + injected fake client.

    The drainer never reaches the network in these tests because the fake
    client's propose() returns 201 instantly — but our assertions target LWM +
    coalescing state, not the drainer.
    """
    p = MoriMemoryProvider()
    client = client or FakeClient()
    p._client = client
    p._normalizer = HermesEventNormalizer()
    p._outbox = GovernedWriteOutbox(
        client=client,
        db_path=tmp_db,
        initial_backoff=0.001,
        max_backoff=0.01,
        breaker_cooldown=0.001,
        _sleep=lambda _: None,
    )
    p._session_id = "test-session"
    return p


@pytest.fixture()
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "mori_outbox.db"


# ── LWM read-your-writes ──────────────────────────────────────────────────────


class TestReadYourWrites:
    def test_write_then_prefetch_sees_it(self, tmp_db: Path) -> None:
        # Canon search returns nothing; the only source is the LWM overlay.
        client = FakeClient(search_results=[])
        p = _provider_with_real_outbox(tmp_db, client)
        p.on_memory_write("add", "memory", "remember this fact", {"memory_id": "fact-1"})
        out = p.prefetch("anything")
        p.shutdown()
        assert "remember this fact" in out
        assert "hermes-memory-fact-1" in out

    def test_prefetch_keyword_only_session_id(self, tmp_db: Path) -> None:
        p = _provider_with_real_outbox(tmp_db)
        p.on_memory_write("add", "memory", "kw test", {"memory_id": "kw"})
        # Must be callable with keyword session_id and must NOT accept positional.
        out = p.prefetch("q", session_id="s")
        with pytest.raises(TypeError):
            p.prefetch("q", "positional-session")  # type: ignore[misc]
        p.shutdown()
        assert "kw test" in out


# ── Action mapping end-to-end ─────────────────────────────────────────────────


class TestActionMappingEndToEnd:
    def test_add_memory_creates_pending_lwm(self, tmp_db: Path) -> None:
        p = _provider_with_real_outbox(tmp_db)
        p.on_memory_write("add", "memory", "body", {"memory_id": "m1"})
        row = p._outbox.lwm_get("hermes-memory-m1")
        p.shutdown()
        assert row is not None
        assert row["status"] == "pending"
        assert row["target"] == "memory"

    def test_add_user_creates_user_namespaced_lwm(self, tmp_db: Path) -> None:
        p = _provider_with_real_outbox(tmp_db)
        p.on_memory_write("add", "user", "alice likes tea", {"user_id": "alice"})
        row = p._outbox.lwm_get("hermes-user-alice")
        p.shutdown()
        assert row is not None
        assert row["target"] == "user"

    def test_replace_supersedes_lwm_content(self, tmp_db: Path) -> None:
        p = _provider_with_real_outbox(tmp_db)
        p.on_memory_write("add", "memory", "v1", {"memory_id": "m1"})
        p.on_memory_write("replace", "memory", "v2", {"memory_id": "m1"})
        row = p._outbox.lwm_get("hermes-memory-m1")
        p.shutdown()
        assert row["content"] == "v2"

    def test_remove_with_unsent_clears_lwm(self, tmp_db: Path) -> None:
        """add then remove while the proposal is still unsent -> LWM cleared."""
        gate_client = _GatedClient()
        p = _provider_with_real_outbox(tmp_db, gate_client)
        p.on_memory_write("add", "memory", "v1", {"memory_id": "m1"})
        p.on_memory_write("remove", "memory", "v1", {"memory_id": "m1"})
        row = p._outbox.lwm_get("hermes-memory-m1")
        gate_client.release()
        p.shutdown()
        assert row is None

    def test_remove_user_target(self, tmp_db: Path) -> None:
        gate_client = _GatedClient()
        p = _provider_with_real_outbox(tmp_db, gate_client)
        p.on_memory_write("add", "user", "fact", {"user_id": "bob"})
        p.on_memory_write("remove", "user", "fact", {"user_id": "bob"})
        row = p._outbox.lwm_get("hermes-user-bob")
        gate_client.release()
        p.shutdown()
        assert row is None

    def test_never_raises_on_normalizer_failure(self, tmp_db: Path) -> None:
        p = _provider_with_real_outbox(tmp_db)
        p._normalizer = MagicMock()
        p._normalizer.normalize.side_effect = RuntimeError("boom")
        p.on_memory_write("add", "memory", "x", {})  # must not raise
        p.shutdown()


class _GatedClient(FakeClient):
    """Client whose propose() blocks until released — keeps rows unsent."""

    def __init__(self) -> None:
        super().__init__()
        import threading

        self._gate = threading.Event()

    def propose(self, **kwargs):
        self._gate.wait(timeout=5)
        return (201, {"status": "created"})

    def release(self) -> None:
        self._gate.set()


# ── sync_turn no-op ───────────────────────────────────────────────────────────


class TestSyncTurnNoOp:
    def test_sync_turn_does_nothing(self, tmp_db: Path) -> None:
        p = _provider_with_real_outbox(tmp_db)
        p.sync_turn("user said", "assistant said", session_id="s")
        # No LWM rows, no proposals.
        rows = p._outbox.lwm_all(exclude_rejected=False)
        propose_calls = len(p._client.propose_calls)
        p.shutdown()
        assert rows == []
        assert propose_calls == 0

    def test_sync_turn_returns_none(self, tmp_db: Path) -> None:
        p = _provider_with_real_outbox(tmp_db)
        assert p.sync_turn("u", "a") is None
        p.shutdown()


# ── prefetch merge ────────────────────────────────────────────────────────────


class TestPrefetchMerge:
    def test_lwm_wins_on_name_collision(self, tmp_db: Path) -> None:
        # Canon has an old body for the same name; LWM has the fresh one.
        canon_search = [
            {"name": "hermes-memory-m1", "title": "M1", "description": "", "body": "OLD CANON"}
        ]
        client = FakeClient(search_results=canon_search)
        p = _provider_with_real_outbox(tmp_db, client)
        p.on_memory_write("add", "memory", "FRESH LOCAL", {"memory_id": "m1"})
        out = p.prefetch("m1")
        p.shutdown()
        assert "FRESH LOCAL" in out
        assert "OLD CANON" not in out

    def test_canon_only_results_shown(self, tmp_db: Path) -> None:
        canon_search = [
            {
                "name": "hermes-memory-other",
                "title": "Other",
                "description": "",
                "body": "canon body",
            }
        ]
        client = FakeClient(search_results=canon_search)
        p = _provider_with_real_outbox(tmp_db, client)
        out = p.prefetch("q")
        p.shutdown()
        assert "canon body" in out

    def test_empty_when_nothing(self, tmp_db: Path) -> None:
        p = _provider_with_real_outbox(tmp_db, FakeClient(search_results=[]))
        assert p.prefetch("q") == ""
        p.shutdown()

    def test_never_raises_on_client_failure(self, tmp_db: Path) -> None:
        client = FakeClient(fail=True)
        p = _provider_with_real_outbox(tmp_db, client)
        # Even with the client raising everywhere, prefetch returns "".
        assert p.prefetch("q") == ""
        p.shutdown()

    def test_never_raises_on_unexpected_error(self, tmp_db: Path) -> None:
        p = _provider_with_real_outbox(tmp_db)
        p._outbox = MagicMock()
        p._outbox.lwm_all.side_effect = RuntimeError("kaboom")
        p._client = MagicMock()
        p._client.search.side_effect = RuntimeError("kaboom")
        assert p.prefetch("q") == ""


# ── Reconciliation ────────────────────────────────────────────────────────────


class TestReconciliation:
    def test_promote_on_hash_match(self, tmp_db: Path) -> None:
        body = "the canonical body"
        chash = content_hash(body)
        client = FakeClient(
            search_results=[],
            canon={"hermes-memory-m1": {"name": "hermes-memory-m1", "body": body}},
        )
        p = _provider_with_real_outbox(tmp_db, client)
        # Seed an LWM row whose content hash matches canon.
        p._outbox.lwm_upsert(
            name="hermes-memory-m1",
            target="memory",
            content=body,
            content_hash=chash,
            status="pending",
        )
        p._reconcile()
        row = p._outbox.lwm_get("hermes-memory-m1")
        p.shutdown()
        assert row["status"] == "canon"

    def test_canon_wins_on_divergence(self, tmp_db: Path) -> None:
        """Dreamer edited content before approval -> LWM overwritten with canon."""
        local_body = "what the agent wrote"
        canon_body = "what the dreamer edited it to"
        client = FakeClient(
            canon={"hermes-memory-m1": {"name": "hermes-memory-m1", "body": canon_body}},
        )
        p = _provider_with_real_outbox(tmp_db, client)
        p._outbox.lwm_upsert(
            name="hermes-memory-m1",
            target="memory",
            content=local_body,
            content_hash=content_hash(local_body),
            status="pending",
        )
        p._reconcile()
        row = p._outbox.lwm_get("hermes-memory-m1")
        p.shutdown()
        assert row["content"] == canon_body
        assert row["content_hash"] == content_hash(canon_body)
        assert row["status"] == "canon"

    def test_evict_on_reject(self, tmp_db: Path) -> None:
        client = FakeClient(
            canon={},  # not in canon
            pending=[{"name": "hermes-memory-m1", "status": "rejected"}],
        )
        p = _provider_with_real_outbox(tmp_db, client)
        p._outbox.lwm_upsert(
            name="hermes-memory-m1",
            target="memory",
            content="rejected thing",
            content_hash=content_hash("rejected thing"),
            status="pending",
        )
        p._reconcile()
        row = p._outbox.lwm_get("hermes-memory-m1")
        p.shutdown()
        assert row["status"] == "rejected"

    def test_still_pending_left_alone(self, tmp_db: Path) -> None:
        client = FakeClient(canon={}, pending=[])
        p = _provider_with_real_outbox(tmp_db, client)
        p._outbox.lwm_upsert(
            name="hermes-memory-m1",
            target="memory",
            content="pending thing",
            content_hash=content_hash("pending thing"),
            status="pending",
        )
        p._reconcile()
        row = p._outbox.lwm_get("hermes-memory-m1")
        p.shutdown()
        assert row["status"] == "pending"

    def test_reconcile_safe_never_raises(self, tmp_db: Path) -> None:
        p = _provider_with_real_outbox(tmp_db)
        p._client = MagicMock()
        p._client.get_memory.side_effect = RuntimeError("boom")
        p._outbox.lwm_upsert(
            name="hermes-memory-m1",
            target="memory",
            content="c",
            content_hash="h",
            status="pending",
        )
        p._reconcile_safe()  # must not raise
        p.shutdown()


# ── is_available ──────────────────────────────────────────────────────────────


class TestIsAvailable:
    """is_available() requires both MORI_API_KEY and MORI_INTAKE_URL (ARCH-004).

    Previously it only checked MORI_API_KEY.  Now it also requires
    MORI_INTAKE_URL so callers know when writes cannot drain.
    """

    def test_true_when_both_keys_set(self) -> None:
        p = MoriMemoryProvider()
        with patch.dict(
            os.environ,
            {"MORI_API_KEY": "some-key", "MORI_INTAKE_URL": "http://intake.example.com:8971"},
        ):
            assert p.is_available() is True

    def test_false_when_api_key_absent(self) -> None:
        p = MoriMemoryProvider()
        env = {k: v for k, v in os.environ.items() if k not in ("MORI_API_KEY", "MORI_INTAKE_URL")}
        env["MORI_INTAKE_URL"] = "http://intake.example.com"
        with patch.dict(os.environ, env, clear=True):
            assert p.is_available() is False

    def test_false_when_api_key_empty(self) -> None:
        p = MoriMemoryProvider()
        with patch.dict(
            os.environ,
            {"MORI_API_KEY": "", "MORI_INTAKE_URL": "http://intake.example.com"},
        ):
            assert p.is_available() is False

    def test_false_when_intake_url_absent(self) -> None:
        """ARCH-004: without MORI_INTAKE_URL writes cannot drain — unavailable."""
        p = MoriMemoryProvider()
        env = {k: v for k, v in os.environ.items() if k != "MORI_INTAKE_URL"}
        env["MORI_API_KEY"] = "some-key"
        with patch.dict(os.environ, env, clear=True):
            assert p.is_available() is False

    def test_false_when_intake_url_empty(self) -> None:
        """Empty MORI_INTAKE_URL → unavailable."""
        p = MoriMemoryProvider()
        with patch.dict(
            os.environ,
            {"MORI_API_KEY": "some-key", "MORI_INTAKE_URL": ""},
        ):
            assert p.is_available() is False

    def test_no_network_call(self) -> None:
        p = MoriMemoryProvider()
        with patch("urllib.request.urlopen") as mock_urlopen:
            with patch.dict(
                os.environ,
                {"MORI_API_KEY": "key", "MORI_INTAKE_URL": "http://x"},
            ):
                p.is_available()
        mock_urlopen.assert_not_called()


# ── get_config_schema ─────────────────────────────────────────────────────────


class TestConfigSchema:
    def test_declares_both_env_vars(self) -> None:
        p = MoriMemoryProvider()
        env_vars = {f["env_var"] for f in p.get_config_schema()}
        assert "MORI_SERVER_URL" in env_vars
        assert "MORI_API_KEY" in env_vars

    def test_api_key_is_secret_and_required(self) -> None:
        p = MoriMemoryProvider()
        api = next(f for f in p.get_config_schema() if f["env_var"] == "MORI_API_KEY")
        assert api["secret"] is True
        assert api["required"] is True


# ── Tool schemas (read-only) ──────────────────────────────────────────────────


class TestToolSchemas:
    def test_all_read_only(self) -> None:
        p = MoriMemoryProvider()
        for t in p.get_tool_schemas():
            for forbidden in ("approve", "reject", "delete", "write"):
                assert forbidden not in t["name"]

    def test_expected_tools_present(self) -> None:
        p = MoriMemoryProvider()
        names = {t["name"] for t in p.get_tool_schemas()}
        assert {"mori_search", "mori_list_pending", "mori_proposal_status"} <= names

    def test_each_schema_well_formed(self) -> None:
        p = MoriMemoryProvider()
        for schema in p.get_tool_schemas():
            assert {"name", "description", "parameters"} <= set(schema)


# ── system_prompt_block ───────────────────────────────────────────────────────


class TestSystemPromptBlock:
    def test_mentions_proposals(self) -> None:
        p = MoriMemoryProvider()
        assert "proposal" in p.system_prompt_block().lower()


# ── server_url env fallback (regression) ──────────────────────────────────────


def test_server_url_falls_back_to_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MORI_SERVER_URL", "http://mori.example:8968")
    monkeypatch.setenv("MORI_API_KEY", "deadbeefcafe")
    with (
        patch("hermes_mori_provider.rest_client.MoriRestClient") as MockClient,
        patch("hermes_mori_provider.outbox.GovernedWriteOutbox"),
    ):
        MoriMemoryProvider().initialize(session_id="t", hermes_home=tmp_path)
    assert MockClient.call_args.kwargs["base_url"] == "http://mori.example:8968"


# ── _format_search_results ────────────────────────────────────────────────────


class TestFormatSearchResults:
    def test_empty(self) -> None:
        assert _format_search_results([]) == ""

    def test_includes_title_and_name(self) -> None:
        mems = [{"name": "hermes-memory-foo", "title": "Foo", "description": "", "body": "abc"}]
        out = _format_search_results(mems)
        assert "Foo" in out
        assert "hermes-memory-foo" in out

    def test_truncates_long_body(self) -> None:
        mems = [{"name": "n", "title": "T", "description": "", "body": "x" * 1000}]
        assert len(_format_search_results(mems)) < 800
