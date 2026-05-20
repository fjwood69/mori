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

from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

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

            interval_ms = int(
                os.environ.get("OTEL_METRIC_EXPORT_INTERVAL", "60")
            ) * 1000
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