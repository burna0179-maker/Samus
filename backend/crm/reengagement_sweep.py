"""Soft-no re-engagement sweep — promotional resurfacing of not_interested prospects.

The canonical call-outcome taxonomy (:mod:`backend.crm.call_outcomes`) treats
``not_interested`` as a SOFT no: the prospect isn't a structural mismatch
(``disqualified``) and hasn't asked us to stop (``do_not_call``), they just
weren't ready on the day of the call. This sweep resurfaces them — but with a
different angle (a promotional/discount package) so we're not just re-sending
the cold opener they already declined.

Cadence + ingress mirror :mod:`backend.portfolio_controller.decay_scan`: scan
the CRM, build a :class:`~backend.cash_engine.models.RevenueTriggerRequest` per
qualifying prospect, present it at the Cash Engine front door
(``review_opportunity``) which throws up the same Codex Gate every revenue
ingress does — so a re-engagement send still requires an operator-authored
Stake Sentence + Codex-clean copy.

Defaults are DORMANT (``samus_reengagement_enabled=False``): nothing fires
until an operator arms the flag. The cash_engine live-send gate
(``cash_engine_live_send_enabled``) still independently governs whether the
queued job actually sends mail vs. parks as a reviewable draft.

What is filtered out
--------------------
* ``do_not_call`` / ``disqualified`` — HARD nos. We filter the scan on
  ``last_outcome=="not_interested"`` to begin with, then re-apply a
  defence-in-depth skip pass in case a malformed row leaked through.
* Prospects still in flight — ``state in {"outreach_sent", "queued",
  "dialing", "in_progress"}`` means we already sent something on a prior
  re-engagement cycle (or the original outreach) and haven't heard back yet.
  Resurfacing them now would double-message; Kelly Zimmerman's parked-on-
  reply CallState (op_f57b3c9a7beb4628b221, first live SendGrid send
  2026-06-22) is the canonical example of why this matters.
* Prospects updated more recently than the cooldown — soft-no requires
  enough time for the prospect's situation to have changed before re-pitching.

Counter ledger
--------------
Each successful enqueue is appended to ``state_path("crm",
"reengagement_queued.jsonl")``. The companion :func:`count_queued_today`
helper drives the forge-ui HUD ``reengagement_queued_today`` counter so the
operator can see the sweep actually working.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from backend.cash_engine.models import RevenueTriggerRequest, RevenueTriggerResult
from backend.common.dates import iso_now
from backend.common.settings import get_settings
from backend.common.state_paths import state_path
from backend.crm import persistence as p
from backend.crm.models import CallState

_LOG = logging.getLogger("samus.crm.reengagement_sweep")

# Soft no — the only outcome this sweep resurfaces. Hard nos are excluded by
# construction (filter the scan on this value) and by the defence-in-depth
# skip pass below.
_SOFT_NO_OUTCOME = "not_interested"
# Defence-in-depth skip list. A row whose last_outcome lands in here is a
# HARD no (we promised the operator + the prospect never to chase them again).
# Mirrors :mod:`backend.crm.call_outcomes` HARD-no taxonomy.
_HARD_NO_OUTCOMES = frozenset({"do_not_call", "disqualified"})
# CallState.state values that mean "we already sent something and are waiting
# on the prospect" — resurfacing now would double-message. Mirrors the
# CallStateValue literal in models.py.
_IN_FLIGHT_STATES = frozenset({
    "outreach_sent", "queued", "dialing", "in_progress",
})

# JSONL ledger path. Each line: {ts, prospect_id, opportunity_id, task_id}.
_LEDGER_DIR = "crm"
_LEDGER_FILE = "reengagement_queued.jsonl"


def _ledger_path() -> Path:
    return state_path(_LEDGER_DIR, _LEDGER_FILE)


def _record_queued(
    *, prospect_id: str, opportunity_id: str, task_id: str,
) -> bool:
    """Append one queued-record to the daily-counter ledger. Best-effort."""
    path = _ledger_path()
    record = {
        "ts": iso_now(),
        "prospect_id": prospect_id,
        "opportunity_id": opportunity_id,
        "task_id": task_id,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except OSError as exc:
        _LOG.warning("reengagement ledger append failed (%s): %s", path, exc)
        return False


def count_queued_today(*, today: str | None = None) -> int:
    """Count queued-records whose ts is on UTC ``today`` (YYYY-MM-DD).

    ``today`` defaults to UTC today. Missing ledger -> 0 (no error). A
    malformed JSON line is skipped silently — the counter must not crash
    the HUD on a partial-write.
    """
    bucket = (today or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d"))[:10]
    path = _ledger_path()
    if not path.exists():
        return 0
    n = 0
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
                ts = str(rec.get("ts", "") or "")[:10]
                if ts == bucket:
                    n += 1
    except OSError as exc:
        _LOG.warning("reengagement ledger read failed (%s): %s", path, exc)
        return 0
    return n


@dataclass
class ReengagementSweepResult:
    """Summary of one re-engagement sweep."""

    scanned: int = 0
    eligible: int = 0
    enqueued: int = 0
    escalated: int = 0
    skipped_in_flight: int = 0
    skipped_hard_no: int = 0
    skipped_within_cooldown: int = 0
    skipped_no_opportunity: int = 0
    cooldown_days: int = 0
    capped: int = 0
    results: list[RevenueTriggerResult] = field(default_factory=list)


def _parse_iso_utc(ts: str) -> _dt.datetime | None:
    """Tolerant parse of an iso_now()-style timestamp to UTC."""
    raw = (ts or "").strip()
    if not raw:
        return None
    try:
        normalised = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        parsed = _dt.datetime.fromisoformat(normalised)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed


def _filter_eq(field_name: str, value: str) -> tuple[str, dict[str, Any], dict[str, str]]:
    """Build a DDB FilterExpression for ``last_outcome == :v``.

    ``last_outcome`` happens not to be a DDB reserved word, but mirroring the
    aliased shape used by :func:`backend.crm.service.list_follow_ups_due`
    keeps the safe_scan helper's expression-attribute-name path exercised
    and future-proofs against schema renames.
    """
    return f"#f = :v", {":v": value}, {"#f": field_name}


def scan_for_reengagement_triggers(
    *,
    cooldown_days: int | None = None,
    max_per_run: int | None = None,
    now: _dt.datetime | None = None,
    crm: Any = None,
    review: Callable[..., RevenueTriggerResult] | None = None,
) -> ReengagementSweepResult:
    """Sweep CallStates, resurface eligible soft-nos through the Cash Engine.

    ``crm`` (CRM service module) and ``review`` (the cash_engine front-door
    handler) are injectable so the sweep is testable in-process with no AWS
    and no real cash_engine queue.

    Returns a :class:`ReengagementSweepResult` with per-bucket counters and
    the full list of :class:`RevenueTriggerResult` verdicts (escalated +
    enqueued alike) so the caller can audit-log the run.

    Dormant by default: if ``samus_reengagement_enabled`` is False the sweep
    short-circuits and returns an empty result. If ``cash_engine_enabled`` is
    False the sweep also short-circuits (the front door would refuse anyway —
    skip the scan entirely).
    """
    settings = get_settings()
    out = ReengagementSweepResult()

    if not getattr(settings, "samus_reengagement_enabled", False):
        _LOG.info("reengagement_sweep skipped: samus_reengagement_enabled is False")
        return out
    if not getattr(settings, "cash_engine_enabled", True):
        _LOG.info("reengagement_sweep skipped: cash_engine_enabled is False")
        return out

    cooldown = int(
        getattr(settings, "samus_reengagement_cooldown_days", 30)
        if cooldown_days is None else cooldown_days
    )
    cap = int(
        getattr(settings, "samus_reengagement_max_per_run", 25)
        if max_per_run is None else max_per_run
    )
    out.cooldown_days = cooldown

    if crm is None:
        from backend.crm import service as crm  # local import: avoid cycle
    if review is None:
        from backend.cash_engine.service import review_opportunity as review

    # The scan limit is the *result* count safe_scan returns after the
    # FilterExpression. We pull a generous batch (cap * 4) so the cooldown +
    # in-flight skip pass below has enough rows to find ``cap`` eligible
    # candidates without re-scanning. _SCAN_PAGE_CAP bounds the underlying
    # pagination so a sparse table can't scan unboundedly.
    scan_limit = max(50, cap * 4)
    fe, vals, names = _filter_eq("last_outcome", _SOFT_NO_OUTCOME)
    items, _trunc, err = p.safe_scan(
        p._call_state_table(), limit=scan_limit,
        filter_expression=fe,
        expression_attribute_values=vals,
        expression_attribute_names=names,
    )
    if err is not None:
        _LOG.warning("reengagement_sweep scan failed: %s", err)
        return out

    now_dt = now or _dt.datetime.now(_dt.timezone.utc)
    cooldown_cutoff = now_dt - _dt.timedelta(days=cooldown)

    for item in items:
        out.scanned += 1
        try:
            state = CallState.model_validate(item)
        except Exception as exc:  # noqa: BLE001 — malformed row
            _LOG.warning("reengagement_sweep skip malformed call_state: %s", exc)
            continue

        # Defence in depth: filter was on not_interested, but if a malformed
        # row leaked through with do_not_call / disqualified, the sweep
        # MUST NOT resurface it.
        if state.last_outcome in _HARD_NO_OUTCOMES:
            out.skipped_hard_no += 1
            continue
        if state.last_outcome != _SOFT_NO_OUTCOME:
            # The filter shouldn't return these, but skip explicitly rather
            # than trusting the table-shim.
            continue

        # Cooldown gate — the soft-no needs time to age.
        updated_dt = _parse_iso_utc(state.updated_at)
        if updated_dt is None or updated_dt > cooldown_cutoff:
            out.skipped_within_cooldown += 1
            continue

        # In-flight gate — a prior outreach is still pending a reply. We
        # would otherwise stomp on Kelly's parked outreach.
        if state.state in _IN_FLIGHT_STATES:
            out.skipped_in_flight += 1
            continue

        # Find the deal. No Opportunity -> nothing to resurface (the soft no
        # was on a discovery call that never opened a deal; not our problem
        # to fix here).
        opp = None
        try:
            opp = crm.get_opportunity_for_prospect(state.prospect_id)
        except Exception as exc:  # noqa: BLE001 — never fail the sweep on one read
            _LOG.warning(
                "reengagement_sweep get_opportunity_for_prospect failed for %s: %s",
                state.prospect_id, exc,
            )
            opp = None
        if opp is None:
            out.skipped_no_opportunity += 1
            continue

        out.eligible += 1

        if out.enqueued >= cap:
            out.capped += 1
            continue

        days_quiet = (now_dt - updated_dt).days
        req = RevenueTriggerRequest(
            prospect_id=state.prospect_id,
            trigger_source="reengagement",
            current_samus_state=(
                f"last_outcome=not_interested "
                f"call_state={state.state} "
                f"days_quiet={days_quiet}"
            ),
            trigger_reason=(
                f"soft-no cooldown {cooldown}d elapsed "
                f"(quiet for {days_quiet}d)"
            ),
        )
        result = review(req, crm=crm)
        out.results.append(result)
        if result.status == "enqueued":
            out.enqueued += 1
            _record_queued(
                prospect_id=state.prospect_id,
                opportunity_id=result.opportunity_id,
                task_id=result.task_id,
            )
        elif result.status == "escalated":
            out.escalated += 1

    _LOG.info(
        "reengagement_sweep: scanned=%d eligible=%d enqueued=%d escalated=%d "
        "skipped_in_flight=%d skipped_hard_no=%d skipped_within_cooldown=%d "
        "skipped_no_opportunity=%d capped=%d cooldown_days=%d",
        out.scanned, out.eligible, out.enqueued, out.escalated,
        out.skipped_in_flight, out.skipped_hard_no, out.skipped_within_cooldown,
        out.skipped_no_opportunity, out.capped, out.cooldown_days,
    )
    return out


__all__ = [
    "ReengagementSweepResult",
    "scan_for_reengagement_triggers",
    "count_queued_today",
]
