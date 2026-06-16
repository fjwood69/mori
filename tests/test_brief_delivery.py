"""Brief delivery telemetry — delivery-verification coverage.

Two layers:
  - PURE unit tests for the metrics accumulator API (served/confirmed/coverage, fail-open).
  - An INTEGRATION test that calls the real `brief()` MCP tool against a seeded SQLite store and asserts the
    coverage gauge appears in the /metrics exposition and reads as confirmed-delivery (the mcp_tool channel's
    return value lands in the transcript by construction).
"""

import asyncio

import pytest

from mori_advisor import metrics
from mori_advisor.memory_store import MemoryStore
from mori_advisor.store.migrations import MIGRATIONS, apply_sqlite


@pytest.fixture(autouse=True)
def _reset_counts():
    metrics.reset_brief_counts()
    yield
    metrics.reset_brief_counts()


# ───────────────────────── pure accumulator API ─────────────────────────
def test_coverage_none_before_any_brief():
    assert metrics.brief_delivery_coverage() is None


def test_confirmed_delivery_is_full_coverage():
    metrics.record_brief_injection(channel="mcp_tool", scope="project", confirmed=True)
    metrics.record_brief_injection(channel="mcp_tool", scope="unscoped", confirmed=True)
    assert metrics.brief_delivery_coverage() == 1.0


def test_unconfirmed_channel_drops_coverage():
    # one confirmed (mcp_tool), one unconfirmed (hook) → coverage = 1/2
    metrics.record_brief_injection(channel="mcp_tool", scope="project", confirmed=True)
    metrics.record_brief_injection(channel="hook", scope="startup", confirmed=False)
    assert metrics.brief_delivery_coverage() == 0.5


def test_record_is_fail_open_on_bad_label(monkeypatch):
    # if the prom layer raises, the accumulator must not propagate (telemetry never breaks a brief)
    def boom(*a, **k):
        raise RuntimeError("prom down")

    monkeypatch.setattr(metrics._brief_served, "labels", boom)
    metrics.record_brief_injection(channel="mcp_tool", confirmed=True)  # must not raise
    # the served accumulator was never bumped because labels() failed first
    assert metrics.brief_delivery_coverage() is None


# ───────────────────────── integration: the real brief() tool ─────────────────────────
def test_brief_tool_records_confirmed_delivery(tmp_path, monkeypatch):
    from mori_advisor import main as m

    db = tmp_path / "memories.db"
    MemoryStore.bootstrap_schema(db)
    apply_sqlite(db, tuple(mig for mig in MIGRATIONS if mig.target == "memories"))
    st = MemoryStore(db)
    st.write(
        name="demo-fact",
        title="A demo project fact",
        body="b",
        type="project",
        tier="working",
        tags=["project:demo"],
    )

    monkeypatch.setattr(m, "store", st)
    monkeypatch.setattr(m, "memory_store", st)
    monkeypatch.setattr(m, "FRESHNESS_ON_BRIEF", False)  # no LLM call in the test

    async def run():
        out = await m.brief(project="demo")
        assert isinstance(out, str) and out.strip()
        # the brief was recorded as confirmed-delivered on the mcp_tool channel
        assert metrics.brief_delivery_coverage() == 1.0
        # and it surfaces in the Prometheus exposition
        body = (await metrics.collect_metrics(st)).decode()
        assert "mori_brief_delivery_coverage" in body
        assert "mori_brief_served_total" in body

    asyncio.run(run())
