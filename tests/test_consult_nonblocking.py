"""Regression: consult_advisor must not block the event loop on its (blocking) LLM call.

bifrost.consult() is the *synchronous* OpenAI client. Called inline in the async
consult_advisor handler it froze the single-worker server — every session, every tool —
for the whole 30-90s generation, so a concurrent /dream or /consult made mori-advisor
"unavailable" and the MCP connection dropped. consult_advisor now offloads the blocking
call off the loop (dedicated executor + semaphore), so other coroutines keep progressing.
"""

import asyncio
import time


def test_consult_advisor_does_not_block_event_loop(monkeypatch):
    from mori_advisor import main as m

    # A blocking consult that sleeps in its worker thread (simulates a slow LLM call).
    def slow_consult(**kwargs):
        time.sleep(0.4)
        return "advice"

    monkeypatch.setattr(m.bifrost, "consult", slow_consult)
    monkeypatch.setattr(m, "CONSULT_CAPTURE", False)

    async def scenario():
        ticks: list[float] = []

        async def ticker():
            # If the loop were blocked by the sync consult, this could not tick during it.
            for _ in range(8):
                ticks.append(time.monotonic())
                await asyncio.sleep(0.05)

        consult_task = asyncio.create_task(m.consult_advisor(question="hi", focus="general"))
        tick_task = asyncio.create_task(ticker())
        result = await consult_task
        await tick_task
        return result, ticks

    result, ticks = asyncio.run(scenario())

    assert result == "advice"
    # The ticker must have ticked through the ~0.4s consult → the loop stayed responsive.
    assert len(ticks) >= 5, f"event loop was blocked during consult (only {len(ticks)} ticks)"


def test_run_llm_offloads_and_returns(monkeypatch):
    """_run_llm runs the blocking fn off the loop and returns its result."""
    from mori_advisor import main as m

    def blocking(**kwargs):
        time.sleep(0.1)
        return kwargs["x"] * 2

    out = asyncio.run(m._run_llm(blocking, x=21))
    assert out == 42
