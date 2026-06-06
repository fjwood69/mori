"""Tests for the mori-intake rate limiter and eligibility-bypass guard.

Two test classes:

1. ``TestIntakeRateLimiter`` — pure logic, no DB, no server.
   Drives ``IntakeRateLimiter`` directly with an injected clock so the tests
   are deterministic and instantaneous.

2. ``TestSubmissionsRateLimit`` — app-level (TestClient / ASGI), no DB.
   Patches ``mori_intake.app.get_limiter`` to inject a limiter with a very low
   threshold and stubs out ``mori_intake.db`` so no Postgres is needed.

3. ``TestEligibilityBypassServer`` — app-level (TestClient / ASGI), no DB.
   Proves that disallowed ``stable_key`` namespaces are rejected server-side
   with 422 ``namespace-not-allowlisted`` and that no submission row is
   created — the agent cannot bypass the gate by choosing an eligible stable_key
   value client-side.
"""

from __future__ import annotations

import time
import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from mori_intake.ratelimit import IntakeRateLimiter

# ── 1. Pure-logic limiter tests ───────────────────────────────────────────────


class TestIntakeRateLimiter:
    """Unit tests for IntakeRateLimiter — no I/O, injected clock."""

    def test_allows_within_limit(self) -> None:
        lim = IntakeRateLimiter(limit_per_min=5, _clock=time.monotonic)
        for _ in range(5):
            v = lim.check("key-a")
            assert v.allowed

    def test_rejects_on_exhaustion(self) -> None:
        lim = IntakeRateLimiter(limit_per_min=3, _clock=time.monotonic)
        for _ in range(3):
            lim.check("key-a")
        v = lim.check("key-a")
        assert not v.allowed
        assert v.retry_after >= 1

    def test_nth_request_429s(self) -> None:
        """After N allowed requests the (N+1)th is denied."""
        lim = IntakeRateLimiter(limit_per_min=2, _clock=time.monotonic)
        v1 = lim.check("k")
        v2 = lim.check("k")
        v3 = lim.check("k")
        assert v1.allowed
        assert v2.allowed
        assert not v3.allowed

    def test_different_keys_are_independent(self) -> None:
        lim = IntakeRateLimiter(limit_per_min=1, _clock=time.monotonic)
        assert lim.check("key-a").allowed
        assert lim.check("key-b").allowed  # separate bucket
        assert not lim.check("key-a").allowed

    def test_window_reset_allows_again(self) -> None:
        """After the window expires the bucket refills."""
        ticks: list[float] = [0.0, 0.5, 61.0]  # 0s, 0.5s, 61s
        idx = [0]

        def fake_clock() -> float:
            t = ticks[idx[0]]
            if idx[0] < len(ticks) - 1:
                idx[0] += 1
            return t

        lim = IntakeRateLimiter(limit_per_min=1, window_seconds=60, _clock=fake_clock)
        assert lim.check("k").allowed  # tick 0s — uses the first token
        assert not lim.check("k").allowed  # tick 0.5s — bucket empty
        assert lim.check("k").allowed  # tick 61s — new window, refilled

    def test_disabled_when_limit_zero(self) -> None:
        lim = IntakeRateLimiter(limit_per_min=0)
        for _ in range(100):
            assert lim.check("any-key").allowed

    def test_enabled_property(self) -> None:
        assert IntakeRateLimiter(limit_per_min=10).enabled
        assert not IntakeRateLimiter(limit_per_min=0).enabled

    def test_retry_after_is_positive(self) -> None:
        lim = IntakeRateLimiter(limit_per_min=1, _clock=time.monotonic)
        lim.check("k")
        v = lim.check("k")
        assert not v.allowed
        assert v.retry_after >= 1

    def test_denied_request_does_not_drain_bucket(self) -> None:
        """A denied call must not decrement the already-empty bucket further."""
        lim = IntakeRateLimiter(limit_per_min=2, _clock=time.monotonic)
        lim.check("k")  # 1 left
        lim.check("k")  # 0 left
        lim.check("k")  # denied — must not underflow
        lim.check("k")  # still denied
        v = lim.check("k")
        assert not v.allowed  # still correctly denied, not wrapped around


# ── App-level test helpers ────────────────────────────────────────────────────


