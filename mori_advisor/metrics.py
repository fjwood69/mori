"""OpenTelemetry metrics for mori-advisor.

Instruments are created once at import time. Exporter is configured
via standard OTel env vars:
    OTEL_EXPORTER_OTLP_ENDPOINT    — e.g. https://otlp.grafana.net/otlp
    OTEL_EXPORTER_OTLP_HEADERS     — "Authorization=Basic <base64>"
    OTEL_SERVICE_NAME              — defaults to "mori-advisor"
    OTEL_METRIC_EXPORT_INTERVAL    — seconds between pushes (default 60)
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Info,
    generate_latest,
)

logger = logging.getLogger(__name__)

_service_name = os.environ.get("OTEL_SERVICE_NAME", "mori-advisor")
_resource = Resource.create({"service.name": _service_name})

# ── Instruments ──────────────────────────────────────────────────────────

_meter: metrics.Meter | None = None
_provider: MeterProvider | None = None

# Gauges — set via .set(value)
memories_gauge: metrics.Gauge | None = None
events_counter: metrics.Counter | None = None
pending_writes_gauge: metrics.Gauge | None = None
eviction_queue_gauge: metrics.Gauge | None = None

# Histograms — record duration/tokens
consult_duration: metrics.Histogram | None = None
dream_duration: metrics.Histogram | None = None
consult_tokens: metrics.Histogram | None = None


def init_metrics() -> None:
    """Initialise the meter provider and create instruments.

    Safe to call multiple times — only acts on first call.
    """
    global _meter, _provider, memories_gauge, events_counter
    global pending_writes_gauge, eviction_queue_gauge
    global consult_duration, dream_duration, consult_tokens

    if _meter is not None:
        return  # already initialised

    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                OTLPMetricExporter,
            )

            interval_ms = int(os.environ.get("OTEL_METRIC_EXPORT_INTERVAL", "60")) * 1000
            exporter = OTLPMetricExporter(endpoint=otlp_endpoint)
            reader = PeriodicExportingMetricReader(exporter, export_interval_millis=interval_ms)
            logger.info("OTLP exporter configured for %s", otlp_endpoint)
        except Exception as e:
            logger.warning("Failed to configure OTLP exporter: %s", e)
            reader = None
    else:
        logger.info("No OTEL_EXPORTER_OTLP_ENDPOINT set — metrics in memory only")
        reader = None

    readers = [reader] if reader else []
    _provider = MeterProvider(resource=_resource, metric_readers=readers)
    metrics.set_meter_provider(_provider)
    _meter = metrics.get_meter(_service_name, version="0.1.0")

    memories_gauge = _meter.create_gauge(
        name="mori_memories_total",
        description="Total number of memories in the store",
        unit="1",
    )
    events_counter = _meter.create_counter(
        name="mori_events_total",
        description="Total number of session events logged",
        unit="1",
    )
    pending_writes_gauge = _meter.create_gauge(
        name="mori_pending_writes",
        description="Number of pending writes awaiting approval",
        unit="1",
    )
    eviction_queue_gauge = _meter.create_gauge(
        name="mori_eviction_queue_size",
        description="Number of unresolved eviction queue entries",
        unit="1",
    )
    consult_duration = _meter.create_histogram(
        name="mori_consult_duration_ms",
        description="Consult call duration in milliseconds",
        unit="ms",
    )
    dream_duration = _meter.create_histogram(
        name="mori_dream_duration_ms",
        description="Dream pipeline run duration in milliseconds",
        unit="ms",
    )
    consult_tokens = _meter.create_histogram(
        name="mori_consult_tokens",
        description="Tokens used per consult call",
        unit="1",
    )

    logger.info("OTel metrics initialised (service=%s)", _service_name)


def shutdown_metrics() -> None:
    """Shut down the meter provider (flush + close)."""
    if _provider is not None:
        _provider.shutdown()


# ── Prometheus client exposition ──────────────────────────────────────────


# Custom registry to avoid cluttering or conflicting with the global registry
prom_registry = CollectorRegistry()

# Define Prometheus metrics
_info = Info("mori", "Mori advisor info", registry=prom_registry)
_memories_total = Gauge(
    "mori_memories_total", "Total memories by tier", ["tier"], registry=prom_registry
)
_memories_protected = Gauge("mori_memories_protected", "Protected memories", registry=prom_registry)
_events_total = Gauge("mori_events_total", "Total session events", registry=prom_registry)
_dream_watermark = Gauge(
    "mori_dream_watermark",
    "Current dream watermark (last dreamed event ID)",
    registry=prom_registry,
)
_dream_undreamed = Gauge("mori_dream_undreamed", "Events not yet dreamed", registry=prom_registry)
_pending_writes = Gauge(
    "mori_pending_writes_total", "Pending writes by status", ["status"], registry=prom_registry
)
_eviction_queue = Gauge("mori_eviction_queue_total", "Eviction queue depth", registry=prom_registry)
_msg_pending = Gauge(
    "mori_msg_pending_total", "Pending inter-agent messages", registry=prom_registry
)
_nats_connected = Gauge(
    "mori_nats_connected", "NATS connectivity (1=connected, 0=not)", registry=prom_registry
)
_ingestion_log = Gauge(
    "mori_ingestion_log_total", "Total ingestion log entries", registry=prom_registry
)
# Ingest-shape instrument (measurement layer) — last committed ingest. The journal
# (ingestion_log rows) is the real artifact; these gauges are the convenience surface.
_ingest_last_candidates = Gauge(
    "mori_ingest_last_candidates",
    "Candidates produced by the last committed ingest",
    registry=prom_registry,
)
_ingest_last_convention_ratio = Gauge(
    "mori_ingest_last_convention_ratio",
    "Share of last ingest's candidates that clustered with another (granularity signal)",
    registry=prom_registry,
)
_ingest_last_anchorable_pct = Gauge(
    "mori_ingest_last_anchorable_pct",
    "Share of last ingest's candidates with a file/symbol reference (smoke signal)",
    registry=prom_registry,
)
_canon_mortality = Gauge(
    "mori_canon_mortality_rate_90d",
    "Share of canonical memories created >90d ago never retrieved (cohort mortality)",
    registry=prom_registry,
)
# TD decision instrument (measurement layer b) — fixed label set (no cardinality risk).
_td_reason = Gauge(
    "mori_td_reason_total",
    "TD approve/reject decisions by reason",
    ["reason"],
    registry=prom_registry,
)
_td_reason_coverage = Gauge(
    "mori_td_reason_coverage",
    "Share of TD approve/reject decisions carrying a reason code",
    registry=prom_registry,
)
# Net canon growth (measurement layer d) — over-production signal.
_net_canon_growth = Gauge(
    "mori_net_canon_growth_7d",
    "Approvals - rejections - deletions over the last 7 days",
    registry=prom_registry,
)
_scrape_duration = Gauge(
    "mori_scrape_duration_seconds", "Time taken to collect metrics", registry=prom_registry
)

# ── Brief delivery telemetry — delivery-verification coverage ──
# Answers "did the injected memory actually reach the agent?" — the prerequisite for trusting advisory memory
# in production, where nothing stream-confirms each injection. Two CHANNELS with different delivery guarantees:
#   - channel="mcp_tool": the agent CALLS the brief tool; the return value lands in its transcript by
#     construction → delivery is CONFIRMED.
#   - channel="hook": a fire-and-forget SessionStart hook emits additionalContext → delivery is UNCONFIRMED
#     (a silently dropped injection is indistinguishable from one the model simply ignored).
# Coverage = confirmed / served. NOTE (denominator growth): only the mcp_tool channel is instrumented in this
# increment, so coverage reads ~1.0 today — correct, and the SHAPE is ready for the hook channel, whose
# attempts grow the denominator and reveal the unverified fraction. ~1.0 here is NOT "done".
_brief_served = Counter(
    "mori_brief_served_total",
    "Briefs served, by channel and scope",
    ["channel", "scope"],
    registry=prom_registry,
)
_brief_confirmed = Counter(
    "mori_brief_delivery_confirmed_total",
    "Briefs whose delivery into the agent transcript is confirmed",
    ["channel"],
    registry=prom_registry,
)
_brief_coverage = Gauge(
    "mori_brief_delivery_coverage",
    "Confirmed-delivery share of briefs served (production delivery-verification coverage)",
    registry=prom_registry,
)
# Phase 2 step 3: tier-authorization decisions at store.write. The `would_block` count over
# the audit-mode soak SIZES the enforce flip (GLM#7 exit criteria). Labels are bounded —
# source/op/reason live in the structured log (joined via actor+name), not as labels.
_tier_decisions = Counter(
    "mori_tier_decisions_total",
    "Tier-authorization decisions at store.write",
    ["actor", "intended_tier", "decision", "mode"],
    registry=prom_registry,
)


def record_tier_decision(actor: str, intended_tier: str, decision: str, mode: str) -> None:
    """Record one tier-authorization decision (allowed | would_block | rejected). Fail-open —
    telemetry must NEVER break a write (the point is to make the decision observable, not fragile)."""
    try:
        _tier_decisions.labels(
            actor=actor, intended_tier=intended_tier, decision=decision, mode=mode
        ).inc()
    except Exception:
        logger.debug("record_tier_decision failed", exc_info=True)


# Process-level accumulators for the coverage ratio (prom Counter internals aren't cleanly readable).
_brief_counts = {"served": 0, "confirmed": 0}


def record_brief_injection(channel: str, scope: str = "n/a", confirmed: bool = False) -> None:
    """Record one brief injection attempt + whether its delivery is CONFIRMED. Fail-open: telemetry must
    NEVER break a brief (the whole point is to make the brief observable, not fragile)."""
    try:
        _brief_served.labels(channel=channel, scope=scope).inc()
        _brief_counts["served"] += 1
        if confirmed:
            _brief_confirmed.labels(channel=channel).inc()
            _brief_counts["confirmed"] += 1
    except Exception:
        pass


def brief_delivery_coverage() -> Optional[float]:
    """confirmed / served over the process lifetime, or None if nothing has been served yet."""
    s = _brief_counts["served"]
    return round(_brief_counts["confirmed"] / s, 4) if s else None


def reset_brief_counts() -> None:
    """Test helper — zero the process accumulators (the prom Counters are append-only and not reset)."""
    _brief_counts["served"] = 0
    _brief_counts["confirmed"] = 0


async def _a(val):
    """Await val if it's a coroutine, else return as-is."""
    import inspect

    if inspect.isawaitable(val):
        return await val
    return val


