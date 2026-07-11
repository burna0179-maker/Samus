"""ROI roll-ups — pre-aggregated daily revenue minus cost, per dimension.

HOTL Tranche 2 (Economic Brain), framework Phase 1 + Phase 9. One
:func:`compute_daily_rollup` call produces a :class:`RoiRollup` for a UTC
day: total revenue, total cost, net, and per-dimension breakdowns
(campaign_id, channel, lead_source, workcell, variant_arm_id).

Data sources (all best-effort; each missing source degrades to zero):

Revenue
    * ``finance/outreach_attribution.py`` conversions ledger (Stripe paid
      checkouts credited to cold-outreach prospects);
    * the Stripe webhook event ledger (``finance/webhook.py``) for paid
      events NOT already counted via an outreach conversion (dedup by
      Stripe ``event_id``);
    * the unified business-event stream (``payment.received`` via
      :mod:`backend.common.business_events_shim`) for events carrying
      dimensions the concrete ledgers lack (campaign_id, variant_arm_id) —
      empty pre-merge.

Cost
    * LLM spend from the llm-budget store's JSON fallback file (per-workcell
      today-counters -> USD via ``llm_pricing.cost_from_usage``; only
      meaningful when ``day`` is the store's current bucket_day);
    * voice (Vapi) spend from ``voice_events.jsonl`` end-of-call records;
    * cost-bearing business events (``cost_usd`` field) from the unified
      stream — empty pre-merge.

Multi-touch attribution (Phase 9 per-workcell profit): each revenue item's
journey is reconstructed via ``read_events(prospect_id=...)``; revenue is
split in equal fractions across the distinct workcells that touched the
journey. Pre-merge (empty stream) the fallback attributes 100% to the
recording workcell.

Persistence extends the ``samus_portfolio_snapshots`` pattern: one row per
day, PK ``bucket_day = "roi_rollup::<YYYY-MM-DD>"`` in the SAME DDB table
(``row_kind="roi_rollup"`` attribute; DDB is schemaless beyond the PK so no
schema change), JSON-file fallback at ``SAMUS_ROI_ROLLUP_PATH``.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from typing import Any

from backend.common.business_events_shim import emit_business_event, read_events
from backend.common.dates import iso_now

_LOG = logging.getLogger("samus.finance.roi")

# JSON fallback store (dict keyed by day). Same data tree as the sibling
# strategy snapshots file.
_DEFAULT_JSON_PATH = "/opt/samus/data/finance/roi_rollups.json"
# PK namespace inside samus_portfolio_snapshots (see module docstring).
_ROLLUP_PK_PREFIX = "roi_rollup::"
ROW_KIND = "roi_rollup"

# Model used to convert llm-budget token counters into USD. The budget store
# tracks tokens, not dollars; nearly all metered Samus calls are Haiku-class.
_DEFAULT_PRICING_MODEL = os.getenv("SAMUS_ROI_LLM_PRICING_MODEL", "claude-haiku-4")

_LLM_BUDGET_JSON_DEFAULT = "/opt/samus/data/llm_budgets.json"

_json_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------


def _empty_bucket() -> dict[str, float]:
    return {"revenue_usd": 0.0, "cost_usd": 0.0, "net_usd": 0.0}


@dataclass
class RoiRollup:
    """One UTC day's pre-aggregated ROI."""

    day: str  # YYYY-MM-DD
    generated_at: str = ""
    revenue_usd: float = 0.0
    cost_usd: float = 0.0
    net_usd: float = 0.0
    conversion_count: int = 0
    by_campaign: dict[str, dict[str, float]] = field(default_factory=dict)
    by_channel: dict[str, dict[str, float]] = field(default_factory=dict)
    by_lead_source: dict[str, dict[str, float]] = field(default_factory=dict)
    by_workcell: dict[str, dict[str, float]] = field(default_factory=dict)
    by_variant_arm: dict[str, dict[str, float]] = field(default_factory=dict)
    sources: dict[str, int] = field(default_factory=dict)  # per-source item counts
    events_stream_available: bool = False

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


def _bucket(table: dict[str, dict[str, float]], key: str) -> dict[str, float]:
    k = key or "(none)"
    if k not in table:
        table[k] = _empty_bucket()
    return table[k]


