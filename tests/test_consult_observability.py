"""Observability for outbound LLM calls, the consult slot queue, and dream runs.

Raised by FOLLOW-ON-consult-observability-2026-09-26: a consult sat `pending` for 25+ minutes
with nothing in the advisor to say whether it was queued, in flight, or answered.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

import httpx
import pytest

from mori_advisor import bifrost_client as bc
from mori_advisor import metrics as mx

_COMPLETION = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 0,
    "model": "stub-model",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10},
    "extra_fields": {"provider": "fireworks"},
}


def _client_over(monkeypatch, handler) -> bc.BifrostClient:
    """A real BifrostClient whose HTTP goes to an in-process handler (the OpenAI client,
    its retry loop and our request hook all run for real)."""
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        bc, "DefaultHttpxClient", lambda **kw: httpx.Client(transport=transport, **kw)
    )
    return bc.BifrostClient(base_url="http://bifrost.test", timeout=5)


def _count(vk: str, outcome: str, provider: str) -> float:
    return (
        mx.prom_registry.get_sample_value(
            "mori_llm_call_duration_seconds_count",
            {"vk": vk, "outcome": outcome, "provider": provider},
        )
        or 0.0
    )


def test_retries_get_distinct_request_ids(monkeypatch):
    """Bifrost keeps ONE log row per x-request-id, so a retry must not reuse the first id."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["x-request-id"])
        if len(seen) == 1:
            return httpx.Response(500, json={"error": {"message": "upstream hiccup"}})
        return httpx.Response(200, json=_COMPLETION)

    client = _client_over(monkeypatch, handler)
    assert client.consult(system="s", user="u", vk="advisor") == "ok"

    assert len(seen) == 2
    assert seen[0].startswith("mori-advisor-")
    assert seen[1] == seen[0] + ".r1"


def test_success_logs_send_and_recv_and_observes_provider(monkeypatch, caplog):
    client = _client_over(monkeypatch, lambda r: httpx.Response(200, json=_COMPLETION))
    before = _count("advisor", "ok", "fireworks")

    with caplog.at_level(logging.INFO, logger="mori_advisor.bifrost_client"):
        client.consult(system="s", user="u", vk="advisor", ref="job-123")

    assert _count("advisor", "ok", "fireworks") == before + 1
    send = [r.getMessage() for r in caplog.records if r.getMessage().startswith("llm.send")]
    recv = [r.getMessage() for r in caplog.records if r.getMessage().startswith("llm.recv")]
    assert len(send) == 1 and len(recv) == 1
    call_id = send[0].split("call_id=")[1].split()[0]
    assert f"call_id={call_id}" in recv[0]
    assert "ref=job-123" in send[0] and "ref=job-123" in recv[0]
    assert "provider=fireworks" in recv[0] and "out_tokens=7" in recv[0]
    assert not mx._inflight, "a finished call must leave the in-flight registry"


def test_failure_is_reraised_unchanged_and_counted_as_error(monkeypatch, caplog):
    client = _client_over(
        monkeypatch, lambda r: httpx.Response(400, json={"error": {"message": "bad request"}})
    )
    before = _count("dream", "error", "unknown")

    import openai

    with caplog.at_level(logging.ERROR, logger="mori_advisor.bifrost_client"):
        with pytest.raises(openai.BadRequestError):
            client.consult(system="s", user="u", vk="dream")

    assert _count("dream", "error", "unknown") == before + 1
    assert any(
        "llm.fail" in r.getMessage() and "outcome=error" in r.getMessage() for r in caplog.records
    )
    assert not mx._inflight


