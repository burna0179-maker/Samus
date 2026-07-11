"""Campaign KPI definitions + event ingestion.

The initial KPI vocabulary (deliverable §9) plus the ingestion path that folds
manual updates and webhook/imported events into a run's ``kpis_updated`` map and
mirrors them onto the Prometheus gauge + conversion counter.

Two update modes, both routed through :func:`apply_kpi_events`:
  * manual   — an operator sets/increments a KPI directly;
  * webhook  — an external analytics/imported event (page views, CTA clicks,
               application starts, ...) increments the funnel counter.
"""

from __future__ import annotations

from typing import Any

from . import metrics
from .models import CampaignKPI, CampaignRun

# --- initial KPI catalog (deliverable §9) ---------------------------------

_DEF = CampaignKPI  # brevity

INITIAL_KPIS: dict[str, CampaignKPI] = {
    k.key: k
    for k in [
        _DEF(key="page_views", label="Page Views", unit="count"),
        _DEF(key="source_clicks", label="Source Clicks", unit="count"),
        _DEF(key="cta_clicks", label="CTA Clicks", unit="count"),
        _DEF(key="phone_clicks", label="Phone Clicks", unit="count"),
        _DEF(key="email_clicks", label="Email Clicks", unit="count"),
        _DEF(key="messenger_clicks", label="Messenger Clicks", unit="count"),
        _DEF(key="application_starts", label="Application Starts", unit="count"),
        _DEF(key="application_completions", label="Application Completions", unit="count"),
        _DEF(key="tour_requests", label="Tour Requests", unit="count"),
        _DEF(key="open_house_registrations", label="Open House Registrations", unit="count"),
        _DEF(key="social_posts_published", label="Social Posts Published", unit="count"),
        _DEF(key="social_engagements", label="Social Engagements", unit="count"),
        _DEF(key="backlinks_created", label="Backlinks Created", unit="count"),
        _DEF(key="reviews_requested", label="Reviews Requested", unit="count"),
        _DEF(key="reviews_received", label="Reviews Received", unit="count"),
        _DEF(key="local_partners_contacted", label="Local Partners Contacted", unit="count"),
        _DEF(key="flyers_distributed", label="Flyers Distributed", unit="count"),
    ]
}

# KPIs that represent a funnel/conversion event — an increment on one of these
# also bumps samus_campaign_conversion_events_total for funnel analytics.
_CONVERSION_KPIS = frozenset(
    {
        "source_clicks",
        "cta_clicks",
        "phone_clicks",
        "email_clicks",
        "messenger_clicks",
        "application_starts",
        "application_completions",
        "tour_requests",
        "open_house_registrations",
    }
)

VALID_MODES = frozenset({"set", "increment", "inc"})


def is_known_kpi(key: str) -> bool:
    return key in INITIAL_KPIS


def normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    """Validate + normalize one raw KPI event into ``{kpi, mode, value}``."""
    kpi = str(event.get("kpi") or event.get("key") or "").strip()
    if not is_known_kpi(kpi):
        raise ValueError(f"unknown KPI {kpi!r}")
    mode = str(event.get("mode", "increment")).strip().lower()
    if mode not in VALID_MODES:
        raise ValueError(f"invalid KPI mode {mode!r}")
    if mode == "inc":
        mode = "increment"
    try:
        value = float(event.get("value", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"non-numeric KPI value: {event.get('value')!r}") from exc
    return {"kpi": kpi, "mode": mode, "value": value}


def apply_kpi_events(run: CampaignRun, events: list[dict[str, Any]]) -> dict[str, float]:
    """Fold KPI events into ``run.kpis_updated`` and mirror to metrics.

    Returns the map of KPIs actually changed to their new values. Invalid
    events raise ``ValueError`` (the caller decides whether to 422). Metrics
    emission is fail-soft inside the ``metrics`` helpers.
    """
    changed: dict[str, float] = {}
    for raw in events:
        norm = normalize_event(raw)
        key, mode, value = norm["kpi"], norm["mode"], norm["value"]
        current = float(run.kpis_updated.get(key, 0.0))
        new_value = value if mode == "set" else current + value
        run.kpis_updated[key] = new_value
        changed[key] = new_value
        metrics.set_kpi_value(run.client_id, run.campaign_id, key, new_value)
        if key in _CONVERSION_KPIS and mode != "set":
            metrics.inc_conversion(run.client_id, run.campaign_id, key, int(max(0, value)))
    if changed:
        run.touch()
    return changed


def snapshot(run: CampaignRun) -> dict[str, float]:
    """Current KPI values (the ``metrics_collection`` node output)."""
    return dict(run.kpis_updated)