class _AppFixture:
    """Context-manager fixture that keeps all patches live for the test body."""

    def __init__(self, fake_key_name: str = "test-agent") -> None:
        self._fake_key_name = fake_key_name
        self._stack = ExitStack()
        self.fake_pool: MagicMock | None = None
        self.client: TestClient | None = None

    def __enter__(self):
        import mori_intake.app as intake_app

        s = self._stack
        fake_key_name = self._fake_key_name

        s.enter_context(
            patch(
                "mori_intake.app.check_key",
                side_effect=lambda k: fake_key_name if k == "test-key" else None,
            )
        )
        s.enter_context(patch("mori_intake.app.role_for", return_value="write"))
        s.enter_context(patch("mori_intake.app.ROLE_LEVELS", {"read": 1, "write": 2, "dreamer": 3}))
        s.enter_context(patch("mori_intake.app.init_auth"))
        s.enter_context(patch("mori_intake.app.check_data_boundary"))
        db_mock = s.enter_context(patch("mori_intake.app.db"))
        migrations_mock = s.enter_context(patch("mori_intake.app.migrations"))
        worker_mock = s.enter_context(patch("mori_intake.app.worker"))

        fake_pool = MagicMock()
        fake_pool.fetchval = AsyncMock(return_value=uuid.uuid4())
        fake_pool.fetch = AsyncMock(return_value=[])
        db_mock.get_pool = MagicMock(return_value=fake_pool)
        db_mock.create_pool = AsyncMock(return_value=fake_pool)
        migrations_mock.apply = AsyncMock()
        worker_mock.run_loop = MagicMock(return_value=AsyncMock())

        self.fake_pool = fake_pool
        # Use lifespan=False so the TestClient doesn't trigger the startup events
        # (which would try to connect to Postgres).
        self.client = TestClient(intake_app.app, raise_server_exceptions=True)
        return self

    def __exit__(self, *args):
        self._stack.close()


# ── 2. App-level rate-limit tests ─────────────────────────────────────────────


class TestSubmissionsRateLimit:
    """Verifies the 429 response shape and Retry-After header."""

    _VALID_BODY = {
        "session_id": "sess-1",
        "agent_id": "hermes",
        "target": "memory",
        "action": "add",
        "stable_key": "learned-test-key",
        "content": "This is a valid durable proposition about the system.",
    }
    _HEADERS = {"x-api-key": "test-key"}

    def test_requests_within_limit_succeed(self) -> None:
        from mori_intake import ratelimit

        old = ratelimit._limiter
        ratelimit._limiter = IntakeRateLimiter(limit_per_min=5)
        try:
            with _AppFixture() as fix:
                for _ in range(3):
                    resp = fix.client.post(
                        "/intake/submissions", json=self._VALID_BODY, headers=self._HEADERS
                    )
                    assert resp.status_code in (202, 422), resp.text
        finally:
            ratelimit._limiter = old

    def test_nth_request_returns_429(self) -> None:
        """After the limit is exhausted the response is 429 with the right shape."""
        from mori_intake import ratelimit

        old = ratelimit._limiter
        ratelimit._limiter = IntakeRateLimiter(limit_per_min=2)
        try:
            with _AppFixture() as fix:
                # Exhaust the 2-request bucket.
                fix.client.post("/intake/submissions", json=self._VALID_BODY, headers=self._HEADERS)
                fix.client.post("/intake/submissions", json=self._VALID_BODY, headers=self._HEADERS)
                # Third request must be 429.
                resp = fix.client.post(
                    "/intake/submissions", json=self._VALID_BODY, headers=self._HEADERS
                )
                assert resp.status_code == 429
                body = resp.json()
                assert body["status"] == "rate_limited"
                assert "retry_after" in body
                assert int(body["retry_after"]) >= 1
                assert "retry-after" in {h.lower() for h in resp.headers}
        finally:
            ratelimit._limiter = old

    def test_429_response_has_retry_after_header(self) -> None:
        from mori_intake import ratelimit

        old = ratelimit._limiter
        ratelimit._limiter = IntakeRateLimiter(limit_per_min=1)
        try:
            with _AppFixture() as fix:
                fix.client.post("/intake/submissions", json=self._VALID_BODY, headers=self._HEADERS)
                resp = fix.client.post(
                    "/intake/submissions", json=self._VALID_BODY, headers=self._HEADERS
                )
                assert resp.status_code == 429
                assert int(resp.headers["retry-after"]) >= 1
        finally:
            ratelimit._limiter = old

    def test_read_endpoints_never_rate_limited(self) -> None:
        """GET /intake/candidates is not subject to rate limiting."""
        from mori_intake import ratelimit

        old = ratelimit._limiter
        ratelimit._limiter = IntakeRateLimiter(limit_per_min=1)
        try:
            with _AppFixture() as fix:
                # Exhaust the write bucket.
                fix.client.post("/intake/submissions", json=self._VALID_BODY, headers=self._HEADERS)
                fix.client.post("/intake/submissions", json=self._VALID_BODY, headers=self._HEADERS)
                # GET must still be served (not 429).
                resp = fix.client.get("/intake/candidates", headers=self._HEADERS)
                assert resp.status_code != 429
        finally:
            ratelimit._limiter = old

    def test_limiter_keyed_per_api_key(self) -> None:
        """Two different API keys have independent buckets."""
        from mori_intake import ratelimit

        old = ratelimit._limiter
        ratelimit._limiter = IntakeRateLimiter(limit_per_min=1)
        try:
            with _AppFixture() as fix:
                # Override check_key inside the already-active fixture.
                with patch(
                    "mori_intake.app.check_key",
                    side_effect=lambda k: {"test-key": "c1", "other-key": "c2"}.get(k),
                ):
                    # c1: first OK, second 429.
                    r1 = fix.client.post(
                        "/intake/submissions",
                        json=self._VALID_BODY,
                        headers={"x-api-key": "test-key"},
                    )
                    assert r1.status_code in (202, 422)
                    r2 = fix.client.post(
                        "/intake/submissions",
                        json=self._VALID_BODY,
                        headers={"x-api-key": "test-key"},
                    )
                    assert r2.status_code == 429
                    # c2: completely fresh bucket.
                    r3 = fix.client.post(
                        "/intake/submissions",
                        json=self._VALID_BODY,
                        headers={"x-api-key": "other-key"},
                    )
                    assert r3.status_code in (202, 422)
        finally:
            ratelimit._limiter = old


