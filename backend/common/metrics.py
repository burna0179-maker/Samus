"""Prometheus instruments per doc §3.15.

Three core HTTP/task instruments + five existing LLM token-budget
instruments + four LLM cost-hardening instruments (token-cost-hardening
2026-05-18: circuit-trip counter, cache hits/creations, dollar
used/cap gauges). ``metrics_response()`` exposes the registry in
Prometheus text format.

All instruments live here so callers never instantiate
``prometheus_client.Counter`` directly (per the brief's hard rule).
"""
from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.responses import Response


REGISTRY = CollectorRegistry(auto_describe=True)


# ---------------------------------------------------------------------------
# Core HTTP / task instruments
# ---------------------------------------------------------------------------

SAMUS_HTTP_REQUESTS_TOTAL = Counter(
    "samus_http_requests_total",
    "Total HTTP requests by service/method/path/status_code.",
    labelnames=("service", "method", "path", "status_code"),
    registry=REGISTRY,
)

SAMUS_HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "samus_http_request_duration_seconds",
    "HTTP request duration in seconds by service/method/path.",
    labelnames=("service", "method", "path"),
    registry=REGISTRY,
)

SAMUS_TASK_TOTAL = Counter(
    "samus_task_total",
    "Task outcomes by service/status.",
    labelnames=("service", "status"),
    registry=REGISTRY,
)


# ---------------------------------------------------------------------------
# Per-workcell LLM token accounting (see backend/common/llm_budget.py)
# Cumulative counters across the process lifetime — Prometheus rate() over a
# scrape window gives the operator current burn. Gauges expose the adaptive
# quota math so dashboards can plot "headroom" without re-implementing it.
# ---------------------------------------------------------------------------

SAMUS_LLM_TOKENS_TOTAL = Counter(
    "samus_llm_tokens_total",
    "LLM tokens consumed by workcell + direction (input|output).",
    labelnames=("workcell", "kind"),
    registry=REGISTRY,
)

SAMUS_LLM_CALLS_TOTAL = Counter(
    "samus_llm_calls_total",
    "LLM call outcomes by workcell + outcome (success|failure|error).",
    labelnames=("workcell", "outcome"),
    registry=REGISTRY,
)

SAMUS_LLM_BUDGET_QUOTA = Gauge(
    "samus_llm_budget_quota_tokens",
    "Current adaptive daily quota by workcell (base * efficiency_factor, "
    "clamped to floor).",
    labelnames=("workcell",),
    registry=REGISTRY,
)

SAMUS_LLM_BUDGET_USED = Gauge(
    "samus_llm_budget_used_tokens",
    "Tokens consumed today by workcell (resets at UTC midnight).",
    labelnames=("workcell",),
    registry=REGISTRY,
)

SAMUS_LLM_EFFICIENCY = Gauge(
    "samus_llm_efficiency_ema",
    "Exponentially-weighted moving average of success ratio by workcell. "
    "Drives the adaptive quota factor.",
    labelnames=("workcell",),
    registry=REGISTRY,
)


# ---------------------------------------------------------------------------
# LLM cost-hardening instruments (token-cost-hardening 2026-05-18)
# Control C: circuit breaker. Control D: prompt cache. Control A: $ tracking.
# ---------------------------------------------------------------------------

SAMUS_LLM_CIRCUIT_TRIPS_TOTAL = Counter(
    "samus_llm_circuit_trips_total",
    "Cumulative count of circuit-breaker trips by workcell. "
    "Each trip = consecutive_errors hit threshold and cooldown started.",
    labelnames=("workcell",),
    registry=REGISTRY,
)

SAMUS_LLM_CACHE_HITS_TOTAL = Counter(
    "samus_llm_cache_hits_total",
    "Cumulative cache_read_input_tokens by workcell (Anthropic prompt cache). "
    "Each hit saves ~90% vs uncached input cost.",
    labelnames=("workcell",),
    registry=REGISTRY,
)

SAMUS_LLM_CACHE_CREATIONS_TOTAL = Counter(
    "samus_llm_cache_creations_total",
    "Cumulative cache_creation_input_tokens by workcell. "
    "First-write premium (~125% of input) for subsequent cache hits.",
    labelnames=("workcell",),
    registry=REGISTRY,
)

SAMUS_LLM_DOLLAR_USED_TODAY = Gauge(
    "samus_llm_dollar_used_today",
    "USD spent today by scope. scope='global' is the cross-workcell total "
    "tracked by the global $-cap store; scope=<workcell> mirrors the same "
    "value (kept per-workcell for at-a-glance dashboard breakdown).",
    labelnames=("scope",),
    registry=REGISTRY,
)

SAMUS_LLM_DOLLAR_CAP = Gauge(
    "samus_llm_dollar_cap",
    "Configured USD cap by scope. scope='global' = llm_global_daily_dollar_cap; "
    "scope=<workcell> mirrors the same cap (no per-workcell $-cap exists yet).",
    labelnames=("scope",),
    registry=REGISTRY,
)


# ---------------------------------------------------------------------------
# Cognitive LLM routing observability (see backend/common/llm_telemetry.py)
# Makes reasoning routing + throttling countable/alertable: which backend a
# workcell's reasoning billed (paid OpenAI vs free local), and which budget
# control denied a call when reasoning goes dark.
# ---------------------------------------------------------------------------