def _round_all(rollup: RoiRollup) -> RoiRollup:
    rollup.revenue_usd = round(rollup.revenue_usd, 4)
    rollup.cost_usd = round(rollup.cost_usd, 4)
    rollup.net_usd = round(rollup.revenue_usd - rollup.cost_usd, 4)
    for table in (
        rollup.by_campaign,
        rollup.by_channel,
        rollup.by_lead_source,
        rollup.by_workcell,
        rollup.by_variant_arm,
    ):
        for row in table.values():
            row["revenue_usd"] = round(row["revenue_usd"], 4)
            row["cost_usd"] = round(row["cost_usd"], 4)
            row["net_usd"] = round(row["revenue_usd"] - row["cost_usd"], 4)
    return rollup


# ---------------------------------------------------------------------------
# Revenue item model — normalised across the three revenue sources
# ---------------------------------------------------------------------------


@dataclass
class _RevenueItem:
    amount_usd: float
    prospect_id: str = ""
    workcell: str = "finance"  # recording workcell (100% fallback)
    channel: str = ""
    lead_source: str = ""
    campaign_id: str = ""
    variant_arm_id: str = ""
    event_id: str = ""  # Stripe event id for cross-source dedup


def _journey_workcells(prospect_id: str) -> list[str]:
    """Distinct workcells touching this prospect's journey, via the shim.

    Empty pre-merge / on failure — caller falls back to the recording
    workcell (100% attribution).
    """
    if not prospect_id:
        return []
    cells: list[str] = []
    for ev in read_events(prospect_id=prospect_id, limit=1000):
        wc = str(ev.get("workcell") or "")
        if wc and wc not in cells:
            cells.append(wc)
    return cells


def _apply_revenue(rollup: RoiRollup, item: _RevenueItem) -> None:
    amt = float(item.amount_usd or 0.0)
    if amt <= 0:
        return
    rollup.revenue_usd += amt
    rollup.conversion_count += 1
    _bucket(rollup.by_campaign, item.campaign_id)["revenue_usd"] += amt
    _bucket(rollup.by_channel, item.channel)["revenue_usd"] += amt
    _bucket(rollup.by_lead_source, item.lead_source)["revenue_usd"] += amt
    _bucket(rollup.by_variant_arm, item.variant_arm_id)["revenue_usd"] += amt
    # Multi-touch: equal fractional split across journey workcells.
    cells = _journey_workcells(item.prospect_id)
    if not cells:
        cells = [item.workcell or "finance"]
    share = amt / len(cells)
    for wc in cells:
        _bucket(rollup.by_workcell, wc)["revenue_usd"] += share


# ---------------------------------------------------------------------------
# Concrete-ledger readers (best-effort, each degrades to empty/zero)
# ---------------------------------------------------------------------------


def _revenue_from_outreach_conversions(day: str) -> list[_RevenueItem]:
    try:
        from backend.finance.outreach_attribution import load_conversions

        conversions = load_conversions()
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("roi: outreach conversions unavailable: %s", exc)
        return []
    items: list[_RevenueItem] = []
    for c in conversions:
        if str(c.attributed_at or "")[:10] != day:
            continue
        items.append(
            _RevenueItem(
                amount_usd=float(c.amount_usd or 0.0),
                prospect_id=c.prospect_id,
                workcell="outreach",
                channel="email",
                lead_source="outreach",
                event_id=c.event_id or "",
            )
        )
    return items


def _revenue_from_webhook_ledger(day: str, *, skip_event_ids: set[str]) -> list[_RevenueItem]:
    """Paid Stripe checkout events on ``day`` not already attributed."""
    try:
        from backend.finance import webhook as fw

        today = _dt.date.today()
        target = _dt.date.fromisoformat(day)
        window = max(1, (today - target).days + 2)
        events = fw.load_recent_events(window_days=window)
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("roi: webhook ledger unavailable: %s", exc)
        return []
    items: list[_RevenueItem] = []
    for ev in events:
        if str(ev.received_at or "")[:10] != day:
            continue
        if ev.event_id and ev.event_id in skip_event_ids:
            continue  # already counted via an outreach conversion
        amount = float(ev.amount_total_usd or 0.0)
        if amount <= 0:
            continue
        if ev.payment_status and ev.payment_status not in ("paid", "no_payment_required"):
            continue
        items.append(
            _RevenueItem(
                amount_usd=amount,
                workcell="finance",
                channel="direct",
                lead_source=ev.hf_offer_code or "stripe",
                event_id=ev.event_id or "",
            )
        )
    return items


