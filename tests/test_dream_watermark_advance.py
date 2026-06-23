"""Regression: the dreamer must ADVANCE the watermark on a valid-but-empty batch, so a
low-signal batch can't permanently stall the queue (observed prod stall: 4629 undreamed,
watermark frozen). A parse FAILURE must NOT advance (retry); dry_run must never mutate.

Mirrors the no-nats mock-store harness in test_dream_intake_promotion.py.
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock

from tests.test_dream_intake_promotion import _make_mock_store, _make_pipeline

EVENTS = [
    {"id": 100, "session_id": "s1", "client": "c1"},
    {"id": 101, "session_id": "s1", "client": "c1"},
    {"id": 104, "session_id": "s2", "client": "c1"},  # gap below max is fine
]
MAX_ID = "104"


def _pipeline_with_events(monkeypatch, *, parse_returns):
    store = _make_mock_store(with_pool=True)
    store.get_dream_state = AsyncMock(return_value="99")  # current watermark
    store.read_events = AsyncMock(return_value=list(EVENTS))
    store.begin_transaction = MagicMock(return_value=nullcontext(MagicMock()))
    pipeline, store, _ = _make_pipeline(store)
    # Neutralise the surrounding machinery — we only exercise the watermark path.
    monkeypatch.setattr(pipeline, "_run_intake_promotion", AsyncMock(return_value=None))
    monkeypatch.setattr(pipeline, "_format_events", lambda events: "formatted")
    monkeypatch.setattr(pipeline, "_call_dream_model", lambda text: "model-output")
    monkeypatch.setattr(pipeline, "_parse_response", lambda r: parse_returns)
    return pipeline, store


def _watermark_writes(store):
    return [
        c
        for c in store.set_dream_state.call_args_list
        if c.args and c.args[0] == "last_dreamed_event_id"
    ]


def test_empty_batch_advances_watermark_to_max_id(monkeypatch):
    pipeline, store = _pipeline_with_events(monkeypatch, parse_returns=[])
    result = asyncio.run(pipeline.run())
    assert result == []
    writes = _watermark_writes(store)
    assert writes, "empty batch must still advance the watermark (else permanent stall)"
    assert writes[-1].args[1] == MAX_ID
    store.prune_events.assert_awaited()  # empty batch also prunes


def test_parse_failure_does_not_advance_watermark(monkeypatch):
    pipeline, store = _pipeline_with_events(monkeypatch, parse_returns=None)
    result = asyncio.run(pipeline.run())
    assert result == []
    assert not _watermark_writes(store), "parse failure must retry — watermark must NOT move"


def test_dry_run_empty_does_not_mutate(monkeypatch):
    pipeline, store = _pipeline_with_events(monkeypatch, parse_returns=[])
    result = asyncio.run(pipeline.run(dry_run=True))
    assert result == []
    assert not _watermark_writes(store), "dry_run must be read-only"
    store.prune_events.assert_not_awaited()


def test_no_events_does_not_advance(monkeypatch):
    # Genuinely no new events → nothing to advance past (distinct from an empty *result*).
    store = _make_mock_store(with_pool=True)
    store.get_dream_state = AsyncMock(return_value="99")
    store.read_events = AsyncMock(return_value=[])
    pipeline, store, _ = _make_pipeline(store)
    monkeypatch.setattr(pipeline, "_run_intake_promotion", AsyncMock(return_value=None))
    assert asyncio.run(pipeline.run()) == []
    assert not _watermark_writes(store)
