"""OpenTelemetry metrics for mori-advisor.

Instruments are created once at import time. Exporter is configured
via standard OTel env vars:
    OTEL_EXPORTER_OTLP_ENDPOINT    — e.g. https://otlp.grafana.net/otlp
    OTEL_EXPORTER_OTLP_HEADERS     — "Authorization=Basic <base64>"
    OTEL_SERVICE_NAME              — defaults to "mori-advisor"
    OTEL_METRIC_EXPORT_INTERVAL    — seconds between pushes (default 60)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Optional

from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
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


def init_metrics() -> None:
    """Initialise the meter provider and create instruments.

    Safe to call multiple times — only acts on first call.
    """
    global _meter, _provider, memories_gauge, events_counter
    global pending_writes_gauge, eviction_queue_gauge

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


# Phase 2 step 6: anatomy/completeness failures at store.write. Emitted only on a FAILED verdict
# (code != ok); `mode` disambiguates the action — audit = would-fail (logged, proceeds), enforce =
# downgraded-to-pending. The count over the soak sizes the MORI_ANATOMY_ENFORCE flip (empty-warrant
# is expected to dominate). `code` is bounded (empty-body|empty-warrant|unwarranted-directive).
_anatomy_decisions = Counter(
    "mori_anatomy_decisions_total",
    "Anatomy/completeness failures at store.write",
    ["actor", "code", "mode"],
    registry=prom_registry,
)


def record_anatomy_decision(actor: str, code: str, mode: str) -> None:
    """Record one FAILED anatomy verdict (empty-body | empty-warrant | unwarranted-directive).
    Fail-open — telemetry must NEVER break a write."""
    try:
        _anatomy_decisions.labels(actor=actor, code=code, mode=mode).inc()
    except Exception:
        logger.debug("record_anatomy_decision failed", exc_info=True)


# ── Outbound LLM calls ────────────────────────────────────────────────────
# Recorded by BifrostClient._send, the single funnel for every LLM call in this process.
# Labelled by `vk` (model profile) because that is all the client boundary knows for certain:
# dream AND ingest both use vk=dream. Per-feature dream timing is the dream last-run summary
# below. Buckets reach 900s = LLM_CALL_TIMEOUT so a timeout lands in a finite bucket.
_llm_call_seconds = Histogram(
    "mori_llm_call_duration_seconds",
    "Outbound LLM call duration by VK profile, outcome (ok|error|timeout) and serving provider",
    ["vk", "outcome", "provider"],
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 900),
    registry=prom_registry,
)
_llm_output_tokens = Counter(
    "mori_llm_output_tokens_total",
    "Completion tokens returned by outbound LLM calls",
    ["vk"],
    registry=prom_registry,
)
_llm_inflight = Gauge(
    "mori_llm_inflight",
    "Outbound LLM calls sent and still awaiting a response",
    ["vk"],
    registry=prom_registry,
)
_llm_oldest_inflight = Gauge(
    "mori_llm_oldest_inflight_seconds",
    "Age of the oldest outbound LLM call still awaiting a response (0 when none)",
    registry=prom_registry,
)
# Consult concurrency slots (main._run_llm). A wedge reads as queued > 0 with active at the cap.
_llm_slot_wait = Histogram(
    "mori_llm_slot_wait_seconds",
    "Time a consult waited for a free LLM slot",
    buckets=(0.1, 1, 5, 15, 30, 60, 120, 300),
    registry=prom_registry,
)
_llm_slots_active = Gauge(
    "mori_llm_slots_active", "Consult LLM slots in use", registry=prom_registry
)
_llm_slots_queued = Gauge(
    "mori_llm_slots_queued", "Consults waiting for a free LLM slot", registry=prom_registry
)

# JSONB array values read back on the Postgres write path that arrived NESTED in JSON strings
# (the pre-v2.3.10 double-encoding bug). After migration 16 the CHECK constraints make that state
# unrepresentable, so ANY increment means the encoding bug is back. Pre-initialised at zero so the
# first event after a restart is visible to increase().
JSONB_UNWRAP_COLUMNS = ("protected_domains", "origin_session_ids", "origin_clients")
_jsonb_unwrapped = Counter(
    "mori_jsonb_unwrapped_total",
    "JSONB array values found nested in JSON strings and unwrapped on the Postgres write path",
    ["column"],
    registry=prom_registry,
)
for _col in JSONB_UNWRAP_COLUMNS:
    _jsonb_unwrapped.labels(column=_col)


def record_jsonb_unwrapped(column: str) -> None:
    """Count one unwrap of a nested JSONB value. Fail-open."""
    try:
        _jsonb_unwrapped.labels(column=column).inc()
    except Exception:
        logger.debug("record_jsonb_unwrapped failed", exc_info=True)


# Owned sequences raised at boot because they would have handed out an id already in use (v2.3.11).
# The v2.3.11 deploy repairs eviction_queue once; any later repair means another out-of-band import
# or restore — alert on it. Pre-initialised for every table with an owned sequence (2026-10-03 prod).
SEQUENCE_TABLES = (
    "delegate_tasks",
    "eviction_queue",
    "ingestion_log",
    "memories",
    "memory_versions",
    "pending_writes",
    "session_events",
    "write_audit",
)
_sequence_repairs = Counter(
    "mori_sequence_repairs_total",
    "Owned sequences raised at boot because their next value was <= max(id)",
    ["table"],
    registry=prom_registry,
)
for _tbl in SEQUENCE_TABLES:
    _sequence_repairs.labels(table=_tbl)


SUPERSESSION_MODES = ("report_only", "write")
SUPERSESSION_RESULTS = ("applied", "proposed", "failed", "already_decided")
_supersessions = Counter(
    "mori_supersessions_total",
    "Contradiction-scan outcomes per supersession mode: applied (superseded_by written), proposed "
    "(queued for review), failed (write/insert failed), already_decided (pair skipped unclassified)",
    ["mode", "result"],
    registry=prom_registry,
)
for _mode in SUPERSESSION_MODES:
    for _res in SUPERSESSION_RESULTS:
        _supersessions.labels(mode=_mode, result=_res)

# The scheduled dream runs in a one-off process whose counters die on exit; its per-run outcome is
# persisted in dream_state and exported here at scrape time (board B-C4).
_dream_last_supersessions = Gauge(
    "mori_dream_last_run_supersessions",
    "Supersession outcomes of the most recent dream run (from dream_state; covers cron runs)",
    ["result"],
    registry=prom_registry,
)
for _res in SUPERSESSION_RESULTS:
    _dream_last_supersessions.labels(result=_res)


def record_supersession(mode: str, result: str) -> None:
    """Count one contradiction-scan outcome. Fail-open."""
    try:
        _supersessions.labels(mode=mode, result=result).inc()
    except Exception:
        logger.debug("record_supersession failed", exc_info=True)


def set_dream_last_supersessions(counts: dict[str, int]) -> None:
    """Export the last dream run's persisted supersession outcomes. Fail-open."""
    try:
        for res in SUPERSESSION_RESULTS:
            _dream_last_supersessions.labels(result=res).set(int(counts.get(res, 0)))
    except Exception:
        logger.debug("set_dream_last_supersessions failed", exc_info=True)


