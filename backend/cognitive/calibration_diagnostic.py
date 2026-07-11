"""Cognitive Observer -- confidence-calibration + loop-completion diagnostic.

Closes two P1 blind spots the framework agent identified in the Samus stack:

  G1 (calibration OPEN)     Every autonomous choice mints a
                            ``DecisionRecord`` with ``confidence`` and
                            ``expected_outcome`` -- but nothing joins those
                            forecasts back to realised business events.
                            Samus can predict "high-confidence booked meeting"
                            and never know it was wrong.

  G2 (loop-completion UNMEASURED)
                            No metric asks whether the reasoning loop that
                            emitted a decision ever REACHED reality (i.e.
                            whether any downstream outcome event shares its
                            correlation ids). Proof: EOD was dark 4 nights,
                            silently degrading, undetected until manual
                            observation.

This module is READ-ONLY over the existing telemetry spine
(``backend.common.business_events`` + ``backend.common.decision_record``):
no schema change, no new emit path, zero side effects on the decision path.
It is scored offline by the Cognitive Observer loop
(``backend.gateway.cognitive_observer_task``) and lands as a per-day artifact
under ``storage.root()/cognition/calibration_report_<date>.json``.

GRACEFUL DEGRADATION
--------------------
Both public functions guarantee they never raise to callers; a telemetry
read fault logs a warning and returns a sensible empty. This mirrors
``record_decision`` / ``emit_business_event`` -- diagnostic instrumentation
must never break the system it is observing.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

_LOG = logging.getLogger("samus.cognitive.calibration_diagnostic")

# --- Tuning knobs (module-level constants -- greppable) --------------------

_DEFAULT_WINDOW_DAYS: Final[int] = 14

# Confidence buckets [low, high) except the top bucket which is [0.8, 1.0].
# Same shape as calibration-plot convention: 5 equal bins across [0, 1].
_BUCKETS: Final[tuple[tuple[float, float, str], ...]] = (
    (0.0, 0.2, "0.0-0.2"),
    (0.2, 0.4, "0.2-0.4"),
    (0.4, 0.6, "0.4-0.6"),
    (0.6, 0.8, "0.6-0.8"),
    (0.8, 1.0, "0.8-1.0"),
)

# Keyword -> outcome-event-type mapping. Cheap intent parser over the free-form
# ``expected_outcome`` string every DecisionRecord carries. Kept module-level
# so an operator can grep the mapping without reading code paths. A decision
# whose expected_outcome matches NO keyword is counted as "unscoreable" and
# reported as a fraction -- never dropped, so the mapping's coverage is
# visible in the diagnostic itself.
_INTENT_MAP: Final[dict[str, tuple[str, ...]]] = {
    "paid": ("payment.received",),
    "payment": ("payment.received",),
    "booked": ("meeting.booked",),
    "meeting": ("meeting.booked",),
    "signed": ("contract.sent", "customer.retained"),
    "contract": ("contract.sent", "customer.retained"),
    "close": ("contract.sent", "customer.retained"),
    "open": ("email.opened",),
    "opened": ("email.opened",),
    "click": ("email.clicked",),
    "clicked": ("email.clicked",),
    "reply": ("call.answered",),
    "answered": ("call.answered",),
}

# The outcome event types that count as "the loop reached reality" for G2
# loop-completion accounting -- everything that isn't itself a decision.made
# or an experiment.assigned (both are internal cognition events, not
# revenue-journey outcomes).
_OUTCOME_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "lead.created",
        "lead.enriched",
        "email.sent",
        "email.opened",
        "email.clicked",
        "call.placed",
        "call.answered",
        "meeting.booked",
        "proposal.sent",
        "contract.sent",
        "invoice.sent",
        "payment.received",
        "customer.retained",
        "customer.churned",
    }
)


# --- Small helpers ---------------------------------------------------------


def _iso_utc(dt: datetime) -> str:
    """Same shape as ``dates.iso_now()`` (Z-suffixed UTC)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_until(until_iso: str | None) -> datetime:
    """Parse ``until_iso`` (Z-suffixed); default to now(UTC). Never raises."""
    if not until_iso:
        return datetime.now(timezone.utc)
    try:
        raw = until_iso.replace("Z", "+00:00")
        return datetime.fromisoformat(raw).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


def _bucket_for(confidence: float) -> str:
    """Map a confidence in [0, 1] to a bucket label. Clamps out-of-range."""
    try:
        c = float(confidence)
    except (TypeError, ValueError):
        c = 0.0
    if c < 0.0:
        c = 0.0
    if c > 1.0:
        c = 1.0
    for lo, hi, label in _BUCKETS:
        # Top bucket is inclusive on the right so 1.0 lands somewhere.
        if label == "0.8-1.0":
            if lo <= c <= hi:
                return label
        elif lo <= c < hi:
            return label
    return _BUCKETS[-1][2]


