"""Regression: consult_advisor must not block the event loop on its (blocking) LLM call.

bifrost.consult() is the *synchronous* OpenAI client. Called inline in an async
handler it froze the single-worker server. consult_advisor now returns a job_id
immediately and runs the LLM in a background task (off-loop via llm_executor).
"""

import asyncio
import json
import time


def test_consult_advisor_does_not_block_event_loop(monkeypatch):
    from mori_advisor import main as m

    _conformant = (
        "P1: Use a thread pool [ASSUMED].\n\n"
        "## COULD NOT VERIFY\nAll premises verified from attached context."
    )

    def slow_consult(**kwargs):
        time.sleep(0.4)
        return _conformant

    monkeypatch.setattr(m.bifrost, "consult", slow_consult)
    monkeypatch.setattr(m, "CONSULT_CAPTURE", False)

    async def scenario():
        ticks: list[float] = []

        async def ticker():
            for _ in range(8):
                ticks.append(time.monotonic())
                await asyncio.sleep(0.05)

        submit = json.loads(await m.consult_advisor(question="hi", focus="general"))
        job_id = submit["job_id"]
        tick_task = asyncio.create_task(ticker())
        # Poll until done while ticker runs
        result = None
        for _ in range(40):
            st = json.loads(await m.consult_status(job_id))
            if st["status"] == "done":
                result = st["result"]
                break
            await asyncio.sleep(0.05)
        await tick_task
        return result, ticks

    result, ticks = asyncio.run(scenario())

    assert result == _conformant
    assert len(ticks) >= 5, f"event loop was blocked during consult (only {len(ticks)} ticks)"


def test_run_llm_offloads_and_returns(monkeypatch):
    """_run_llm runs the blocking fn off the loop and returns its result."""
    from mori_advisor import main as m

    def blocking(**kwargs):
        time.sleep(0.1)
        return kwargs["x"] * 2

    out = asyncio.run(m._run_llm(blocking, x=21))
    assert out == 42