# The time of the most recent repair, from dream_state — process-independent, so a repair in ANY
# process (server, ingestion, cron dream) and a second repair on a later boot are both visible.
# 0 = never repaired. Alert: time() - this < 3600.
_sequence_last_repair = Gauge(
    "mori_sequence_last_repair_timestamp_seconds",
    "Unix time of the most recent boot-time sequence repair (any process; 0 = never)",
    registry=prom_registry,
)
_sequence_last_repair.set(0)


def record_sequence_repair(table: str) -> None:
    """Count one boot-time sequence repair. Fail-open."""
    try:
        _sequence_repairs.labels(table=table).inc()
    except Exception:
        logger.debug("record_sequence_repair failed", exc_info=True)


# finish_reason == "length": the reply hit max_tokens. Routine for some advisor/dream calls, so
# this is a dashboard signal, not an alert — the classifier counter below carries the alert.
_llm_truncated = Counter(
    "mori_llm_truncated_total",
    "Outbound LLM calls that stopped at max_tokens (finish_reason=length)",
    ["vk"],
    registry=prom_registry,
)

# ── One-word classifiers (freshness / contradiction scan / intake assessor) ──
# `outcome` is the parsed verdict (lower-case), or `unparseable` (a reply that is not exactly one
# allowed token — e.g. empty content from a reasoning model that spent its budget thinking), or
# `error` (the call or the scan raised). A healthy site shows verdicts; a broken one shows
# unparseable/error. Bounded: 3 sites x <= 5 outcomes each.
_classifier_verdicts = Counter(
    "mori_classifier_verdicts_total",
    "One-word classifier results by site (freshness|contradiction|intake) and outcome",
    ["site", "outcome"],
    registry=prom_registry,
)