def _revenue_from_event_stream(day: str, *, skip_event_ids: set[str]) -> list[_RevenueItem]:
    """``payment.received`` events from the unified stream (empty pre-merge)."""
    items: list[_RevenueItem] = []
    for ev in read_events(since=day, event_types=["payment.received"], limit=1000):
        if str(ev.get("ts") or "")[:10] != day:
            continue
        meta = ev.get("metadata") or {}
        stripe_id = str(meta.get("event_id") or meta.get("stripe_event_id") or "")
        if stripe_id and stripe_id in skip_event_ids:
            continue  # concrete ledger already counted this payment
        amount = float(ev.get("revenue_usd") or 0.0)
        if amount <= 0:
            continue
        items.append(
            _RevenueItem(
                amount_usd=amount,
                prospect_id=str(ev.get("prospect_id") or ""),
                workcell=str(ev.get("workcell") or "finance"),
                channel=str(meta.get("channel") or ""),
                lead_source=str(meta.get("lead_source") or ""),
                campaign_id=str(ev.get("campaign_id") or ""),
                variant_arm_id=str(ev.get("variant_arm_id") or ""),
                event_id=stripe_id,
            )
        )
    return items


def _llm_costs_by_workcell(day: str) -> dict[str, float]:
    """Per-workcell LLM spend (USD) for ``day`` from the budget store's JSON.

    The llm-budget store keeps *today's* token counters only, so a non-today
    ``day`` (or a bucket_day mismatch) yields {} — historical LLM cost rides
    the unified event stream once Tranche 1 merges.
    """
    path = os.getenv("SAMUS_LLM_BUDGET_PATH", _LLM_BUDGET_JSON_DEFAULT)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, ValueError):
        return {}
    try:
        from backend.common.llm_pricing import cost_from_usage
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, float] = {}
    for workcell, row in data.items():
        if not isinstance(row, dict):
            continue
        if str(row.get("bucket_day") or "") != day:
            continue
        usage = {
            "input_tokens": int(row.get("input_tokens_today", 0) or 0),
            "output_tokens": int(row.get("output_tokens_today", 0) or 0),
        }
        try:
            usd = float(cost_from_usage(_DEFAULT_PRICING_MODEL, usage))
        except Exception:  # noqa: BLE001
            usd = 0.0
        if usd > 0:
            out[str(workcell)] = round(usd, 4)
    return out


def _voice_cost_for_day(day: str) -> float:
    """Sum Vapi end-of-call costs recorded on ``day`` from voice_events.jsonl."""
    try:
        from backend.voice.voice_cost_tracker import _events_path

        path = _events_path()
    except Exception:  # noqa: BLE001
        return 0.0
    if not path.exists():
        return 0.0
    total = 0.0
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("kind") not in ("end_of_call", "inbound_end_of_call"):
                    continue
                if str(rec.get("ts", ""))[:10] != day:
                    continue
                try:
                    total += float(rec.get("vapi_cost") or 0.0)
                except (TypeError, ValueError):
                    continue
    except OSError as exc:
        _LOG.debug("roi: voice events unreadable: %s", exc)
        return 0.0
    return round(total, 4)


def _apply_event_stream_costs(rollup: RoiRollup, day: str) -> None:
    """Cost-bearing unified-stream events (empty pre-merge)."""
    for ev in read_events(since=day, limit=2000):
        if str(ev.get("ts") or "")[:10] != day:
            continue
        cost = float(ev.get("cost_usd") or 0.0)
        if cost <= 0:
            continue
        meta = ev.get("metadata") or {}
        rollup.cost_usd += cost
        _bucket(rollup.by_workcell, str(ev.get("workcell") or ""))["cost_usd"] += cost
        _bucket(rollup.by_campaign, str(ev.get("campaign_id") or ""))["cost_usd"] += cost
        _bucket(rollup.by_channel, str(meta.get("channel") or ""))["cost_usd"] += cost
        _bucket(rollup.by_lead_source, str(meta.get("lead_source") or ""))["cost_usd"] += cost
        _bucket(rollup.by_variant_arm, str(ev.get("variant_arm_id") or ""))["cost_usd"] += cost


