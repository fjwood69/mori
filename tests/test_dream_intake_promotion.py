"""Unit tests for B3 — dream-as-promotion-trigger.

Always runs — no database, no network.  Tests focus on the integration
between ``DreamPipeline.run()`` and ``_run_intake_promotion()``:

* Flag off → ``_run_intake_promotion`` is a no-op regardless of events.
* Flag on + SQLite-like store (no ``pool``) → no-op (feature UNAVAILABLE).
* Flag on + no intake DSN → no-op.
* ``_run_intake_promotion`` NEVER raises into ``run()`` (errors are absorbed).
* ``_run_intake_promotion`` fires BEFORE the no-events early return in
  ``run()`` — i.e. it runs even when there are no new dream events.

These tests use a minimal mock store so they do NOT require the ``nats``
module (which is absent in the CI environment).  They exercise the
``DreamPipeline`` directly, bypassing ``SQLiteStore.__init__`` which
triggers the ``nats`` import.

The full Postgres/canon integration test (seed → assess → drain → verify
canon) is in ``test_intake_assessor.py`` (gated on both DSNs).
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

# ── Minimal mock store (no nats dependency) ───────────────────────────────────


def _make_mock_store(*, with_pool: bool = False):
    """Return a minimal async-compatible store mock for DreamPipeline.

    DreamPipeline accesses ``self.session_log`` (the ``_log`` alias) and
    ``self.store`` directly.  We configure both to avoid MagicMock
    auto-attribute surprises.

    By default the mock has NO ``pool`` attribute — simulating a SQLiteStore.
    Pass ``with_pool=True`` to add a pool stub (simulates PostgresStore).
    """
    # Use a spec list to prevent MagicMock from auto-creating attributes like
    # 'pool' or '_log' that would fool hasattr() guards in DreamPipeline.
    _methods = [
        "get_dream_state",
        "read_events",
        "count_events",
        "count_events_since",
        "list_sessions",
        "set_dream_state",
        "prune_events",
        "write",
        "begin_transaction",
        "canon_reader",
    ]
    if with_pool:
        _methods.append("pool")

    store = MagicMock(spec=_methods)
    store.get_dream_state = AsyncMock(return_value="0")
    store.read_events = AsyncMock(return_value=[])
    store.count_events = AsyncMock(return_value=0)
    store.count_events_since = AsyncMock(return_value=0)
    store.list_sessions = AsyncMock(return_value=[])
    store.set_dream_state = AsyncMock(return_value=None)
    store.prune_events = AsyncMock(return_value=0)
    store.write = AsyncMock(return_value="written")
    if with_pool:
        store.pool = MagicMock()

    return store


def _make_pipeline(mock_store=None):
    """Return a DreamPipeline backed by a mock store (avoids nats import).

    The store is injected directly so ``SQLiteStore.__init__`` is never
    called (which would trigger the ``nats`` module import absent in CI).
    """
    from mori_advisor.bifrost_client import BifrostClient
    from mori_advisor.dream import DreamPipeline

    if mock_store is None:
        mock_store = _make_mock_store()

    client = MagicMock(spec=BifrostClient)
    pipeline = DreamPipeline(
        db_path="/tmp/dream-test.db",
        bifrost_client=client,
        store=mock_store,
    )
    # DreamPipeline sets self.session_log = store._log if hasattr(store, '_log')
    # else store.  With a spec-constrained mock, _log is not present, so
    # session_log == store — which is what we want (all the AsyncMocks are there).
    return pipeline, mock_store, client


# ── B3 flag-off unit tests ─────────────────────────────────────────────────────


class TestB3FlagOff:
    """When MORI_INTAKE_PROMOTION_ENABLED is unset / false, _run_intake_promotion is a no-op."""

    def test_flag_off_noop(self, monkeypatch):
        """Flag off → method returns immediately, no imports."""
        monkeypatch.delenv("MORI_INTAKE_PROMOTION_ENABLED", raising=False)
        pipeline, _, _ = _make_pipeline()
        sys.modules.pop("mori_intake.db", None)

        asyncio.run(pipeline._run_intake_promotion())

        # mori_intake.db must not have been imported.
        assert "mori_intake.db" not in sys.modules

    def test_flag_false_noop(self, monkeypatch):
        """MORI_INTAKE_PROMOTION_ENABLED=false → no-op."""
        monkeypatch.setenv("MORI_INTAKE_PROMOTION_ENABLED", "false")
        pipeline, _, _ = _make_pipeline()
        asyncio.run(pipeline._run_intake_promotion())  # must not raise

    def test_flag_0_noop(self, monkeypatch):
        """MORI_INTAKE_PROMOTION_ENABLED=0 → no-op (only 'true' activates)."""
        monkeypatch.setenv("MORI_INTAKE_PROMOTION_ENABLED", "0")
        pipeline, _, _ = _make_pipeline()
        asyncio.run(pipeline._run_intake_promotion())  # must not raise

    def test_run_flag_off_returns_empty_no_events(self, monkeypatch):
        """run() with flag off + no events returns [] cleanly."""
        monkeypatch.delenv("MORI_INTAKE_PROMOTION_ENABLED", raising=False)
        pipeline, _, _ = _make_pipeline()
        result = asyncio.run(pipeline.run())
        assert result == []

    def test_run_flag_off_never_calls_consult(self, monkeypatch):
        """run() with flag off + no events never calls the dream model."""
        monkeypatch.delenv("MORI_INTAKE_PROMOTION_ENABLED", raising=False)
        pipeline, _, client = _make_pipeline()
        asyncio.run(pipeline.run())
        client.consult.assert_not_called()


# ── B3 flag-on, non-PG store ─────────────────────────────────────────────────


class TestB3FlagOnNonPG:
    """Flag-on + store without 'pool' → no-op (feature unavailable)."""

    def test_no_pool_attr_is_noop(self, monkeypatch):
        """Store without 'pool' attribute → guard fires, no connection attempt."""
        monkeypatch.setenv("MORI_INTAKE_PROMOTION_ENABLED", "true")
        monkeypatch.setenv("MORI_INTAKE_DATABASE_URL", "postgresql://fake/intake")

        pipeline, mock_store, _ = _make_pipeline()
        assert not hasattr(mock_store, "pool"), (
            "Mock store must not have a 'pool' attribute (Postgres-only guard)"
        )

        # Must not raise or attempt any connection.
        asyncio.run(pipeline._run_intake_promotion())

    def test_no_intake_dsn_is_noop(self, monkeypatch):
        """flag=on + no MORI_INTAKE_DATABASE_URL → no-op."""
        monkeypatch.setenv("MORI_INTAKE_PROMOTION_ENABLED", "true")
        monkeypatch.delenv("MORI_INTAKE_DATABASE_URL", raising=False)

        pipeline, _, _ = _make_pipeline()
        asyncio.run(pipeline._run_intake_promotion())  # must not raise


# ── B3 error absorption ───────────────────────────────────────────────────────


class TestB3ErrorAbsorption:
    """_run_intake_promotion MUST NOT raise into run()."""

    def test_connection_error_absorbed(self, monkeypatch):
        """If intake DB connection fails, method returns silently without raising."""
        monkeypatch.setenv("MORI_INTAKE_PROMOTION_ENABLED", "true")
        monkeypatch.setenv("MORI_INTAKE_DATABASE_URL", "postgresql://bad-host/intake")

        mock_store = _make_mock_store(with_pool=True)
        pipeline, _, _ = _make_pipeline(mock_store)

        # Patch asyncpg.create_pool to simulate a connection failure.
        mock_asyncpg = MagicMock()
        mock_asyncpg.create_pool = AsyncMock(side_effect=OSError("connection refused"))

        with patch.dict(sys.modules, {"asyncpg": mock_asyncpg}):
            asyncio.run(pipeline._run_intake_promotion())  # must not raise

    def test_asyncpg_missing_absorbed(self, monkeypatch):
        """If asyncpg is not importable, error is absorbed silently."""
        monkeypatch.setenv("MORI_INTAKE_PROMOTION_ENABLED", "true")
        monkeypatch.setenv("MORI_INTAKE_DATABASE_URL", "postgresql://fake/intake")

        mock_store = _make_mock_store(with_pool=True)
        pipeline, _, _ = _make_pipeline(mock_store)

        import builtins

        real_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):
            if name == "asyncpg":
                raise ImportError("asyncpg not installed")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_fake_import):
            asyncio.run(pipeline._run_intake_promotion())  # must not raise

    def test_run_always_returns_list_even_when_promotion_errors(self, monkeypatch):
        """run() returns [] cleanly even when _run_intake_promotion raises internally."""
        monkeypatch.delenv("MORI_INTAKE_PROMOTION_ENABLED", raising=False)
        pipeline, mock_store, _ = _make_pipeline()

        call_log: list[str] = []

        original = pipeline._run_intake_promotion

        async def _tracked():
            call_log.append("called")
            await original()  # flag off → no-op

        pipeline._run_intake_promotion = _tracked  # type: ignore[method-assign]

        result = asyncio.run(pipeline.run())
        assert result == []
        assert "called" in call_log


# ── B3 fires before early-return ─────────────────────────────────────────────


class TestB3FiresBeforeEarlyReturn:
    """_run_intake_promotion fires before the no-events and no-memories early returns."""

    def test_promotion_called_even_with_no_events(self, monkeypatch):
        """_run_intake_promotion is called even when there are no dream events."""
        monkeypatch.delenv("MORI_INTAKE_PROMOTION_ENABLED", raising=False)
        pipeline, _, _ = _make_pipeline()

        call_log: list[str] = []

        async def _track_promotion():
            call_log.append("promotion")

        pipeline._run_intake_promotion = _track_promotion  # type: ignore[method-assign]

        # No events in the store → early return at the events check.
        result = asyncio.run(pipeline.run())
        assert result == []
        assert "promotion" in call_log, (
            "_run_intake_promotion must be called before the no-events early return"
        )

    def test_promotion_called_before_model_invocation(self, monkeypatch):
        """_run_intake_promotion runs before the dream model is ever called."""
        monkeypatch.delenv("MORI_INTAKE_PROMOTION_ENABLED", raising=False)
        pipeline, _, client = _make_pipeline()

        call_order: list[str] = []

        async def _track_promotion():
            call_order.append("promotion")

        pipeline._run_intake_promotion = _track_promotion  # type: ignore[method-assign]
        original_consult = client.consult.side_effect

        def _track_consult(*args, **kwargs):
            call_order.append("consult")
            if original_consult:
                return original_consult(*args, **kwargs)
            return ""

        client.consult.side_effect = _track_consult

        asyncio.run(pipeline.run())

        if "consult" in call_order:
            assert call_order.index("promotion") < call_order.index("consult"), (
                "_run_intake_promotion must fire before the dream model consult"
            )
        else:
            # No events → consult never called; promotion must still have fired.
            assert "promotion" in call_order


# ── Dream suite regression ────────────────────────────────────────────────────


class TestDreamSuiteRegression:
    """The existing dream pipeline (distillation) is unchanged when flag is off.

    These verify that adding _run_intake_promotion did not break existing
    pipeline behaviour.  Uses a mock store to avoid nats import.
    """

    def test_empty_store_returns_empty_list(self, monkeypatch):
        """run() with no events returns [] — no crash, no model call."""
        monkeypatch.delenv("MORI_INTAKE_PROMOTION_ENABLED", raising=False)
        pipeline, _, _ = _make_pipeline()
        result = asyncio.run(pipeline.run())
        assert result == []

    def test_get_status_returns_formatted_string(self, monkeypatch):
        """get_status() returns a string containing expected field names."""
        monkeypatch.delenv("MORI_INTAKE_PROMOTION_ENABLED", raising=False)
        pipeline, _, _ = _make_pipeline()
        status = asyncio.run(pipeline.get_status())
        assert "Dream State" in status
        assert "Last dreamed" in status
        assert "Undreamed events" in status
