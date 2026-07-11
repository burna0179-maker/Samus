"""Domain metric helpers for the Campaign Orchestrator.

The Prometheus instruments themselves live in ``backend.common.metrics`` (the
hard rule: callers never instantiate ``prometheus_client.*`` directly). This
module is the thin, intention-revealing wrapper the executor / orchestrator /
KPI ingestion call — so instrumentation reads as ``metrics.record_run(...)``
rather than raw label plumbing, and every emit is fail-soft (a metrics error
never breaks a campaign).
"""

from __future__ import annotations

import logging

from backend.common.metrics import (
    SAMUS_CAMPAIGN_APPROVAL_QUEUE_TOTAL,
    SAMUS_CAMPAIGN_ARTIFACTS_TOTAL,
    SAMUS_CAMPAIGN_CONVERSION_EVENTS_TOTAL,
    SAMUS_CAMPAIGN_KPI_VALUE,
    SAMUS_CAMPAIGN_NODE_DURATION_SECONDS,
    SAMUS_CAMPAIGN_NODE_FAILURES_TOTAL,
    SAMUS_CAMPAIGN_RUNS_TOTAL,
)

_LOG = logging.getLogger("samus.campaigns.metrics")


def _safe(fn) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 — metrics must never break a run
        _LOG.debug("campaign metric emit failed: %s", exc)


def record_run(client_id: str, template_id: str, status: str) -> None:
    _safe(lambda: SAMUS_CAMPAIGN_RUNS_TOTAL.labels(client_id, template_id, status).inc())


def observe_node_duration(client_id: str, template_id: str, node: str, seconds: float) -> None:
    _safe(
        lambda: SAMUS_CAMPAIGN_NODE_DURATION_SECONDS.labels(client_id, template_id, node).observe(
            max(0.0, float(seconds))
        )
    )


def record_node_failure(client_id: str, template_id: str, node: str, reason: str) -> None:
    _safe(
        lambda: SAMUS_CAMPAIGN_NODE_FAILURES_TOTAL.labels(
            client_id, template_id, node, reason
        ).inc()
    )


def set_kpi_value(client_id: str, campaign_id: str, kpi: str, value: float) -> None:
    _safe(lambda: SAMUS_CAMPAIGN_KPI_VALUE.labels(client_id, campaign_id, kpi).set(float(value)))


def inc_artifacts(client_id: str, campaign_id: str, artifact_type: str, n: int = 1) -> None:
    _safe(
        lambda: SAMUS_CAMPAIGN_ARTIFACTS_TOTAL.labels(client_id, campaign_id, artifact_type).inc(n)
    )


def set_approval_queue(client_id: str, campaign_id: str, severity: str, depth: int) -> None:
    _safe(
        lambda: SAMUS_CAMPAIGN_APPROVAL_QUEUE_TOTAL.labels(client_id, campaign_id, severity).set(
            max(0, int(depth))
        )
    )


def inc_conversion(client_id: str, campaign_id: str, event_type: str, n: int = 1) -> None:
    _safe(
        lambda: SAMUS_CAMPAIGN_CONVERSION_EVENTS_TOTAL.labels(
            client_id, campaign_id, event_type
        ).inc(n)
    )