def test_timeout_is_counted_as_timeout(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("provider never answered", request=request)

    client = _client_over(monkeypatch, handler)
    before = _count("fast", "timeout", "unknown")

    import openai

    with pytest.raises(openai.APITimeoutError):
        client.consult(system="s", user="u", vk="fast")

    assert _count("fast", "timeout", "unknown") == before + 1


class _StubStore:
    def __init__(self, state: dict | None = None) -> None:
        self.state = state or {}

    def get_dream_state(self, key, default=None):
        return self.state.get(key, default)


def _scrape(store=None) -> str:
    return asyncio.run(mx.collect_metrics(store or _StubStore())).decode()


def test_inflight_gauges_track_the_oldest_open_call():
    def sample(name, labels=None):
        return mx.prom_registry.get_sample_value(name, labels or {})

    mx.llm_call_started("probe-call", "advisor")
    try:
        time.sleep(0.05)
        _scrape()
        assert sample("mori_llm_inflight", {"vk": "advisor"}) == 1.0
        assert sample("mori_llm_oldest_inflight_seconds") >= 0.05
    finally:
        mx.llm_call_finished("probe-call", "advisor", "ok", "stub", 0.05)
    _scrape()
    assert sample("mori_llm_inflight", {"vk": "advisor"}) == 0.0
    assert sample("mori_llm_oldest_inflight_seconds") == 0.0


# ── consult slot queue (main._run_llm) ─────────────────────────────────────────


def _one_slot(monkeypatch, m, *, wait_s: float):
    monkeypatch.setattr(m, "LLM_SLOTS", 1)
    monkeypatch.setattr(m, "_llm_sem", asyncio.Semaphore(1))
    monkeypatch.setattr(m, "LLM_SLOT_WAIT_TIMEOUT", wait_s)


def test_run_llm_passes_ref_through_to_the_callee():
    from mori_advisor import main as m

    got: dict = {}

    def fn(**kwargs):
        got.update(kwargs)
        return "r"

    assert asyncio.run(m._run_llm(fn, ref="job-9", x=1)) == "r"
    assert got == {"ref": "job-9", "x": 1}


def test_slot_wait_is_bounded_with_an_explicit_error(monkeypatch):
    from mori_advisor import main as m

    _one_slot(monkeypatch, m, wait_s=0.2)
    release = threading.Event()

    async def scenario():
        holder = asyncio.create_task(m._run_llm(lambda **kw: release.wait(5)))
        await asyncio.sleep(0.05)
        with pytest.raises(
            m.LLMSlotUnavailable, match=r"no LLM slot free after 0.2s \(1 of 1 in use\)"
        ):
            await m._run_llm(lambda **kw: "never runs", ref="job-queued")
        assert m._llm_queued == 0, "a caller that gave up must leave the queue count"
        release.set()
        await holder

    asyncio.run(scenario())
    assert m._llm_active == 0


def test_call_timeout_releases_the_slot(monkeypatch):
    from mori_advisor import main as m

    _one_slot(monkeypatch, m, wait_s=1)
    monkeypatch.setattr(m, "LLM_CALL_TIMEOUT", 0.1)

    async def scenario():
        with pytest.raises(asyncio.TimeoutError, match="no response within 0.1s"):
            await m._run_llm(lambda **kw: time.sleep(0.4))
        # the slot must be free again even though the thread is still sleeping
        return await m._run_llm(lambda **kw: "next")

    assert asyncio.run(scenario()) == "next"
    assert m._llm_active == 0


def test_queued_consult_fails_visibly_instead_of_pending_forever(monkeypatch, caplog):
    """End to end: with the only slot held, a second consult reports an error naming the
    cause — it does not sit in `pending`."""
    from mori_advisor import main as m

    _one_slot(monkeypatch, m, wait_s=0.2)
    monkeypatch.setattr(m, "CONSULT_CAPTURE", False)
    release = threading.Event()
    monkeypatch.setattr(m.bifrost, "consult", lambda **kw: (release.wait(5), "answer")[1])

    async def poll(job_id: str) -> dict:
        for _ in range(200):
            status = json.loads(await m.consult_status(job_id))
            if status["status"] != "pending":
                return status
            await asyncio.sleep(0.02)
        raise AssertionError(f"{job_id} still pending")

    async def scenario():
        first = json.loads(await m.consult_advisor(question="q1"))["job_id"]
        await asyncio.sleep(0.05)
        second = json.loads(await m.consult_advisor(question="q2"))["job_id"]
        queued = await poll(second)
        release.set()
        done = await poll(first)
        return queued, done, second

    with caplog.at_level(logging.INFO, logger="mori_advisor.main"):
        queued, done, second = asyncio.run(scenario())

    assert queued["status"] == "error"
    assert "LLMSlotUnavailable: no LLM slot free" in queued["error"]
    assert done["status"] == "done"
    lines = [r.getMessage() for r in caplog.records]
    assert any(line.startswith(f"consult.created job_id={second}") for line in lines)
    assert any(line.startswith(f"llm.slot_queued ref={second}") for line in lines)
    assert any(line.startswith(f"consult.error job_id={second}") for line in lines)


# ── dream last-run summary ─────────────────────────────────────────────────────


def _last_run_writes(store) -> dict:
    return {
        c.args[0]: c.args[1]
        for c in store.set_dream_state.call_args_list
        if c.args and str(c.args[0]).startswith("last_run_")
    }


@pytest.mark.parametrize(("parse_returns", "outcome"), [([], "empty_batch"), (None, "parse_error")])
def test_dream_run_records_its_outcome(monkeypatch, parse_returns, outcome):
    from test_dream_watermark_advance import _pipeline_with_events

    pipeline, store = _pipeline_with_events(monkeypatch, parse_returns=parse_returns)
    asyncio.run(pipeline.run())

    summary = _last_run_writes(store)
    assert summary["last_run_outcome"] == outcome
    assert summary["last_run_written"] == "0"
    assert float(summary["last_run_duration_s"]) >= 0
    assert float(summary["last_run_finished_at"]) > 1.7e9


def test_dream_run_that_raises_still_records_error(monkeypatch):
    from test_dream_watermark_advance import _pipeline_with_events

    pipeline, store = _pipeline_with_events(monkeypatch, parse_returns=[])

    def boom(text):
        raise RuntimeError("model down")

    monkeypatch.setattr(pipeline, "_call_dream_model", boom)
    with pytest.raises(RuntimeError, match="model down"):
        asyncio.run(pipeline.run())
    assert _last_run_writes(store)["last_run_outcome"] == "error"


def test_dry_run_records_nothing(monkeypatch):
    from test_dream_watermark_advance import _pipeline_with_events

    pipeline, store = _pipeline_with_events(monkeypatch, parse_returns=[])
    asyncio.run(pipeline.run(dry_run=True))
    assert _last_run_writes(store) == {}


def test_undreamed_counts_events_after_the_watermark_not_rows_minus_id():
    """Pruning keeps the row count far below the max event id, so `count - watermark` read 0
    while events were waiting (UAT 2026-09-27: gauge said 0, the dream then wrote 3 memories)."""

    class _PrunedStore(_StubStore):
        def count_events(self):
            return 10  # rows left after pruning

        def count_events_since(self, since_event_id):
            return 5 if since_event_id == 100 else -1

    _scrape(_PrunedStore({"last_dreamed_event_id": "100"}))
    assert mx.prom_registry.get_sample_value("mori_dream_undreamed") == 5.0


def test_last_run_summary_is_exposed_on_metrics():
    text = _scrape(
        _StubStore(
            {
                "last_run_finished_at": "1790000000",
                "last_run_duration_s": "42.5",
                "last_run_written": "3",
                "last_run_outcome": "empty_batch",
            }
        )
    )
    assert "mori_dream_last_run_duration_seconds 42.5" in text
    assert "mori_dream_last_run_memories_written 3.0" in text
    assert 'mori_dream_last_run_outcome{outcome="empty_batch"} 1.0' in text
    assert 'mori_dream_last_run_outcome{outcome="ok"} 0.0' in text
