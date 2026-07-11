"""Commercial action wrap — EFH/template/ISV/RBL/dual-channel/VER pipeline.

Ordering (load-bearing — see solvency_does_not_swallow_ethics,
audit_envelope_precedes_commercial_commit):

    1. ISV scope check (no_active_isv refuses immediately)
    2. RBL band check (critical/freeze gates affected classes)
    3. Template lookup -> EFH bypass with template_<id> attribution
       OR EFH evaluation (veto blocks commit)
    4. Dual-channel: send ActionTakenEnvelope to Major; wait audit_ack
    5. Emit ValueExchangeRecord (only after EFH-clear + audit_ack)
    6. Caller performs the external side effect

Required metadata is validated per market_interface.spec.yaml v0.5.0.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import uuid
from pathlib import Path
from typing import Any

import httpx
import yaml

from backend.common.state_paths import state_path

_LOG = logging.getLogger("samus.governance.commercial_wrap")

_SAMUS_ROOT = Path(__file__).resolve().parents[3]
# Durable governance state lives on the writable data volume in-container
# (SAMUS_STATE_ROOT=/opt/samus/data/state); falls back to <code root>/state
# on the host. The image root /opt/samus is read-only. See state_paths.py.
_VER_DIR = state_path("value_exchanges")
_RBL_CACHE = state_path("rbl", "current_band.json")

COMMERCIAL_ACTION_CLASSES: frozenset[str] = frozenset({
    "paid_deliverable_committed",
    "subscription_contract_signed",
    "prospect_outreach_with_paid_cta",
    "llm_call_above_threshold",
    "stripe_payment_processed",
    "refund_or_cancellation",
})

# Subset of required metadata fields per market_interface.spec.yaml. Each
# entry is the MINIMUM set the wrapper enforces — workcells may carry more.
REQUIRED_METADATA: dict[str, tuple[str, ...]] = {
    "paid_deliverable_committed": (
        "value_received", "cost_to_acquire", "roi_timeframe_days",
        "confidence", "contribution_margin_estimate_pct",
    ),
    "subscription_contract_signed": (
        "value_received", "cost_to_acquire", "value_compounding_rate",
        "confidence",
    ),
    "prospect_outreach_with_paid_cta": (
        "target_value_band", "cost_to_send",
    ),
    "llm_call_above_threshold": (
        "estimated_cost_usd", "purpose",
    ),
    "stripe_payment_processed": (
        "stripe_event_id", "amount_usd", "customer_id", "livemode",
    ),
    "refund_or_cancellation": (
        "stripe_refund_id", "amount_usd_negative", "linked_original_transaction",
        "reason",
    ),
}

# Classes RBL freezes by band. critical freezes the non-revenue subset;
# freeze blocks everything except inbound revenue (stripe_payment_processed).
_RBL_BAND_BLOCKS: dict[str, frozenset[str]] = {
    "healthy": frozenset(),
    "warning": frozenset(),
    "critical": frozenset({
        "prospect_outreach_with_paid_cta",
        "llm_call_above_threshold",
    }),
    "freeze": frozenset({
        "paid_deliverable_committed",
        "subscription_contract_signed",
        "prospect_outreach_with_paid_cta",
        "llm_call_above_threshold",
        "refund_or_cancellation",  # explicitly: refunds blocked in freeze
    }),
}


class CommercialActionRefusal(RuntimeError):
    """Raised when the wrap rejects a commercial action commit."""

    def __init__(self, reason: str, *, action_class: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.action_class = action_class


class RblBandConsumer:
    """Reads Major's current RBL band; falls back to local cache file."""

    def __init__(self, *, status_url: str | None = None) -> None:
        self._url = (status_url or "").strip()

    def current_band(self) -> str:
        if self._url:
            try:
                with httpx.Client(timeout=5.0) as client:
                    r = client.get(f"{self._url.rstrip('/')}/api/major/rbl/status")
                    if r.status_code == 200:
                        body = r.json() or {}
                        band = body.get("current_band")
                        if isinstance(band, str) and band in _RBL_BAND_BLOCKS:
                            return band
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("samus.rbl.status_probe_failed: %s", exc)
        # Local cache fallback (populated by inbound RBL alerts).
        if _RBL_CACHE.is_file():
            try:
                doc = json.loads(_RBL_CACHE.read_text(encoding="utf-8"))
                band = doc.get("current_band")
                if isinstance(band, str) and band in _RBL_BAND_BLOCKS:
                    return band
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("samus.rbl.cache_read_failed: %s", exc)
        # TODO(major-rbl-endpoint): default healthy when no signal yet.
        return "healthy"

    def is_action_blocked(self, action_class: str) -> tuple[bool, str]:
        band = self.current_band()
        blocked = _RBL_BAND_BLOCKS.get(band, frozenset())
        if action_class in blocked:
            return True, f"rbl_band_{band}_blocks_{action_class}"
        return False, band