# Every series the alert reads exists at zero from import. A labelled counter series is otherwise
# created on its first increment, and `increase()` cannot see a jump from absent to N — so the
# first burst of failures after a restart (the 2026-09-25 case) would be invisible to the alert.
CLASSIFIER_OUTCOMES: dict[str, tuple[str, ...]] = {
    "freshness": ("yes", "no", "stale", "unparseable", "error"),
    "contradiction": ("supersedes", "related", "unrelated", "unparseable", "error"),
    "intake": ("supersedes", "related", "unrelated", "unparseable", "error"),
}
for _site, _outcomes in CLASSIFIER_OUTCOMES.items():
    for _outcome in _outcomes:
        _classifier_verdicts.labels(site=_site, outcome=_outcome)
for _vk in ("advisor", "dream", "fast"):
    _llm_truncated.labels(vk=_vk)

_inflight_lock = threading.Lock()
_inflight: dict[str, tuple[str, float]] = {}  # call_id -> (vk, monotonic start)
_inflight_vks_seen: set[str] = {"advisor", "dream", "fast"}


def llm_call_started(call_id: str, vk: str) -> None:
    """Register an outbound call as in flight. Fail-open; called from executor threads."""
    try:
        with _inflight_lock:
            _inflight[call_id] = (vk, time.monotonic())
            _inflight_vks_seen.add(vk)
    except Exception:
        logger.debug("llm_call_started failed", exc_info=True)


def llm_call_finished(
    call_id: str,
    vk: str,
    outcome: str,
    provider: str,
    elapsed_s: float,
    output_tokens: Optional[int] = None,
) -> None:
    """Retire an in-flight call and observe its duration. Fail-open."""
    try:
        with _inflight_lock:
            _inflight.pop(call_id, None)
        _llm_call_seconds.labels(vk=vk, outcome=outcome, provider=provider).observe(elapsed_s)
        if output_tokens:
            _llm_output_tokens.labels(vk=vk).inc(output_tokens)
    except Exception:
        logger.debug("llm_call_finished failed", exc_info=True)


def llm_call_truncated(vk: str) -> None:
    """Count a call whose reply stopped at max_tokens. Fail-open."""
    try:
        _llm_truncated.labels(vk=vk).inc()
    except Exception:
        logger.debug("llm_call_truncated failed", exc_info=True)


def record_classifier_verdict(site: str, outcome: str) -> None:
    """Count one classifier result (verdict | unparseable | error). Fail-open."""
    try:
        _classifier_verdicts.labels(site=site, outcome=outcome).inc()
    except Exception:
        logger.debug("record_classifier_verdict failed", exc_info=True)