def _intent_targets(expected_outcome: str) -> tuple[str, ...]:
    """Return the outcome event-types this expected_outcome intends, or ().

    Empty tuple = unscoreable (no keyword matched). Case-insensitive whole-
    substring match; whichever keyword hits first wins so the caller sees a
    stable target set for that decision.
    """
    text = (expected_outcome or "").lower()
    if not text:
        return ()
    seen: list[str] = []
    for keyword, targets in _INTENT_MAP.items():
        if keyword in text:
            for t in targets:
                if t not in seen:
                    seen.append(t)
    return tuple(seen)


def _extract_decision_record(event: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the embedded DecisionRecord out of a decision.made event.

    Mirrors ``decision_record._extract_record`` but is duplicated here
    intentionally: (a) this module is read-only over the telemetry spine and
    should not couple to the emitter's internals beyond the well-known
    metadata key, (b) it only cares about full records (thin approval-
    lifecycle syntheses have no confidence/expected_outcome to score).
    """
    if not isinstance(event, dict):
        return None
    meta = event.get("metadata") or {}
    if not isinstance(meta, dict):
        return None
    embedded = meta.get("decision_record")
    if not isinstance(embedded, dict) or not embedded.get("decision_id"):
        return None
    return embedded


def _correlation_keys(rec_or_event: dict[str, Any]) -> tuple[str, str, str]:
    """(prospect_id, opportunity_id, campaign_id) as strings ('' when absent)."""
    return (
        str(rec_or_event.get("prospect_id") or ""),
        str(rec_or_event.get("opportunity_id") or ""),
        str(rec_or_event.get("campaign_id") or ""),
    )


# --- Public API ------------------------------------------------------------


def reconcile_decisions(
    *,
    window_days: int = _DEFAULT_WINDOW_DAYS,
    until_iso: str | None = None,
) -> dict[str, Any]:
    """Score decisions against realised outcomes over a trailing window.

    Reads ``decision.made`` events from ``[until - window_days, until]`` via
    :func:`backend.common.business_events.read_events`, then for each decision
    scans the same window for downstream outcome events that share ANY of its
    correlation ids (prospect / opportunity / campaign -- first non-empty id
    wins the join). Computes:

      * per-actor confidence-bucket hit rates (G1),
      * per-actor loop-completion rate  (G2 proxy -- did any outcome event
        ever appear on this decision's correlation id?),
      * an overall summary + the unscoreable fraction (decisions whose
        expected_outcome matched none of :data:`_INTENT_MAP`).

    The join is a WITHIN-window heuristic, not a causal proof: a decision
    fired at day 0 and a payment received at day 13 both fall in the window
    and share a prospect_id -> counted as a hit. That's the right resolution
    for a nightly forecast-vs-reality signal; sharper attribution can layer
    on later.

    NEVER raises: any read fault logs a warning and returns an empty scaffold.
    """
    end = _parse_until(until_iso)
    try:
        span_days = max(1, int(window_days))
    except (TypeError, ValueError):
        span_days = _DEFAULT_WINDOW_DAYS
    start = end - timedelta(days=span_days)
    since_iso = _iso_utc(start)
    end_iso = _iso_utc(end)
    scaffold: dict[str, Any] = {
        "window": {"start": since_iso, "end": end_iso, "days": span_days},
        "overall": {
            "decisions": 0,
            "unscoreable": 0,
            "loop_completion_rate": 0.0,
        },
        "per_actor": {},
    }
    try:
        from backend.common.business_events import read_events
    except Exception as exc:  # noqa: BLE001 -- telemetry import fault, degrade
        _LOG.warning("calibration reconcile: business_events import failed: %s", exc)
        return scaffold

    try:
        decision_events = read_events(
            since=since_iso,
            event_types=["decision.made"],
            limit=100_000,
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("calibration reconcile: read decisions failed: %s", exc)
        return scaffold

    try:
        outcome_events = read_events(
            since=since_iso,
            event_types=sorted(_OUTCOME_EVENT_TYPES),
            limit=100_000,
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("calibration reconcile: read outcomes failed: %s", exc)
        outcome_events = []

    # Filter to <= end (read_events has no upper bound).
    def _in_range(ts: Any) -> bool:
        s = str(ts or "")
        return since_iso <= s <= end_iso

    decision_events = [e for e in decision_events if _in_range(e.get("ts"))]
    outcome_events = [e for e in outcome_events if _in_range(e.get("ts"))]

    # Build correlation-id -> {event_types set} indexes so the per-decision
    # scan is O(k), not O(decisions * outcomes).
    idx_prospect: dict[str, set[str]] = {}
    idx_opportunity: dict[str, set[str]] = {}
    idx_campaign: dict[str, set[str]] = {}
    for oe in outcome_events:
        etype = str(oe.get("event_type") or "")
        if not etype:
            continue
        p, o, c = _correlation_keys(oe)
        if p:
            idx_prospect.setdefault(p, set()).add(etype)
        if o:
            idx_opportunity.setdefault(o, set()).add(etype)
        if c:
            idx_campaign.setdefault(c, set()).add(etype)

    # Per-actor accumulator.
    class _ActorAgg:
        __slots__ = ("decisions", "loop_completed", "buckets")

        def __init__(self) -> None:
            self.decisions = 0
            self.loop_completed = 0
            # bucket label -> {"count": int, "hits": int}
            self.buckets: dict[str, dict[str, int]] = {
                label: {"count": 0, "hits": 0} for _, _, label in _BUCKETS
            }

    per_actor: dict[str, _ActorAgg] = {}
    total_decisions = 0
    total_unscoreable = 0
    total_loop_completed = 0

    for ev in decision_events:
        rec = _extract_decision_record(ev)
        if rec is None:
            # A thin decision.made without an embedded record can't be scored
            # for calibration -- skip it entirely from BOTH accounting so we
            # don't skew loop-completion with events that never carried a
            # forecast in the first place.
            continue
        actor = str(rec.get("actor") or "unknown") or "unknown"
        # Correlation ids: prefer the decision record's own, fall back to the
        # carrying event (decision_record._extract_record already mirrors,
        # but we can't assume the raw stream went through that path).
        p, o, c = _correlation_keys(rec)
        if not (p or o or c):
            p, o, c = _correlation_keys(ev)
        expected = str(rec.get("expected_outcome") or "")
        confidence = rec.get("confidence")

        # Loop-completion: ANY outcome event on ANY of the correlation ids.
        outcome_types: set[str] = set()
        if p:
            outcome_types |= idx_prospect.get(p, set())
        if o:
            outcome_types |= idx_opportunity.get(o, set())
        if c:
            outcome_types |= idx_campaign.get(c, set())
        loop_completed = bool(outcome_types)

        agg = per_actor.setdefault(actor, _ActorAgg())
        agg.decisions += 1
        if loop_completed:
            agg.loop_completed += 1
        total_decisions += 1
        if loop_completed:
            total_loop_completed += 1

        targets = _intent_targets(expected)
        if not targets:
            total_unscoreable += 1
            # Unscoreable decisions still count in the bucket totals (for
            # per-bucket volume) but never as hits.
            bucket = _bucket_for(confidence if confidence is not None else 0.0)
            agg.buckets[bucket]["count"] += 1
            continue

        bucket = _bucket_for(confidence if confidence is not None else 0.0)
        agg.buckets[bucket]["count"] += 1
        # Hit = ANY intended outcome-type appears in the joined outcome set.
        if outcome_types & set(targets):
            agg.buckets[bucket]["hits"] += 1

    # Emit shape.
    def _rate(hits: int, count: int) -> float:
        if count <= 0:
            return 0.0
        return round(hits / count, 4)

    per_actor_out: dict[str, Any] = {}
    for actor, agg in per_actor.items():
        per_actor_out[actor] = {
            "decisions": agg.decisions,
            "loop_completion_rate": _rate(agg.loop_completed, agg.decisions),
            "calibration": [
                {
                    "bucket": label,
                    "count": agg.buckets[label]["count"],
                    "hits": agg.buckets[label]["hits"],
                    "hit_rate": _rate(agg.buckets[label]["hits"], agg.buckets[label]["count"]),
                }
                for _, _, label in _BUCKETS
            ],
        }

    scaffold["overall"] = {
        "decisions": total_decisions,
        "unscoreable": total_unscoreable,
        "loop_completion_rate": _rate(total_loop_completed, total_decisions),
    }
    scaffold["per_actor"] = per_actor_out
    return scaffold


def write_calibration_report(day: str) -> Path:
    """Compute today's diagnostic and persist it under ``storage.root()``.

    Idempotent: overwrites the day's report atomically. Never raises: on any
    fault logs a warning and returns the intended path (whose ``.is_file()``
    the caller can check).
    """
    day = (day or "").strip()
    if not day:
        day = datetime.now(timezone.utc).date().isoformat()

    # Late import so the module stays importable in test envs that stub
    # storage after import.
    try:
        from backend.common import storage

        base = storage.root() / "cognition"
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("calibration write: storage.root() failed: %s", exc)
        # Best-effort fallback: current dir. The Path is still returned so
        # the caller sees the intended location in logs.
        base = Path("cognition")

    path = base / f"calibration_report_{day}.json"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _LOG.warning("calibration write: mkdir failed at %s: %s", base, exc)
        return path

    try:
        report = reconcile_decisions()
    except Exception as exc:  # noqa: BLE001 -- reconcile is already fail-soft
        _LOG.warning("calibration write: reconcile faulted: %s", exc)
        report = {
            "window": {"start": "", "end": "", "days": _DEFAULT_WINDOW_DAYS},
            "overall": {"decisions": 0, "unscoreable": 0, "loop_completion_rate": 0.0},
            "per_actor": {},
        }
    report["day"] = day
    report["written_at"] = _iso_utc(datetime.now(timezone.utc))

    try:
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as exc:
        _LOG.warning("calibration write: persist failed at %s: %s", path, exc)
    return path


__all__ = [
    "reconcile_decisions",
    "write_calibration_report",
    "_INTENT_MAP",
    "_DEFAULT_WINDOW_DAYS",
]
