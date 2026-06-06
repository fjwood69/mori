"""Tests for the hermes-mori-provider intake repoint (v0.3.0).

Covers:
  * ``MoriRestClient.submit_intake`` — payload shape, header, 202/422/429/5xx.
  * ``HermesEventNormalizer.derive_intake_stable_key`` — eligible-namespace mapping.
  * Normalizer ``normalize()`` exposes ``intake_stable_key`` and ``action``.
  * op → action mapping (propose→add, supersede→replace, retract→remove).
  * ``GovernedWriteOutbox`` drain via intake path:
      - 202 accepted (including duplicate flag).
      - 422 eligibility rejection → dead-letter, never retry.
      - 429 rate-limited → retry with back-off.
      - 5xx / transport error → retry.
  * ``MoriMemoryProvider.initialize`` reads ``MORI_INTAKE_URL``; unset → ERROR log.
  * Reads (prefetch / search / reconcile) still go to the mori server — intake
    client is NOT used for reads.

All tests are deterministic — no real network calls, no real sleeps.
The ``_opener`` seam and fake clients from the existing test suite are reused.
"""

from __future__ import annotations

import io
import json
import logging
import sys
import time as _time
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from hermes_mori_provider.normalizer import HermesEventNormalizer
from hermes_mori_provider.outbox import GovernedWriteOutbox
from hermes_mori_provider.provider import MoriMemoryProvider
from hermes_mori_provider.rest_client import MoriRestClient, MoriTransportError

# ── Fake opener helpers (mirrors test_rest_client.py) ─────────────────────────


def _make_response(status: int, body: dict) -> Any:
    body_bytes = json.dumps(body).encode()
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = body_bytes
    resp.__enter__ = lambda self: self
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _opener_returning(status: int, body: dict) -> Any:
    resp = _make_response(status, body)

    def opener(req: Any, timeout: int = 15) -> Any:
        return resp

    return opener, resp


def _opener_http_error(status: int, body: dict) -> Any:
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
    def opener(req: Any, timeout: int = 15) -> Any:
        raise OSError("connection refused")

    return opener


def _client(opener: Any = None, base_url: str = "http://localhost:8971") -> MoriRestClient:
    return MoriRestClient(base_url=base_url, api_key="test-key", timeout=5, _opener=opener)


# ── Fake outbox clients ───────────────────────────────────────────────────────


class FakeIntakeClient:
    """Fake intake-side client with controllable submit_intake responses."""

    def __init__(self, responses: list[tuple[int, dict]] | None = None) -> None:
        self._responses = list(
            responses
            or [(202, {"status": "accepted", "submission_id": "uuid-1", "duplicate": False})]
        )
        self._calls: list[dict[str, Any]] = []

    def submit_intake(self, **kwargs: Any) -> tuple[int, dict]:
        self._calls.append(dict(kwargs))
        resp = self._responses[0] if len(self._responses) == 1 else self._responses.pop(0)
        return resp

    @property
    def call_count(self) -> int:
        return len(self._calls)

    @property
    def calls(self) -> list[dict[str, Any]]:
        return list(self._calls)


class FakeReadClient:
    """Fake read-side client — has propose() so the outbox can fall back if needed."""

    def __init__(self) -> None:
        self.propose_calls: list[dict] = []

    def propose(self, **kwargs: Any) -> tuple[int, dict]:  # pragma: no cover
        self.propose_calls.append(dict(kwargs))
        return (201, {"status": "created"})

    def search(self, *args: Any, **kwargs: Any) -> list[dict]:
        return []

    def list_pending(self, *args: Any, **kwargs: Any) -> list[dict]:
        return []

    def get_memory(self, *args: Any, **kwargs: Any) -> dict | None:
        return None