def llm_slot_state(active: int, queued: int) -> None:
    try:
        _llm_slots_active.set(active)
        _llm_slots_queued.set(queued)
    except Exception:
        logger.debug("llm_slot_state failed", exc_info=True)


def llm_slot_waited(seconds: float) -> None:
    try:
        _llm_slot_wait.observe(seconds)
    except Exception:
        logger.debug("llm_slot_waited failed", exc_info=True)


def _refresh_inflight() -> None:
    """Recompute the in-flight gauges from the live registry (so oldest-age grows between events)."""
    now = time.monotonic()
    with _inflight_lock:
        entries = list(_inflight.values())
        seen = set(_inflight_vks_seen)
    counts: dict[str, int] = {}
    for vk, _ in entries:
        counts[vk] = counts.get(vk, 0) + 1
    for vk in seen:
        _llm_inflight.labels(vk=vk).set(counts.get(vk, 0))
    _llm_oldest_inflight.set(max((now - started for _, started in entries), default=0.0))


# ── Dream last run ────────────────────────────────────────────────────────
# DreamPipeline.run() persists a summary in dream_state because the scheduled dream runs in a
# SEPARATE process (`python -m mori_advisor.dream_job`); read at scrape time so both scheduled
# and on-demand runs appear. avg_over_time() of the duration gauge ≈ per-run mean when runs
# are evenly spaced (each value is held until the next run).
DREAM_OUTCOMES = ("ok", "no_events", "empty_batch", "parse_error", "error")
_dream_last_duration = Gauge(
    "mori_dream_last_run_duration_seconds",
    "Wall time of the last dream run (scheduled or on-demand)",
    registry=prom_registry,
)
_dream_last_finished = Gauge(
    "mori_dream_last_run_timestamp_seconds",
    "Unix time the last dream run finished",
    registry=prom_registry,
)
_dream_last_written = Gauge(
    "mori_dream_last_run_memories_written",
    "Memories written by the last dream run",
    registry=prom_registry,
)
_dream_last_outcome = Gauge(
    "mori_dream_last_run_outcome",
    "1 on the outcome label of the last dream run, 0 on the others",
    ["outcome"],
    registry=prom_registry,
)


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


async def _a(val: Any) -> Any:
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
        _dream_undreamed.set(await _a(store.count_events_since(watermark)))
    except Exception:
        pass

    # Dream last run — only set once a run has recorded one (absent ≠ zero).
    try:
        finished = await _a(store.get_dream_state("last_run_finished_at"))
        if finished:
            _dream_last_finished.set(float(finished))
            _dream_last_duration.set(
                float(await _a(store.get_dream_state("last_run_duration_s")) or 0)
            )
            _dream_last_written.set(int(await _a(store.get_dream_state("last_run_written")) or 0))
            last_outcome = await _a(store.get_dream_state("last_run_outcome"))
            for outcome in DREAM_OUTCOMES:
                _dream_last_outcome.labels(outcome=outcome).set(1 if outcome == last_outcome else 0)
            sup = await _a(store.get_dream_state("last_run_supersessions"))
            if sup:
                set_dream_last_supersessions(json.loads(sup))
    except Exception:
        pass

    # Last boot-time sequence repair (persisted by whichever process repaired). A failed read must
    # NOT reset the gauge to 0 ("never repaired") — that would silence MoriSequenceRepaired. Keep the
    # last known value and say so. An absent key genuinely means never repaired.
    try:
        repaired_at = await _a(store.get_dream_state("last_sequence_repair_at"))
    except Exception:
        logger.warning(
            "metrics: could not read last_sequence_repair_at; keeping the last known value",
            exc_info=True,
        )
    else:
        _sequence_last_repair.set(float(repaired_at) if repaired_at else 0)

    # Outbound LLM calls in flight (process-local; recomputed so oldest-age is current).
    try:
        _refresh_inflight()
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