SAMUS_LLM_ROUTING_TOTAL = Counter(
    "samus_llm_routing_total",
    "LLM calls dispatched by workcell + resolved backend "
    "(backend='openai' is paid, 'local' is free LM Studio). rate() shows how "
    "much reasoning is billing paid vs running free.",
    labelnames=("workcell", "backend"),
    registry=REGISTRY,
)

SAMUS_LLM_DENIALS_TOTAL = Counter(
    "samus_llm_denials_total",
    "LLM calls DENIED pre-flight by workcell + the control that denied "
    "(global_cap|workcell_quota|circuit|frozen|broker|model_floor). A rising "
    "series for a reasoning workcell = its cognition is going dark on throttle.",
    labelnames=("workcell", "control"),
    registry=REGISTRY,
)


# ---------------------------------------------------------------------------
# Learning observability (see backend/common/learning_telemetry.py)
# Makes bandit / attribution learning updates countable + alertable: a rising
# persisted="false" series means the durable store is rejecting writes and
# learning is silently degrading to cache-only (lost on restart).
# ---------------------------------------------------------------------------

SAMUS_LEARNING_UPDATES_TOTAL = Counter(
    "samus_learning_updates_total",
    "Bandit / attribution learning updates by kind (bandit|variant) + whether "
    "the durable write persisted (true|false). persisted=false = learning "
    "silently degrading to cache-only.",
    labelnames=("kind", "persisted"),
    registry=REGISTRY,
)


# ---------------------------------------------------------------------------
# CRM anomaly sensors (security-engineering-baseline §4)
# EMA-based spike detection on high-value financial events.
# ---------------------------------------------------------------------------

SAMUS_CRM_CLOSED_WON_TOTAL = Counter(
    "samus_crm_closed_won_total",
    "Cumulative closed-won opportunity events. Used to drive anomaly EMA.",
    registry=REGISTRY,
)

SAMUS_CRM_CLOSED_WON_ANOMALY_TOTAL = Counter(
    "samus_crm_closed_won_anomaly_total",
    "Closed-won spike anomaly events (value > 3 * EMA). Advisory-only.",
    registry=REGISTRY,
)

SAMUS_CRM_CLOSED_WON_BLOCKED_TOTAL = Counter(
    "samus_crm_closed_won_blocked_total",
    "Closed-won advances BLOCKED for exceeding SAMUS_CRM_MAX_CLOSE_AMOUNT_USD "
    "(redteam FIN-10). Hard fail-closed control, not advisory.",
    registry=REGISTRY,
)


# ---------------------------------------------------------------------------
# Campaign Orchestrator instruments (see backend/campaigns/metrics.py)
# The campaigns workcell composes existing workcells from a declarative graph;
# these make a campaign run's progress, cost, KPIs, artifacts, approval queue
# and conversion funnel countable + alertable. All instruments live here per
# the hard rule that callers never instantiate prometheus_client.* directly —
# backend/campaigns/metrics.py is the thin domain wrapper over them.
# ---------------------------------------------------------------------------

SAMUS_CAMPAIGN_RUNS_TOTAL = Counter(
    "samus_campaign_runs_total",
    "Campaign run lifecycle transitions by client/template/status. rate() over "
    "status='completed'|'failed' shows throughput; status is the terminal or "
    "latest observed run state.",
    labelnames=("client_id", "template_id", "status"),
    registry=REGISTRY,
)

SAMUS_CAMPAIGN_NODE_DURATION_SECONDS = Histogram(
    "samus_campaign_node_duration_seconds",
    "Wall-clock duration of a campaign node execution by client/template/node.",
    labelnames=("client_id", "template_id", "node"),
    registry=REGISTRY,
)

SAMUS_CAMPAIGN_NODE_FAILURES_TOTAL = Counter(
    "samus_campaign_node_failures_total",
    "Campaign node execution failures by client/template/node/reason "
    "(reason='dispatch'|'timeout'|'capability'|'unsafe_unapproved'|'exception').",
    labelnames=("client_id", "template_id", "node", "reason"),
    registry=REGISTRY,
)

SAMUS_CAMPAIGN_KPI_VALUE = Gauge(
    "samus_campaign_kpi_value",
    "Latest value of a tracked campaign KPI by client/campaign/kpi.",
    labelnames=("client_id", "campaign_id", "kpi"),
    registry=REGISTRY,
)

SAMUS_CAMPAIGN_ARTIFACTS_TOTAL = Counter(
    "samus_campaign_artifacts_total",
    "Campaign artifacts created by client/campaign/type (only references are "
    "stored; large media lives outside the ledger).",
    labelnames=("client_id", "campaign_id", "type"),
    registry=REGISTRY,
)

SAMUS_CAMPAIGN_APPROVAL_QUEUE_TOTAL = Gauge(
    "samus_campaign_approval_queue_total",
    "Current pending approval-gate depth by client/campaign/severity. A rising "
    "series = campaign paused waiting on a human decision.",
    labelnames=("client_id", "campaign_id", "severity"),
    registry=REGISTRY,
)

SAMUS_CAMPAIGN_CONVERSION_EVENTS_TOTAL = Counter(
    "samus_campaign_conversion_events_total",
    "Campaign conversion / funnel events by client/campaign/event_type "
    "(cta_clicks, application_starts, tour_requests, ...).",
    labelnames=("client_id", "campaign_id", "event_type"),
    registry=REGISTRY,
)


def metrics_response() -> Response:
    """Return a Prometheus-format Response for the /metrics endpoint."""
    return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