def _make_outbox_with_intake(
    tmp_db: Path,
    intake_responses: list[tuple[int, dict]] | None = None,
    intake_client: Any = None,
    read_client: Any = None,
    autostart_drain: bool = True,
) -> tuple[GovernedWriteOutbox, FakeIntakeClient, FakeReadClient]:
    ic = intake_client or FakeIntakeClient(intake_responses)
    rc = read_client or FakeReadClient()
    outbox = GovernedWriteOutbox(
        client=rc,
        db_path=tmp_db,
        intake_client=ic,
        intake_agent_id="hermes",
        intake_session_id="test-session",
        initial_backoff=0.001,
        max_backoff=0.01,
        breaker_threshold=5,
        breaker_cooldown=0.001,
        autostart_drain=autostart_drain,
        _sleep=lambda _: None,
    )
    return outbox, ic, rc


def _wait_drain(outbox: GovernedWriteOutbox, timeout: float = 5.0) -> bool:
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if outbox.pending_count() == 0:
            return True
        _time.sleep(0.02)
    return False


def _payload(
    name: str = "hermes-memory-test",
    body: str = "The deployment uses Podman rootless containers.",
    op: str = "propose",
    action: str = "add",
    target: str = "memory",
    intake_stable_key: str = "learned-test",
    session_id: str = "test-session",
) -> dict[str, Any]:
    return {
        "op": op,
        "action": action,
        "name": name,
        "intake_stable_key": intake_stable_key,
        "target": target,
        "session_id": session_id,
        "title": name,
        "description": "",
        "type": "project",
        "body": body,
        "tags": ["source:hermes"],
        "idempotency_key": "abc123",
    }


@pytest.fixture()
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "outbox.db"


# ── 1. MoriRestClient.submit_intake ───────────────────────────────────────────


class TestSubmitIntake:
    """submit_intake() sends the right payload and handles all status codes."""

    def test_x_api_key_header_sent(self) -> None:
        captured: list[Any] = []

        def opener(req: Any, timeout: int = 15) -> Any:
            captured.append(req)
            return _make_response(
                202, {"status": "accepted", "submission_id": "u1", "duplicate": False}
            )

        c = _client(opener=opener)
        c.submit_intake(
            session_id="s1",
            agent_id="hermes",
            target="memory",
            action="add",
            stable_key="learned-test",
            content="Some durable content here.",
        )
        assert captured[0].get_header("X-api-key") == "test-key"

    def test_posts_to_intake_submissions_path(self) -> None:
        captured: list[Any] = []

        def opener(req: Any, timeout: int = 15) -> Any:
            captured.append(req)
            return _make_response(
                202, {"status": "accepted", "submission_id": "u1", "duplicate": False}
            )

        c = _client(opener=opener, base_url="http://intake-host:8971")
        c.submit_intake(
            session_id="s1",
            agent_id="hermes",
            target="memory",
            action="add",
            stable_key="learned-deploy",
            content="Deploy content.",
        )
        assert "/intake/submissions" in captured[0].full_url

    def test_payload_fields_correct(self) -> None:
        captured: list[Any] = []

        def opener(req: Any, timeout: int = 15) -> Any:
            captured.append(req)
            return _make_response(
                202, {"status": "accepted", "submission_id": "u1", "duplicate": False}
            )

        c = _client(opener=opener)
        c.submit_intake(
            session_id="my-session",
            agent_id="hermes",
            target="memory",
            action="add",
            stable_key="learned-config-key",
            content="The system uses Prometheus for monitoring.",
            provenance={"mori_name": "hermes-memory-x", "op": "propose"},
        )
        req = captured[0]
        body = json.loads(req.data.decode())
        assert body["session_id"] == "my-session"
        assert body["agent_id"] == "hermes"
        assert body["target"] == "memory"
        assert body["action"] == "add"
        assert body["stable_key"] == "learned-config-key"
        assert body["content"] == "The system uses Prometheus for monitoring."
        assert body["provenance"]["mori_name"] == "hermes-memory-x"

    def test_provenance_omitted_when_none(self) -> None:
        captured: list[Any] = []

        def opener(req: Any, timeout: int = 15) -> Any:
            captured.append(req)
            return _make_response(
                202, {"status": "accepted", "submission_id": "u1", "duplicate": False}
            )

        c = _client(opener=opener)
        c.submit_intake(
            session_id="s",
            agent_id="hermes",
            target="memory",
            action="add",
            stable_key="learned-x",
            content="Content here.",
        )
        body = json.loads(captured[0].data.decode())
        assert "provenance" not in body

    def test_202_returned(self) -> None:
        opener, _ = _opener_returning(
            202, {"status": "accepted", "submission_id": "u1", "duplicate": False}
        )
        c = _client(opener=opener)
        status, body = c.submit_intake(
            session_id="s",
            agent_id="a",
            target="memory",
            action="add",
            stable_key="learned-x",
            content="Content.",
        )
        assert status == 202
        assert body["status"] == "accepted"

    def test_422_returned_not_raised(self) -> None:
        opener = _opener_http_error(
            422, {"status": "rejected", "reason": "namespace-not-allowlisted"}
        )
        c = _client(opener=opener)
        status, body = c.submit_intake(
            session_id="s",
            agent_id="a",
            target="memory",
            action="add",
            stable_key="scratch-x",
            content="Content.",
        )
        assert status == 422
        assert body["reason"] == "namespace-not-allowlisted"

    def test_429_returned_not_raised(self) -> None:
        opener = _opener_http_error(429, {"status": "rate_limited", "retry_after": 30})
        c = _client(opener=opener)
        status, body = c.submit_intake(
            session_id="s",
            agent_id="a",
            target="memory",
            action="add",
            stable_key="learned-x",
            content="Content.",
        )
        assert status == 429

    def test_500_raises_transport_error(self) -> None:
        opener = _opener_http_error(500, {"error": "server error"})
        c = _client(opener=opener)
        with pytest.raises(MoriTransportError) as exc_info:
            c.submit_intake(
                session_id="s",
                agent_id="a",
                target="memory",
                action="add",
                stable_key="learned-x",
                content="Content.",
            )
        assert exc_info.value.status_code == 500

    def test_connection_error_raises_transport_error(self) -> None:
        c = _client(opener=_opener_connection_error())
        with pytest.raises(MoriTransportError):
            c.submit_intake(
                session_id="s",
                agent_id="a",
                target="memory",
                action="add",
                stable_key="learned-x",
                content="Content.",
            )