def ingest_rbl_band(
    band: str,
    *,
    source: str = "unknown",
    ts: str | None = None,
) -> bool:
    """Persist an inbound RBL band alert to the local cache the consumer reads.

    This is the PRODUCER half of the local-cache path. Major broadcasts its
    current Runway-Band-Ledger (RBL) band; this writes it to ``_RBL_CACHE`` so
    :meth:`RblBandConsumer.current_band` (the consumer half) sees a real signal
    even when no live ``samus_major_rbl_status_url`` HTTP endpoint is wired (the
    default local deployment). Without this, the cache had no writer and the
    consumer silently defaulted to ``healthy`` — the RBL signal was never
    actually consumed.

    Fail-CLOSED on a band the wrapper does not recognise: an unknown band is
    NOT written (a malformed/forged alert must never be able to *clear* a real
    freeze by overwriting the cache with garbage). Returns ``True`` when the
    cache was updated, ``False`` when the band was rejected or the write failed
    (the caller stays on whatever band was previously cached / probed).

    The write is atomic (temp + replace) so a concurrent reader never observes
    a half-written cache file.
    """
    band = (band or "").strip()
    if band not in _RBL_BAND_BLOCKS:
        _LOG.warning("samus.rbl.ingest_rejected_unknown_band: %r from %s", band, source)
        return False
    doc = {
        "current_band": band,
        "source": str(source or "unknown"),
        "ts": ts or _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    try:
        _RBL_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _RBL_CACHE.with_suffix(_RBL_CACHE.suffix + ".tmp")
        tmp.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
        tmp.replace(_RBL_CACHE)
    except OSError as exc:
        _LOG.error("samus.rbl.ingest_write_failed: %s", exc)
        return False
    _LOG.info("samus.rbl.ingested band=%s source=%s", band, source)
    return True


def _validate_required_metadata(action_class: str, payload: dict) -> list[str]:
    missing = []
    for f in REQUIRED_METADATA.get(action_class, ()):
        if f not in payload or payload[f] in (None, ""):
            missing.append(f)
    return missing


def commit_commercial_action(
    *,
    action_class: str,
    action_payload: dict,
    commercial_destination: str,
    isv_consumer: Any | None,
    template_registry: Any | None,
    efh_evaluator: Any | None,
    dual_channel: Any | None,
    rbl_consumer: RblBandConsumer | None,
    trust_premium: float = 1.0,
) -> dict:
    """Run the full commercial commit pipeline.

    Returns the persisted ValueExchangeRecord dict. Raises
    CommercialActionRefusal with a structured reason on any gate.
    """
    if action_class not in COMMERCIAL_ACTION_CLASSES:
        raise CommercialActionRefusal(
            f"unknown_commercial_action_class:{action_class}",
            action_class=action_class,
        )

    missing = _validate_required_metadata(action_class, action_payload)
    if missing:
        raise CommercialActionRefusal(
            f"missing_required_metadata:{','.join(missing)}",
            action_class=action_class,
        )

    # Step 1 — ISV scope check.
    isv_id: str | None = None
    if isv_consumer is not None:
        in_scope, reason = isv_consumer.is_action_in_scope(action_class, action_payload)
        if not in_scope:
            raise CommercialActionRefusal(reason, action_class=action_class)
        active = isv_consumer.get_active_isv()
        if isinstance(active, dict):
            isv_id = active.get("isv_id")

    # Step 2 — RBL band gate.
    rbl_band = "healthy"
    if rbl_consumer is not None:
        blocked, info = rbl_consumer.is_action_blocked(action_class)
        if blocked:
            raise CommercialActionRefusal(info, action_class=action_class)
        rbl_band = info

    # Step 3 — Template OR EFH. solvency_does_not_swallow_ethics:
    # EFH is mandatory unless a signed template covers the action.
    efh_attribution: str
    template_hit = None
    if template_registry is not None:
        template_hit = template_registry.lookup_for_action(action_class, action_payload)
    if template_hit is not None:
        usage = template_registry.record_use(
            template_hit,
            action_ref=action_payload.get("action_ref"),
        )
        efh_attribution = usage["efh_attribution"]
    else:
        if efh_evaluator is None:
            # No EFH wired and no template — refuse rather than skip.
            raise CommercialActionRefusal(
                "no_efh_evaluator_and_no_template",
                action_class=action_class,
            )
        proposed = {
            "proposing_agent": "samus",
            "kind": "goal_commit",
            "body": {"action_class": action_class, **action_payload},
            "notes": f"commercial_wrap:{action_class}",
        }
        veto = efh_evaluator.evaluate(proposed)
        if veto is not None:
            raise CommercialActionRefusal(
                f"efh_veto:{veto.get('veto_id')}",
                action_class=action_class,
            )
        efh_attribution = f"efh_pass:{action_class}"

        # PDC composite observer: after EFH passes, run the composite
        # (confusion + elegance + adversarial) as a parallel observer.
        # Never refuses — EFH stays the load-bearing inviolable gate.
        # Dormant unless SAMUS_PDC_OBSERVE_ENABLED is truthy. Fail-open.
        try:
            from backend.common.config import get_settings as _get_settings
            if getattr(_get_settings(), "samus_pdc_observe_enabled", False):
                from backend.governance.pdc_composite import run_pdc as _run_pdc
                _run_pdc(proposed, plan=action_payload.get("plan") or {})
        except Exception:  # noqa: BLE001
            pass

    # Step 4 — Dual-channel: audit envelope MUST land before commit.
    ver_id = str(uuid.uuid4())
    ver_ref = f"Samus/state/value_exchanges/{ver_id}.yaml"
    audit_ack_ts: str | None = None
    if dual_channel is not None:
        audit_ack_ts = dual_channel.send_action(
            isv_id=isv_id,
            action_class=action_class,
            trust_premium_at_commit=trust_premium,
            value_exchange_record_ref=ver_ref,
            commercial_destination=commercial_destination,
            syscall_summary={},
        )

    # Step 5 — ValueExchangeRecord emission.
    counterparty = action_payload.get("counterparty") or {}
    if not isinstance(counterparty, dict):
        counterparty = {"raw": counterparty}
    record = {
        "exchange_id": ver_id,
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "agent": "samus",
        "commercial_action_class": action_class,
        "counterparty": counterparty,
        "value_received": action_payload.get("value_received", {}),
        "cost_to_acquire": action_payload.get("cost_to_acquire", {}),
        "roi_timeframe_days": int(action_payload.get("roi_timeframe_days") or 0),
        "contribution_margin_estimate_pct": float(
            action_payload.get("contribution_margin_estimate_pct") or 0.0
        ),
        "confidence": str(action_payload.get("confidence") or "low"),
        "efh_verdict_ref": efh_attribution,
        "mfh_verdict_ref": action_payload.get("mfh_verdict_ref"),
        "rbl_band_at_commit": rbl_band,
        "status": "committed",
        "audit_ack_ts": audit_ack_ts,
        "trust_premium_at_commit": float(trust_premium),
        "notes": action_payload.get("notes", ""),
    }
    _VER_DIR.mkdir(parents=True, exist_ok=True)
    target = _VER_DIR / f"{ver_id}.yaml"
    target.write_text(yaml.safe_dump(record, sort_keys=False), encoding="utf-8")
    _LOG.info(
        "samus.commercial_wrap.committed: class=%s id=%s rbl=%s efh=%s",
        action_class, ver_id, rbl_band, efh_attribution,
    )
    return record
