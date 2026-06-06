"""Tests for GovernedWriteOutbox (v0.2.0: proposal queue + LWM + coalescing).

All tests are deterministic — no real network, no real sleeps. The clock and
sleep are injected so back-off is fast and verifiable.

Covers:
  * enqueue() returns immediately; drainer calls propose().
  * 429 / transport error -> retry after injected back-off.
  * 4xx non-429 -> mark FAILED (dead-letter), no retry.
  * Backpressure drops + warns above threshold.
  * Restart durability: re-opening drains persisted rows.
  * Coalescing: supersede updates an unsent row in place; retract cancels it.
  * LWM helpers: upsert / mark / set_content / delete / get / all.
  * Circuit breaker trips after N consecutive failures.
  * metrics_snapshot exposes depth + counters.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from hermes_mori_provider.outbox import (
    LWM_CANON,
    LWM_PENDING,
    LWM_REJECTED,
    GovernedWriteOutbox,
)
from hermes_mori_provider.rest_client import MoriTransportError

# ── Fake clients ──────────────────────────────────────────────────────────────


class FakeClient:
    """Minimal fake for MoriRestClient with controllable responses."""

    def __init__(self, responses: list[tuple[int, dict]] | None = None) -> None:
        self._responses = list(responses or [(201, {"status": "created"})])
        self._calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def propose(self, **kwargs: Any) -> tuple[int, dict]:
        with self._lock:
            self._calls.append(dict(kwargs))
            resp = self._responses[0] if len(self._responses) == 1 else self._responses.pop(0)
        return resp

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self._calls)

    @property
    def calls(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._calls)


class TransportErrorClient:
    """Client that raises MoriTransportError (optionally then succeeds)."""

    def __init__(self, then_succeed: bool = False, fail_n: int | None = None) -> None:
        self._called = 0
        self._then_succeed = then_succeed
        self._fail_n = fail_n
        self._lock = threading.Lock()

    def propose(self, **kwargs: Any) -> tuple[int, dict]:
        with self._lock:
            self._called += 1
            called = self._called
        if self._fail_n is not None and called > self._fail_n:
            return (201, {"status": "created"})
        if self._then_succeed and called > 1:
            return (201, {"status": "created"})
        raise MoriTransportError("connection refused")

    @property
    def called(self) -> int:
        with self._lock:
            return self._called


# ── Fixtures / helpers ────────────────────────────────────────────────────────


@pytest.fixture()
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "outbox.db"


def _make_outbox(
    client: Any,
    db_path: Path,
    max_pending: int = 100,
    initial_backoff: float = 0.001,
    max_backoff: float = 0.01,
    breaker_threshold: int = 5,
    breaker_cooldown: float = 0.001,
    autostart_drain: bool = True,
    _sleep: Any = None,
) -> GovernedWriteOutbox:
    sleep_fn = _sleep if _sleep is not None else (lambda _: None)
    return GovernedWriteOutbox(
        client=client,
        db_path=db_path,
        max_pending=max_pending,
        initial_backoff=initial_backoff,
        max_backoff=max_backoff,
        breaker_threshold=breaker_threshold,
        breaker_cooldown=breaker_cooldown,
        autostart_drain=autostart_drain,
        _sleep=sleep_fn,
    )


def _wait_drain(outbox: GovernedWriteOutbox, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if outbox.pending_count() == 0:
            return True
        time.sleep(0.02)
    return False


def _payload(
    name: str = "hermes-memory-test", body: str = "body", op: str = "propose"
) -> dict[str, Any]:
    return {
        "op": op,
        "name": name,
        "title": name,
        "description": "",
        "type": "project",
        "body": body,
        "tags": ["source:hermes"],
        "idempotency_key": "abc123",
    }


# ── Basic enqueue / drain ─────────────────────────────────────────────────────


class TestEnqueueDrain:
    def test_enqueue_returns_true_immediately(self, tmp_db: Path) -> None:
        client = FakeClient()
        outbox = _make_outbox(client, tmp_db)
        start = time.monotonic()
        result = outbox.enqueue(_payload())
        elapsed = time.monotonic() - start
        outbox.shutdown()
        assert result is True
        assert elapsed < 0.1

    def test_drainer_calls_propose(self, tmp_db: Path) -> None:
        client = FakeClient([(201, {"status": "created"})])
        outbox = _make_outbox(client, tmp_db)
        outbox.enqueue(_payload("hermes-memory-drain"))
        drained = _wait_drain(outbox)
        outbox.shutdown()
        assert drained
        assert client.call_count >= 1
        assert client.calls[0]["name"] == "hermes-memory-drain"

    def test_propose_kwargs_match_payload(self, tmp_db: Path) -> None:
        client = FakeClient([(201, {"status": "created"})])
        outbox = _make_outbox(client, tmp_db)
        outbox.enqueue(_payload("hermes-memory-kwargs"))
        _wait_drain(outbox)
        outbox.shutdown()
        sent = client.calls[0]
        assert sent["name"] == "hermes-memory-kwargs"
        assert sent["body"] == "body"
        assert "source:hermes" in sent["tags"]

    def test_4xx_marks_failed_no_retry(self, tmp_db: Path) -> None:
        client = FakeClient([(400, {"error": "bad request"})])
        outbox = _make_outbox(client, tmp_db)
        outbox.enqueue(_payload("hermes-memory-bad"))
        _wait_drain(outbox)
        outbox.shutdown()
        assert client.call_count == 1


# ── Back-off ──────────────────────────────────────────────────────────────────


class TestBackoff:
    def test_429_retries(self, tmp_db: Path) -> None:
        client = FakeClient([(429, {"error": "rate limited"}), (201, {"status": "created"})])
        sleep_calls: list[float] = []
        outbox = _make_outbox(client, tmp_db, _sleep=sleep_calls.append)
        outbox.enqueue(_payload("hermes-memory-rate"))
        drained = _wait_drain(outbox)
        outbox.shutdown()
        assert drained
        assert client.call_count == 2
        assert len(sleep_calls) >= 1

    def test_backoff_is_capped(self, tmp_db: Path) -> None:
        responses = [(429, {})] * 5 + [(201, {"status": "ok"})]
        client = FakeClient(responses)
        sleep_calls: list[float] = []
        outbox = _make_outbox(
            client,
            tmp_db,
            initial_backoff=0.5,
            max_backoff=2.0,
            breaker_threshold=999,  # don't let breaker cooldown interfere
            _sleep=sleep_calls.append,
        )
        outbox.enqueue(_payload("hermes-memory-cap"))
        drained = _wait_drain(outbox)
        outbox.shutdown()
        assert drained
        for s in sleep_calls:
            assert s <= 2.0 + 1e-6

    def test_transport_error_retries(self, tmp_db: Path) -> None:
        client = TransportErrorClient(then_succeed=True)
        sleep_calls: list[float] = []
        outbox = _make_outbox(client, tmp_db, _sleep=sleep_calls.append)
        outbox.enqueue(_payload("hermes-memory-transport"))
        drained = _wait_drain(outbox)
        outbox.shutdown()
        assert drained
        assert client.called >= 2
        assert len(sleep_calls) >= 1


# ── Circuit breaker ───────────────────────────────────────────────────────────


class TestCircuitBreaker:
    def test_breaker_trips_after_threshold(self, tmp_db: Path) -> None:
        # Fail 5 times then succeed; breaker_threshold=3 -> trips once.
        client = TransportErrorClient(fail_n=5)
        cooldowns: list[float] = []
        outbox = _make_outbox(
            client,
            tmp_db,
            breaker_threshold=3,
            breaker_cooldown=0.123,
            _sleep=cooldowns.append,
        )
        outbox.enqueue(_payload("hermes-memory-breaker"))
        drained = _wait_drain(outbox)
        snap = outbox.metrics_snapshot()
        outbox.shutdown()
        assert drained
        assert snap["breaker_trips"] >= 1
        # The cooldown value must have been used at least once.
        assert any(abs(c - 0.123) < 1e-9 for c in cooldowns)

    def test_breaker_resets_on_success(self, tmp_db: Path) -> None:
        client = TransportErrorClient(fail_n=2)
        outbox = _make_outbox(client, tmp_db, breaker_threshold=5)
        outbox.enqueue(_payload("hermes-memory-reset"))
        _wait_drain(outbox)
        snap = outbox.metrics_snapshot()
        outbox.shutdown()
        # 2 failures < threshold 5 -> never tripped; ends closed.
        assert snap["breaker_open"] == 0
        assert snap["breaker_trips"] == 0


# ── Backpressure ──────────────────────────────────────────────────────────────


class TestBackpressure:
    def test_drops_when_above_threshold(self, tmp_db: Path, caplog: Any) -> None:
        import logging

        client = FakeClient([(429, {})] * 1000)
        with caplog.at_level(logging.WARNING, logger="hermes_mori_provider.outbox"):
            outbox = _make_outbox(client, tmp_db, max_pending=3, breaker_threshold=999)
            for i in range(3):
                outbox.enqueue(_payload(f"hermes-memory-ok-{i}"))
            time.sleep(0.1)
            for i in range(3):
                outbox.enqueue(_payload(f"hermes-memory-fill-{i}"))
            result = outbox.enqueue(_payload("hermes-memory-overflow"))
            outbox.shutdown()
        assert result is False
        assert any("backpressure" in r.message.lower() for r in caplog.records)


# ── Coalescing ────────────────────────────────────────────────────────────────


class TestCoalescing:
    def test_supersede_updates_unsent_row_in_place(self, tmp_db: Path) -> None:
        """add then replace while still local -> ONE row, latest body sent.

        Drain is gated until both enqueues land, so the supersede coalesces into
        the queued (never-sent) row.
        """
        client = FakeClient([(201, {})] * 10)
        outbox = _make_outbox(client, tmp_db, autostart_drain=False)
        outbox.enqueue(_payload("hermes-memory-x", body="v1", op="propose"))
        outbox.enqueue(_payload("hermes-memory-x", body="v2", op="supersede"))
        # Still exactly one unsent row (coalesced in place).
        assert outbox.pending_count() == 1
        outbox.resume_drain()
        _wait_drain(outbox)
        outbox.shutdown()
        # Only the coalesced (latest) body should have been proposed, exactly once.
        bodies = [c["body"] for c in client.calls]
        assert bodies == ["v2"]
        assert client.call_count == 1

    def test_retract_cancels_unsent_row(self, tmp_db: Path) -> None:
        """add then remove while still local -> nothing sent (net no-op)."""
        client = FakeClient([(201, {})] * 10)
        outbox = _make_outbox(client, tmp_db, autostart_drain=False)
        outbox.enqueue(_payload("hermes-memory-y", body="v1", op="propose"))
        result = outbox.enqueue(_payload("hermes-memory-y", op="retract"))
        assert result is True
        assert outbox.pending_count() == 0
        outbox.resume_drain()
        time.sleep(0.05)
        outbox.shutdown()
        assert client.call_count == 0

    def test_retract_with_no_unsent_row_emits_retraction(self, tmp_db: Path) -> None:
        """remove with nothing local -> a retraction proposal IS sent."""
        client = FakeClient([(201, {})])
        outbox = _make_outbox(client, tmp_db)
        outbox.enqueue(_payload("hermes-memory-z", op="retract"))
        drained = _wait_drain(outbox)
        outbox.shutdown()
        assert drained
        assert client.call_count == 1
        assert client.calls[0]["name"] == "hermes-memory-z"


# ── LWM helpers ───────────────────────────────────────────────────────────────


class TestLWM:
    def test_upsert_then_get(self, tmp_db: Path) -> None:
        outbox = _make_outbox(FakeClient(), tmp_db)
        outbox.lwm_upsert(
            name="hermes-memory-a",
            target="memory",
            content="hello",
            content_hash="h1",
            session_id="s1",
        )
        row = outbox.lwm_get("hermes-memory-a")
        outbox.shutdown()
        assert row is not None
        assert row["content"] == "hello"
        assert row["status"] == LWM_PENDING
        assert row["session_id"] == "s1"

    def test_upsert_is_idempotent_by_name(self, tmp_db: Path) -> None:
        outbox = _make_outbox(FakeClient(), tmp_db)
        outbox.lwm_upsert(name="hermes-memory-a", target="memory", content="v1", content_hash="h1")
        outbox.lwm_upsert(name="hermes-memory-a", target="memory", content="v2", content_hash="h2")
        rows = outbox.lwm_all()
        outbox.shutdown()
        assert len(rows) == 1
        assert rows[0]["content"] == "v2"

    def test_mark_status(self, tmp_db: Path) -> None:
        outbox = _make_outbox(FakeClient(), tmp_db)
        outbox.lwm_upsert(name="hermes-memory-a", target="memory", content="c", content_hash="h")
        outbox.lwm_mark("hermes-memory-a", LWM_CANON)
        row = outbox.lwm_get("hermes-memory-a")
        outbox.shutdown()
        assert row["status"] == LWM_CANON
        assert row["last_reconciled_at"] is not None

    def test_set_content_overwrites(self, tmp_db: Path) -> None:
        outbox = _make_outbox(FakeClient(), tmp_db)
        outbox.lwm_upsert(name="hermes-memory-a", target="memory", content="old", content_hash="h1")
        outbox.lwm_set_content("hermes-memory-a", "new canon", "h2", LWM_CANON)
        row = outbox.lwm_get("hermes-memory-a")
        outbox.shutdown()
        assert row["content"] == "new canon"
        assert row["content_hash"] == "h2"
        assert row["status"] == LWM_CANON

    def test_delete(self, tmp_db: Path) -> None:
        outbox = _make_outbox(FakeClient(), tmp_db)
        outbox.lwm_upsert(name="hermes-memory-a", target="memory", content="c", content_hash="h")
        outbox.lwm_delete("hermes-memory-a")
        gone = outbox.lwm_get("hermes-memory-a")
        outbox.shutdown()
        assert gone is None

    def test_all_excludes_rejected_by_default(self, tmp_db: Path) -> None:
        outbox = _make_outbox(FakeClient(), tmp_db)
        outbox.lwm_upsert(name="hermes-memory-a", target="memory", content="c", content_hash="h")
        outbox.lwm_upsert(
            name="hermes-memory-b",
            target="memory",
            content="c",
            content_hash="h",
            status=LWM_REJECTED,
        )
        names = [r["name"] for r in outbox.lwm_all()]
        all_names = [r["name"] for r in outbox.lwm_all(exclude_rejected=False)]
        outbox.shutdown()
        assert "hermes-memory-a" in names
        assert "hermes-memory-b" not in names
        assert "hermes-memory-b" in all_names

    def test_pending_count(self, tmp_db: Path) -> None:
        outbox = _make_outbox(FakeClient(), tmp_db)
        outbox.lwm_upsert(name="hermes-memory-a", target="memory", content="c", content_hash="h")
        outbox.lwm_upsert(
            name="hermes-memory-b",
            target="memory",
            content="c",
            content_hash="h",
            status=LWM_CANON,
        )
        count = outbox.lwm_pending_count()
        outbox.shutdown()
        assert count == 1


# ── Metrics ───────────────────────────────────────────────────────────────────


class TestMetrics:
    def test_snapshot_keys_present(self, tmp_db: Path) -> None:
        outbox = _make_outbox(FakeClient(), tmp_db)
        snap = outbox.metrics_snapshot()
        outbox.shutdown()
        for key in ("outbox_depth", "lwm_pending", "proposals_sent", "proposals_failed"):
            assert key in snap

    def test_proposals_sent_increments(self, tmp_db: Path) -> None:
        client = FakeClient([(201, {})])
        outbox = _make_outbox(client, tmp_db)
        outbox.enqueue(_payload("hermes-memory-metric"))
        _wait_drain(outbox)
        snap = outbox.metrics_snapshot()
        outbox.shutdown()
        assert snap["proposals_sent"] >= 1


# ── Restart / persistence ─────────────────────────────────────────────────────


class TestRestart:
    def test_pending_rows_drained_after_restart(self, tmp_db: Path) -> None:
        import json
        import sqlite3

        from hermes_mori_provider.outbox import _PENDING, _SCHEMA, _now

        tmp_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(tmp_db))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        now = _now()
        conn.execute(
            """
            INSERT INTO outbox
                (name, title, description, type, body, tags, idempotency,
                 op, status, attempts, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                "hermes-memory-restart",
                "hermes-memory-restart",
                "",
                "project",
                "body",
                json.dumps(["source:hermes"]),
                "idem-abc",
                "propose",
                _PENDING,
                now,
                now,
            ),
        )
        conn.commit()
        conn.close()

        client2 = FakeClient([(201, {"status": "created"})])
        outbox2 = _make_outbox(client2, tmp_db)
        drained = _wait_drain(outbox2)
        outbox2.shutdown()
        assert drained
        assert client2.call_count >= 1
        assert client2.calls[0]["name"] == "hermes-memory-restart"