# ── 2. Eligible-namespace stable_key mapping ──────────────────────────────────


class TestIntakeStableKeyMapping:
    """HermesEventNormalizer.derive_intake_stable_key produces eligible prefixes."""

    @pytest.fixture()
    def norm(self) -> HermesEventNormalizer:
        return HermesEventNormalizer()

    def test_memory_with_memory_id_gives_learned_prefix(self, norm: HermesEventNormalizer) -> None:
        key = norm.derive_intake_stable_key("memory", "body", {"memory_id": "my-fact"})
        assert key.startswith("learned-")
        assert "my-fact" in key

    def test_memory_without_memory_id_gives_learned_slug_hash(
        self, norm: HermesEventNormalizer
    ) -> None:
        content = "The staging cluster uses Podman rootless containers."
        key = norm.derive_intake_stable_key("memory", content, {})
        assert key.startswith("learned-")
        assert len(key) > len("learned-")

    def test_memory_intake_key_is_deterministic(self, norm: HermesEventNormalizer) -> None:
        content = "Same content always yields the same intake key."
        a = norm.derive_intake_stable_key("memory", content, {})
        b = norm.derive_intake_stable_key("memory", content, {})
        assert a == b

    def test_memory_different_content_gives_different_keys(
        self, norm: HermesEventNormalizer
    ) -> None:
        a = norm.derive_intake_stable_key("memory", "first body text here", {})
        b = norm.derive_intake_stable_key("memory", "second body text here", {})
        assert a != b

    def test_user_with_user_id_gives_preference_prefix(self, norm: HermesEventNormalizer) -> None:
        key = norm.derive_intake_stable_key("user", "body", {"user_id": "alice"})
        assert key == "preference-alice"

    def test_user_default_user_id_gives_preference_default(
        self, norm: HermesEventNormalizer
    ) -> None:
        key = norm.derive_intake_stable_key("user", "body", {})
        assert key == "preference-default"

    def test_memory_intake_key_independent_from_mori_name(
        self, norm: HermesEventNormalizer
    ) -> None:
        """The intake stable_key and the mori name are different strings."""
        content = "Deployment uses Kubernetes on GKE."
        mori_name = norm.derive_name("memory", content, {"memory_id": "k8s-deploy"})
        intake_key = norm.derive_intake_stable_key("memory", content, {"memory_id": "k8s-deploy"})
        assert mori_name != intake_key
        assert mori_name.startswith("hermes-memory-")
        assert intake_key.startswith("learned-")


