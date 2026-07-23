"""Async consult: consult_advisor returns job_id; consult_status polls to done."""

from __future__ import annotations

import asyncio
import json
import time

_CONFORMANT = (
    "P1: Use a thread pool [ASSUMED].\n\n"
    "## COULD NOT VERIFY\nAll premises verified from attached context."
)


async def _await_advice(m, **kwargs) -> str:
    """Submit consult and poll until done/error."""
    submit = json.loads(await m.consult_advisor(**kwargs))
    assert submit["status"] == "pending"
    job_id = submit["job_id"]
    for _ in range(200):
        status = json.loads(await m.consult_status(job_id))
        if status["status"] == "done":
            return status["result"]
        if status["status"] == "error":
            raise AssertionError(status.get("error"))
        await asyncio.sleep(0.02)
    raise AssertionError(f"consult job {job_id} did not complete")


def test_consult_advisor_returns_job_id_immediately(monkeypatch):
    from mori_advisor import main as m

    def slow_consult(**kwargs):
        time.sleep(0.3)
        return _CONFORMANT

    monkeypatch.setattr(m.bifrost, "consult", slow_consult)
    monkeypatch.setattr(m, "CONSULT_CAPTURE", False)

    async def scenario():
        t0 = time.monotonic()
        raw = await m.consult_advisor(question="hi", focus="general")
        elapsed = time.monotonic() - t0
        data = json.loads(raw)
        assert data["status"] == "pending"
        assert "job_id" in data
        # Must return well before the 0.3s LLM finishes
        assert elapsed < 0.15, f"consult_advisor blocked for {elapsed:.3f}s"
        advice = None
        for _ in range(50):
            st = json.loads(await m.consult_status(data["job_id"]))
            if st["status"] == "done":
                advice = st["result"]
                break
            await asyncio.sleep(0.05)
        assert advice == _CONFORMANT

    asyncio.run(scenario())


def test_consult_status_unknown_job():
    from mori_advisor import main as m

    raw = asyncio.run(m.consult_status("does-not-exist"))
    data = json.loads(raw)
    assert data["status"] == "error"
    assert "not found" in data["error"].lower()


def test_consult_file_contents_reach_prompt(monkeypatch):
    from mori_advisor import main as m

    seen: dict = {}

    def capture_consult(**kwargs):
        seen["user"] = kwargs.get("user", "")
        return _CONFORMANT

    monkeypatch.setattr(m.bifrost, "consult", capture_consult)
    monkeypatch.setattr(m, "CONSULT_CAPTURE", False)

    advice = asyncio.run(
        _await_advice(
            m,
            question="review",
            file_contents=[{"name": "widget.py", "content": "WIDGET_MARKER = 42\n"}],
            focus="general",
            depth="quick",
        )
    )
    assert advice == _CONFORMANT
    assert "WIDGET_MARKER" in seen["user"]
    assert "widget.py" in seen["user"]


def test_consult_file_contents_truncation(monkeypatch):
    from mori_advisor import main as m

    blocks, errors, total = m._blocks_from_file_contents(
        [{"name": "big.py", "content": "x" * (m.MAX_FILE_SIZE + 1000)}]
    )
    assert blocks
    assert total <= m.MAX_FILE_SIZE + len("\n... (truncated)")
    assert "truncated" in blocks[0]