async def collect_metrics(store, nats_url: Optional[str] = None) -> bytes:
    """Collect all metrics and return Prometheus exposition format bytes."""
    t0 = time.monotonic()
    events_val = 0

    # Info
    version = os.environ.get("MORI_VERSION", "unknown")
    backend = "postgres" if "postgresql" in os.environ.get("MORI_DATABASE_URL", "") else "sqlite"
    _info.info({"version": version, "backend": backend})

    # Memory counts by tier
    try:
        for tier in ("canonical", "working", "ephemeral"):
            count = await _a(store.count(tier=tier))
            _memories_total.labels(tier=tier).set(count)
        protected = await _a(store.count(protected=True))
        _memories_protected.set(protected)
    except Exception:
        pass

    # Events
    try:
        events_val = await _a(store.count_events())
        _events_total.set(events_val)
    except Exception:
        pass

    # Dream state
    try:
        watermark_raw = await _a(store.get_dream_state("last_dreamed_event_id"))
        watermark = int(watermark_raw or 0)
        _dream_watermark.set(watermark)
        _dream_undreamed.set(max(0, events_val - watermark))
    except Exception:
        pass

    # Pending writes
    try:
        for status in ("pending", "approved", "rejected"):
            count = await _a(store.pending_count(status=status))
            _pending_writes.labels(status=status).set(count)
    except Exception:
        pass

    # Eviction queue
    try:
        evictions = await _a(store.eviction_count())
        _eviction_queue.set(evictions)
    except Exception:
        pass

    # Msg pending
    try:
        msgs = await _a(store.count_messages(status="pending"))
        _msg_pending.set(msgs)
    except Exception:
        pass

    # NATS connectivity — must never hang /metrics. nats.connect() retries a refused
    # server (reconnect loop) even with connect_timeout, so disable reconnect AND wrap
    # in a hard wait_for; a down/unreachable NATS just reports 0.
    try:
        if nats_url:
            import asyncio

            import nats

            nc = await asyncio.wait_for(
                nats.connect(
                    nats_url,
                    connect_timeout=2,
                    allow_reconnect=False,
                    max_reconnect_attempts=0,
                ),
                timeout=3,
            )
            await nc.drain()
            _nats_connected.set(1)
        else:
            _nats_connected.set(0)
    except Exception:
        _nats_connected.set(0)

    # Ingestion log
    try:
        ingestions = await _a(store.count_ingestion())
        _ingestion_log.set(ingestions)
    except Exception:
        pass

    # Ingest-shape (last committed ingest)
    try:
        shape = await _a(store.latest_ingestion_shape())
        if shape:
            if shape.get("candidates_total") is not None:
                _ingest_last_candidates.set(shape["candidates_total"])
            if shape.get("convention_ratio") is not None:
                _ingest_last_convention_ratio.set(shape["convention_ratio"])
            if shape.get("anchorable_pct") is not None:
                _ingest_last_anchorable_pct.set(shape["anchorable_pct"])
    except Exception:
        pass

    # Canon mortality (cohort rate — measurement layer)
    try:
        rate = await _a(store.canon_mortality_rate(days=90))
        if rate is not None:
            _canon_mortality.set(rate)
    except Exception:
        pass

    # TD decisions + net canon growth (measurement layer b+d)
    try:
        g = await _a(store.audit_governance_stats(days=7))
        if g:
            dist = g.get("td_reason", {})
            for reason in ("too-granular", "duplicate", "stale", "low-value", "other"):
                _td_reason.labels(reason=reason).set(dist.get(reason, 0))
            total = g.get("td_total", 0)
            _td_reason_coverage.set(round(g.get("td_reasoned", 0) / total, 3) if total else 0.0)
            _net_canon_growth.set(g.get("net_canon_growth", 0))
    except Exception:
        pass

    # Brief delivery coverage — process-level, set from the accumulators.
    try:
        cov = brief_delivery_coverage()
        if cov is not None:
            _brief_coverage.set(cov)
    except Exception:
        pass

    _scrape_duration.set(time.monotonic() - t0)

    return generate_latest(prom_registry)


def metrics_content_type() -> str:
    return CONTENT_TYPE_LATEST