# ── 3. normalize() exposes intake_stable_key + action ─────────────────────────


class TestNormalizeIntakeFields:
    @pytest.fixture()
    def norm(self) -> HermesEventNormalizer:
        return HermesEventNormalizer()

    def test_action_carried_through(self, norm: HermesEventNormalizer) -> None:
        for action in ("add", "replace", "remove"):
            desc = norm.normalize(action, "memory", "content", {"memory_id": "m"})
            assert desc["action"] == action

    def test_intake_stable_key_present(self, norm: HermesEventNormalizer) -> None:
        desc = norm.normalize("add", "memory", "content", {"memory_id": "m"})
        assert "intake_stable_key" in desc

    def test_intake_stable_key_memory_learned_prefix(self, norm: HermesEventNormalizer) -> None:
        desc = norm.normalize("add", "memory", "content", {"memory_id": "m"})
        assert desc["intake_stable_key"].startswith("learned-")

    def test_intake_stable_key_user_preference_prefix(self, norm: HermesEventNormalizer) -> None:
        desc = norm.normalize("add", "user", "content", {"user_id": "alice"})
        assert desc["intake_stable_key"].startswith("preference-")

    def test_op_action_map(self, norm: HermesEventNormalizer) -> None:
        """op→action is the inverse of action→op."""
        cases = [
            ("add", "propose"),
            ("replace", "supersede"),
            ("remove", "retract"),
        ]
        for action, expected_op in cases:
            desc = norm.normalize(action, "memory", "body", {"memory_id": "m"})
            assert desc["op"] == expected_op
            assert desc["action"] == action


# ── 4. Outbox drain — intake response handling ────────────────────────────────