# ── 3. Eligibility-bypass tests ───────────────────────────────────────────────


class TestEligibilityBypassServer:
    """Proves server-side eligibility gate rejects disallowed namespaces.

    The gate is default-deny on the server; no client-supplied flag can bypass it.
    Checks that:
    - stable_key in {psychology-x, scratch-x, health-x, temp-x} → 422
      with reason "namespace-not-allowlisted".
    - No submission row is created (pool.fetchval not called with INSERT args).
    """

    _CONTENT = "This is a valid proposition about the deployment system."
    _HEADERS = {"x-api-key": "test-key"}

    @pytest.mark.parametrize(
        "stable_key",
        [
            "psychology-inference-about-user",
            "scratch-temp-note",
            "health-dietary-data",
            "temp-working-note",
        ],
    )
    def test_disallowed_namespace_returns_422(self, stable_key: str) -> None:
        from mori_intake import ratelimit

        old = ratelimit._limiter
        ratelimit._limiter = IntakeRateLimiter(limit_per_min=1000)  # don't throttle
        try:
            with _AppFixture() as fix:
                body = {
                    "session_id": "sess-bypass",
                    "agent_id": "hermes",
                    "target": "memory",
                    "action": "add",
                    "stable_key": stable_key,
                    "content": self._CONTENT,
                }
                resp = fix.client.post("/intake/submissions", json=body, headers=self._HEADERS)
                assert resp.status_code == 422, (
                    f"Expected 422 for {stable_key!r}, got {resp.status_code}: {resp.text}"
                )
                data = resp.json()
                assert data["status"] == "rejected"
                assert data["reason"] == "namespace-not-allowlisted"
        finally:
            ratelimit._limiter = old

    @pytest.mark.parametrize(
        "stable_key",
        [
            "psychology-inference-about-user",
            "scratch-temp-note",
            "health-dietary-data",
            "temp-working-note",
        ],
    )
    def test_disallowed_namespace_no_db_insert(self, stable_key: str) -> None:
        """The eligibility gate fires BEFORE the DB insert — no row is created."""
        from mori_intake import ratelimit

        old = ratelimit._limiter
        ratelimit._limiter = IntakeRateLimiter(limit_per_min=1000)
        try:
            with _AppFixture() as fix:
                body = {
                    "session_id": "sess-bypass",
                    "agent_id": "hermes",
                    "target": "memory",
                    "action": "add",
                    "stable_key": stable_key,
                    "content": self._CONTENT,
                }
                fix.client.post("/intake/submissions", json=body, headers=self._HEADERS)
                # pool.fetchval should NOT have been called — gate returned before INSERT.
                fix.fake_pool.fetchval.assert_not_called()
        finally:
            ratelimit._limiter = old

    def test_user_target_hard_deny_prefixes(self) -> None:
        """psychology-*, health-*, mood-* on user target are hard-denied."""
        from mori_intake import ratelimit

        old = ratelimit._limiter
        ratelimit._limiter = IntakeRateLimiter(limit_per_min=1000)
        try:
            with _AppFixture() as fix:
                for sk in ("psychology-trait", "health-metric", "mood-current"):
                    body = {
                        "session_id": "sess-x",
                        "agent_id": "hermes",
                        "target": "user",
                        "action": "add",
                        "stable_key": sk,
                        "content": self._CONTENT,
                    }
                    resp = fix.client.post("/intake/submissions", json=body, headers=self._HEADERS)
                    assert resp.status_code == 422, f"Expected 422 for {sk!r}"
                    assert resp.json()["reason"] == "namespace-not-allowlisted"
                # No DB inserts across all rejected calls.
                fix.fake_pool.fetchval.assert_not_called()
        finally:
            ratelimit._limiter = old

    def test_eligible_namespace_reaches_db(self) -> None:
        """An eligible stable_key passes the gate and attempts the DB insert."""
        from mori_intake import ratelimit

        old = ratelimit._limiter
        ratelimit._limiter = IntakeRateLimiter(limit_per_min=1000)
        try:
            with _AppFixture() as fix:
                body = {
                    "session_id": "sess-ok",
                    "agent_id": "hermes",
                    "target": "memory",
                    "action": "add",
                    "stable_key": "learned-deploy-command",
                    "content": "The deploy command for the staging cluster is shown above.",
                }
                resp = fix.client.post("/intake/submissions", json=body, headers=self._HEADERS)
                # The gate passed — DB was consulted.
                assert resp.status_code == 202
                assert fix.fake_pool.fetchval.called
        finally:
            ratelimit._limiter = old