# ---------------------------------------------------------------------------
# Roll-up computation
# ---------------------------------------------------------------------------


def compute_daily_rollup(day: str | None = None) -> RoiRollup:
    """Compute the ROI roll-up for one UTC ``day`` (default: today).

    Never raises: every source is independently best-effort, so the worst
    case is an all-zero roll-up (which is itself a truthful statement about
    what the ledgers contain).
    """
    day = (day or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d"))[:10]
    rollup = RoiRollup(day=day, generated_at=iso_now())

    from backend.common.business_events_shim import events_available

    rollup.events_stream_available = events_available()

    # -- revenue ------------------------------------------------------------
    conv_items = _revenue_from_outreach_conversions(day)
    seen_event_ids = {i.event_id for i in conv_items if i.event_id}
    webhook_items = _revenue_from_webhook_ledger(day, skip_event_ids=seen_event_ids)
    seen_event_ids |= {i.event_id for i in webhook_items if i.event_id}
    stream_items = _revenue_from_event_stream(day, skip_event_ids=seen_event_ids)

    for item in conv_items + webhook_items + stream_items:
        _apply_revenue(rollup, item)
    rollup.sources = {
        "outreach_conversions": len(conv_items),
        "webhook_ledger": len(webhook_items),
        "event_stream": len(stream_items),
    }

    # -- costs ---------------------------------------------------------------
    llm_costs = _llm_costs_by_workcell(day)
    for workcell, usd in llm_costs.items():
        rollup.cost_usd += usd
        _bucket(rollup.by_workcell, workcell)["cost_usd"] += usd
        _bucket(rollup.by_channel, "llm")["cost_usd"] += usd
    voice_cost = _voice_cost_for_day(day)
    if voice_cost > 0:
        rollup.cost_usd += voice_cost
        _bucket(rollup.by_workcell, "voice")["cost_usd"] += voice_cost
        _bucket(rollup.by_channel, "call")["cost_usd"] += voice_cost
    _apply_event_stream_costs(rollup, day)

    return _round_all(rollup)


# ---------------------------------------------------------------------------
# Persistence — samus_portfolio_snapshots-pattern store (DDB + JSON fallback)
# ---------------------------------------------------------------------------


def rollup_store_path() -> str:
    """JSON fallback path (``SAMUS_ROI_ROLLUP_PATH`` overrides)."""
    return os.environ.get("SAMUS_ROI_ROLLUP_PATH", _DEFAULT_JSON_PATH)


def _save_json(rollup: RoiRollup) -> bool:
    path = rollup_store_path()
    with _json_lock:
        data: dict[str, Any] = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
            except (OSError, ValueError):
                data = {}
        data[rollup.day] = {"row_kind": ROW_KIND, **rollup.to_record()}
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
            os.replace(tmp, path)
            return True
        except OSError as exc:
            _LOG.warning("roi rollup json save failed: %s", exc)
            return False


def _save_ddb(rollup: RoiRollup) -> bool:
    """Mirror into the samus_portfolio_snapshots DDB table (new row kind).

    The table's PK is ``bucket_day`` (opaque string); the roll-up namespaces
    its rows ``roi_rollup::<day>`` + tags ``row_kind`` so the strategy
    snapshot reader (which loads plain ``<day>`` keys) never collides.
    """
    table_name = os.environ.get(
        "DDB_PORTFOLIO_SNAPSHOTS_TABLE",
        "samus_portfolio_snapshots",
    )
    if not table_name:
        return False
    try:
        from backend.common import aws
        from backend.common.config import get_settings

        region = get_settings().aws_region
        item = {
            "bucket_day": f"{_ROLLUP_PK_PREFIX}{rollup.day}",
            "row_kind": ROW_KIND,
            "payload": json.dumps(rollup.to_record(), default=str),
        }
        aws.table(table_name, region).put_item(Item=item)
        return True
    except Exception as exc:  # noqa: BLE001 — JSON fallback is the safety net
        _LOG.debug("roi rollup ddb save skipped/failed: %s", exc)
        return False


def save_rollup(rollup: RoiRollup) -> bool:
    """Persist one roll-up (DDB mirror best-effort, JSON authoritative-local)."""
    _save_ddb(rollup)
    return _save_json(rollup)


def load_rollup(day: str) -> dict[str, Any] | None:
    """Load a persisted roll-up for ``day`` from the JSON store, or None."""
    path = rollup_store_path()
    with _json_lock:
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except (OSError, ValueError) as exc:
            _LOG.warning("roi rollup json load failed: %s", exc)
            return None
    row = data.get(day)
    return dict(row) if isinstance(row, dict) else None


def get_rollup(day: str | None = None, *, recompute: bool = False) -> dict[str, Any]:
    """Persisted roll-up for ``day`` (default today), computing when absent.

    Today's roll-up is always recomputed (the day is still accruing);
    historical days are served from the store unless ``recompute``.
    """
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    day = (day or today)[:10]
    if not recompute and day != today:
        stored = load_rollup(day)
        if stored is not None:
            return stored
    rollup = compute_daily_rollup(day)
    save_rollup(rollup)
    return {"row_kind": ROW_KIND, **rollup.to_record()}


# ---------------------------------------------------------------------------
# Nightly entry point (scheduler-callable)
# ---------------------------------------------------------------------------


def run_nightly_rollup(*, day: str | None = None) -> RoiRollup:
    """Compute + persist the roll-up for ``day`` (default: yesterday UTC).

    The callable a scheduler (Task Scheduler entry / in-container timer —
    wiring is a separate job) invokes after midnight so the just-finished
    day is aggregated once, durably. Also emits a ``decision.made``-adjacent
    audit trail via the unified stream (no-op pre-merge).
    """
    if day is None:
        day = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
    rollup = compute_daily_rollup(day)
    save_rollup(rollup)
    emit_business_event(
        "decision.made",
        workcell="finance",
        revenue_usd=rollup.revenue_usd,
        cost_usd=rollup.cost_usd,
        metadata={
            "decision_kind": "roi_nightly_rollup",
            "day": rollup.day,
            "net_usd": rollup.net_usd,
            "conversion_count": rollup.conversion_count,
        },
    )
    _LOG.info(
        "roi nightly rollup day=%s revenue=$%.2f cost=$%.2f net=$%.2f",
        rollup.day,
        rollup.revenue_usd,
        rollup.cost_usd,
        rollup.net_usd,
    )
    return rollup


# ---------------------------------------------------------------------------
# Gateway route registration (single call-site line in gateway/app.py)
# ---------------------------------------------------------------------------


def register_economics_admin_routes(app: Any) -> None:
    """Attach ``GET /admin/roi`` + ``GET /admin/arbitration`` to a FastAPI app.

    Defined here (an owned Tranche-2 module) so ``gateway/app.py`` only
    gains one registration line — sibling branches edit that file too.
    """

    @app.get("/admin/roi")
    async def admin_roi(day: str | None = None, recompute: bool = False) -> dict[str, Any]:
        """Daily ROI roll-up (revenue - cost per campaign/channel/source/
        workcell/variant). ``day=YYYY-MM-DD`` defaults to today; pure read
        apart from caching the computed roll-up."""
        try:
            return {"ok": True, "rollup": get_rollup(day, recompute=recompute)}
        except Exception as exc:  # noqa: BLE001 — operator surface never 500s
            _LOG.warning("/admin/roi failed: %s", exc)
            return {"ok": False, "error": str(exc), "rollup": None}

    @app.get("/admin/arbitration")
    async def admin_arbitration(refresh: bool = False) -> dict[str, Any]:
        """Latest economically ranked work queue from the task arbiter.

        ``refresh=true`` runs a fresh arbitration (collect + rank + persist);
        default serves the most recent persisted run.
        """
        try:
            from backend.strategy import arbiter

            if refresh:
                return {"ok": True, "arbitration": arbiter.arbitrate_daily().to_record()}
            latest = arbiter.latest_arbitration()
            return {"ok": True, "arbitration": latest}
        except Exception as exc:  # noqa: BLE001 — operator surface never 500s
            _LOG.warning("/admin/arbitration failed: %s", exc)
            return {"ok": False, "error": str(exc), "arbitration": None}


__all__ = [
    "RoiRollup",
    "ROW_KIND",
    "compute_daily_rollup",
    "save_rollup",
    "load_rollup",
    "get_rollup",
    "run_nightly_rollup",
    "rollup_store_path",
    "register_economics_admin_routes",
]