class TestOutboxIntakeDrain:
    """Drain loop calls submit_intake and handles all response codes correctly."""

    def test_202_marks_done_and_increments_sent(self, tmp_db: Path) -> None:
        outbox, ic, _ = _make_outbox_with_intake(tmp_db)
        outbox.enqueue(_payload())
        drained = _wait_drain(outbox)
        snap = outbox.metrics_snapshot()
        outbox.shutdown()
        assert drained
        assert ic.call_count >= 1
        assert snap["proposals_sent"] >= 1

    def test_202_duplicate_flag_logged_and_still_marked_done(self, tmp_db: Path) -> None:
        ic = FakeIntakeClient(
            [(202, {"status": "accepted", "submission_id": "u1", "duplicate": True})]
        )
        outbox, _, _ = _make_outbox_with_intake(tmp_db, intake_client=ic)
        outbox.enqueue(_payload())
        drained = _wait_drain(outbox)
        snap = outbox.metrics_snapshot()
        outbox.shutdown()
        assert drained
        assert snap["proposals_sent"] >= 1

    def test_422_dead_letters_immediately_no_retry(self, tmp_db: Path) -> None:
        """422 eligibility rejection → dead-letter, drainer moves on without retry."""
        ic = FakeIntakeClient(
            [(422, {"status": "rejected", "reason": "namespace-not-allowlisted"})]
        )
        outbox, _, _ = _make_outbox_with_intake(tmp_db, intake_client=ic)
        outbox.enqueue(_payload())
        _wait_drain(outbox)
        # Capture metrics and pending count BEFORE shutdown (DB closes on shutdown).
        pending = outbox.pending_count()
        snap = outbox.metrics_snapshot()
        outbox.shutdown()
        # Row is gone from pending (dead-lettered).
        assert pending == 0
        # Exactly one call — never retried.
        assert ic.call_count == 1
        assert snap["proposals_failed"] >= 1

    def test_429_retries(self, tmp_db: Path) -> None:
        """429 rate-limited → retry; eventually succeeds."""
        ic = FakeIntakeClient(
            [
                (429, {"status": "rate_limited", "retry_after": 1}),
                (202, {"status": "accepted", "submission_id": "u1", "duplicate": False}),
            ]
        )
        outbox, _, _ = _make_outbox_with_intake(tmp_db, intake_client=ic)
        outbox.enqueue(_payload())
        drained = _wait_drain(outbox)
        outbox.shutdown()
        assert drained
        assert ic.call_count == 2

    def test_5xx_transport_error_retries(self, tmp_db: Path) -> None:
        """5xx raises MoriTransportError → retry, eventually succeeds."""

        class FailThenSucceedClient(FakeIntakeClient):
            def __init__(self) -> None:
                super().__init__()
                self._n = 0

            def submit_intake(self, **kwargs: Any) -> tuple[int, dict]:
                self._n += 1
                self._calls.append(dict(kwargs))
                if self._n < 3:
                    raise MoriTransportError("server error", status_code=500)
                return (202, {"status": "accepted", "submission_id": "u1", "duplicate": False})

        ic = FailThenSucceedClient()
        outbox, _, _ = _make_outbox_with_intake(tmp_db, intake_client=ic)
        outbox.enqueue(_payload())
        drained = _wait_drain(outbox)
        outbox.shutdown()
        assert drained
        assert ic.call_count >= 3

    def test_submit_intake_payload_fields(self, tmp_db: Path) -> None:
        """The intake payload sent by the drain matches the spec fields."""
        outbox, ic, _ = _make_outbox_with_intake(tmp_db)
        outbox.enqueue(
            _payload(
                name="hermes-memory-fact-1",
                body="The system runs Podman rootless on Ubuntu 24.04.",
                action="add",
                target="memory",
                intake_stable_key="learned-fact-1",
                session_id="session-abc",
            )
        )
        drained = _wait_drain(outbox)
        outbox.shutdown()
        assert drained
        assert ic.call_count >= 1
        sent = ic.calls[0]
        assert sent["session_id"] == "session-abc"
        assert sent["agent_id"] == "hermes"
        assert sent["target"] == "memory"
        assert sent["action"] == "add"
        assert sent["stable_key"] == "learned-fact-1"
        assert "Podman rootless" in sent["content"]
        prov = sent.get("provenance", {})
        assert prov.get("mori_name") == "hermes-memory-fact-1"
        assert prov.get("plugin_version") == "0.3.0"

    def test_eligible_intake_stable_key_not_scratch(self, tmp_db: Path) -> None:
        """The intake_stable_key stored in the outbox starts with 'learned-' for memory."""
        outbox, ic, _ = _make_outbox_with_intake(tmp_db)
        outbox.enqueue(
            _payload(
                intake_stable_key="learned-k8s-deploy",
            )
        )
        drained = _wait_drain(outbox)
        outbox.shutdown()
        assert drained
        assert ic.calls[0]["stable_key"] == "learned-k8s-deploy"

    def test_fail_closed_when_no_intake_client(self, tmp_db: Path, caplog) -> None:
        """When intake_client is None the drain FAILS CLOSED.

        Rows MUST remain queued (not marked done/failed), submit_intake MUST
        NOT be called, the legacy propose() MUST NOT be called, and an ERROR
        MUST be logged naming MORI_INTAKE_URL.
        """
        import logging

        rc = FakeReadClient()
        with caplog.at_level(logging.ERROR, logger="hermes_mori_provider.outbox"):
            outbox = GovernedWriteOutbox(
                client=rc,
                db_path=tmp_db,
                intake_client=None,
                initial_backoff=0.001,
                max_backoff=0.01,
                _sleep=lambda _: None,
            )
            outbox.enqueue(_payload())
            # Give the drain loop a moment to attempt processing.
            _time.sleep(0.15)
            pending = outbox.pending_count()
            outbox.shutdown()

        # Row must remain queued — not drained, not dead-lettered.
        assert pending >= 1, "row must remain queued when no intake client is configured"
        # The legacy propose() path must NEVER be called.
        assert len(rc.propose_calls) == 0, "propose() must not be called (ungoverned path closed)"
        # An ERROR must have been logged.
        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert error_records, "an ERROR must be logged when intake_client is None"
        assert any("MORI_INTAKE_URL" in r.message for r in error_records), (
            "ERROR must mention MORI_INTAKE_URL"
        )


# ── 5. Provider initialize — MORI_INTAKE_URL config ──────────────────────────


class TestProviderIntakeConfig:
    def test_intake_url_set_creates_intake_client(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("MORI_INTAKE_URL", "http://intake.example:8971")
        monkeypatch.setenv("MORI_API_KEY", "deadbeefcafe")
        monkeypatch.setenv("MORI_SERVER_URL", "http://mori.example:8968")

        with (
            patch("hermes_mori_provider.rest_client.MoriRestClient") as MockClient,
            patch("hermes_mori_provider.outbox.GovernedWriteOutbox") as MockOutbox,
        ):
            MockOutbox.return_value = MagicMock()
            MoriMemoryProvider().initialize(session_id="t", hermes_home=tmp_path)

        # Two clients should have been created: one for reads (mori server),
        # one for the intake service.
        assert MockClient.call_count == 2
        urls = {call.kwargs["base_url"] for call in MockClient.call_args_list}
        assert "http://mori.example:8968" in urls
        assert "http://intake.example:8971" in urls

    def test_intake_url_unset_logs_error(self, tmp_path: Path, monkeypatch, caplog) -> None:
        monkeypatch.setenv("MORI_API_KEY", "deadbeefcafe")
        monkeypatch.delenv("MORI_INTAKE_URL", raising=False)

        with (
            patch("hermes_mori_provider.rest_client.MoriRestClient"),
            patch("hermes_mori_provider.outbox.GovernedWriteOutbox") as MockOutbox,
            caplog.at_level(logging.ERROR, logger="hermes_mori_provider.provider"),
        ):
            MockOutbox.return_value = MagicMock()
            MoriMemoryProvider().initialize(session_id="t", hermes_home=tmp_path)

        assert any("MORI_INTAKE_URL" in r.message for r in caplog.records)
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    def test_intake_url_unset_does_not_crash(self, tmp_path: Path, monkeypatch) -> None:
        """A missing MORI_INTAKE_URL must not prevent the provider from initialising."""
        monkeypatch.setenv("MORI_API_KEY", "deadbeefcafe")
        monkeypatch.delenv("MORI_INTAKE_URL", raising=False)

        with (
            patch("hermes_mori_provider.rest_client.MoriRestClient"),
            patch("hermes_mori_provider.outbox.GovernedWriteOutbox") as MockOutbox,
        ):
            MockOutbox.return_value = MagicMock()
            p = MoriMemoryProvider()
            p.initialize(session_id="t", hermes_home=tmp_path)  # must not raise

    def test_reads_still_use_mori_server_not_intake(self, tmp_path: Path, monkeypatch) -> None:
        """prefetch / reconcile ALWAYS go to the mori server (MORI_SERVER_URL)."""
        monkeypatch.setenv("MORI_INTAKE_URL", "http://intake.example:8971")
        monkeypatch.setenv("MORI_SERVER_URL", "http://mori.example:8968")
        monkeypatch.setenv("MORI_API_KEY", "deadbeefcafe")

        p = MoriMemoryProvider()
        fake_read = FakeReadClient()

        with patch("hermes_mori_provider.rest_client.MoriRestClient", return_value=fake_read):
            p.initialize(session_id="t", hermes_home=tmp_path)

        # prefetch triggers a search call on _client, not on the intake client.
        result = p.prefetch("anything")
        p.shutdown()
        # FakeReadClient.search returns [] — so prefetch returns "" (no results).
        assert result == ""

    def test_intake_agent_id_configurable(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("MORI_INTAKE_URL", "http://intake.example:8971")
        monkeypatch.setenv("MORI_API_KEY", "deadbeefcafe")
        monkeypatch.setenv("MORI_INTAKE_AGENT_ID", "myagent")

        with (
            patch("hermes_mori_provider.rest_client.MoriRestClient"),
            patch("hermes_mori_provider.outbox.GovernedWriteOutbox") as MockOutbox,
        ):
            MockOutbox.return_value = MagicMock()
            MoriMemoryProvider().initialize(session_id="t", hermes_home=tmp_path)

        init_kwargs = MockOutbox.call_args.kwargs
        assert init_kwargs["intake_agent_id"] == "myagent"

    def test_session_id_used_as_intake_session_id_by_default(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("MORI_INTAKE_URL", "http://intake.example:8971")
        monkeypatch.setenv("MORI_API_KEY", "deadbeefcafe")
        monkeypatch.delenv("MORI_INTAKE_SESSION_ID", raising=False)

        with (
            patch("hermes_mori_provider.rest_client.MoriRestClient"),
            patch("hermes_mori_provider.outbox.GovernedWriteOutbox") as MockOutbox,
        ):
            MockOutbox.return_value = MagicMock()
            MoriMemoryProvider().initialize(session_id="live-session-xyz", hermes_home=tmp_path)

        init_kwargs = MockOutbox.call_args.kwargs
        assert init_kwargs["intake_session_id"] == "live-session-xyz"


# ── 6. on_memory_write passes intake fields through ──────────────────────────


class TestOnMemoryWriteIntakeFields:
    """on_memory_write enqueues a payload that carries intake_stable_key + action."""

    def _provider(self, tmp_db: Path) -> tuple[MoriMemoryProvider, FakeIntakeClient]:
        ic = FakeIntakeClient()
        p = MoriMemoryProvider()
        p._client = FakeReadClient()
        p._normalizer = HermesEventNormalizer()
        p._session_id = "test-session"
        p._outbox = GovernedWriteOutbox(
            client=p._client,
            db_path=tmp_db,
            intake_client=ic,
            intake_agent_id="hermes",
            intake_session_id="test-session",
            initial_backoff=0.001,
            max_backoff=0.01,
            breaker_cooldown=0.001,
            _sleep=lambda _: None,
        )
        return p, ic

    def test_memory_add_uses_learned_prefix(self, tmp_db: Path) -> None:
        p, ic = self._provider(tmp_db)
        p.on_memory_write(
            "add", "memory", "The deploy command is shown above.", {"memory_id": "deploy-cmd"}
        )
        _wait_drain(p._outbox)
        p.shutdown()
        assert ic.call_count >= 1
        sent = ic.calls[0]
        assert sent["stable_key"].startswith("learned-")
        assert sent["action"] == "add"

    def test_user_replace_uses_preference_prefix(self, tmp_db: Path) -> None:
        p, ic = self._provider(tmp_db)
        p.on_memory_write(
            "replace",
            "user",
            "Fred prefers concise answers in British English.",
            {"user_id": "fred"},
        )
        _wait_drain(p._outbox)
        p.shutdown()
        assert ic.call_count >= 1
        sent = ic.calls[0]
        assert sent["stable_key"].startswith("preference-")
        assert sent["action"] == "replace"

    def test_session_id_carried_from_provider(self, tmp_db: Path) -> None:
        p, ic = self._provider(tmp_db)
        p._session_id = "provider-session-99"
        p._outbox._intake_session_id = "provider-session-99"
        p.on_memory_write(
            "add", "memory", "Important fact about the system.", {"memory_id": "fact-z"}
        )
        _wait_drain(p._outbox)
        p.shutdown()
        sent = ic.calls[0]
        assert sent["session_id"] == "provider-session-99"
