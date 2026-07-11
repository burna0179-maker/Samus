"""Self-supply — starvation-aware input replenishment for the idle-drive.

THE GAP THIS CLOSES (found live 2026-07-03): the idle-production drive decided
``produce`` on every business-hours tick, the campaign portfolio ran — and
initiated ZERO campaigns, every tick, because both seeded campaigns starve on
the same missing input (today's ``daily_calls/call_list_<date>.csv``, normally
built by a host scheduled task that silently skipped when its user had no
interactive session). The drive had no awareness that its actuation yielded
nothing and no way to remediate. That is exactly the external-trigger
dependency the operator directed us to remove: when Samus's reasoning decides
to produce, it must also be able to notice the pantry is empty and RESTOCK IT
ITSELF through its own governed workcells.

Three layers, judgment kept pure and testable (same shape as idle_production):

  - :func:`diagnose_starvation` — PURE. Given a portfolio actuation summary,
    identifies whether production starved and on which inputs.
  - :func:`replenish` — the actuator. For each cause it can self-heal, fires
    the corresponding in-stack producer (call list -> prospecting workcell's
    ``daily_supply`` /work action, ADR-014 path). Bounded by a per-business-day
    attempt cap; every attempt is ledgered.
  - :func:`run_self_supply` — the idle-drive hook. Flag-gated, never raises.
    When attempts exhaust without the input appearing, it ESCALATES (alert
    artifact the operator surfaces) instead of retrying forever.

Wire-not-arm: gated by ``SAMUS_SELF_SUPPLY_ENABLED`` (default OFF). Disarmed,
it still DIAGNOSES and reports starvation on the tick record (awareness is
free); only the replenish actuation is held.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

_LOG = logging.getLogger("samus.cash_engine.self_supply")

# Replenish attempts allowed per input per business day. The prospecting fire
# takes minutes and the control tick re-checks every 30 — three misses in a day
# means something structural is wrong and a human should look.
_MAX_ATTEMPTS_PER_DAY = 3

_LEDGER_REL = ("cash_engine", "self_supply_ledger.jsonl")

# Causes this module knows how to self-heal, mapped to the workcell action
# that produces the missing input.
_REPLENISHABLE = {
    "call_list_missing": {
        "service": "prospecting",
        "action": "daily_supply",
    },
}


@dataclass
class StarvationDiagnosis:
    starved: bool
    causes: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"starved": self.starved, "causes": list(self.causes), "note": self.note}


def _call_list_exists() -> bool:
    try:
        from backend.common import storage
        from backend.common.us_timezones import business_today

        return (storage.root() / "daily_calls" /
                f"call_list_{business_today().isoformat()}.csv").is_file()
    except Exception:  # noqa: BLE001
        return False


def diagnose_starvation(
    actuation: Optional[dict[str, Any]],
    *,
    call_list_exists: Optional[Callable[[], bool]] = None,
) -> StarvationDiagnosis:
    """PURE judgment: did the portfolio starve, and on what?

    Starvation = the portfolio ran, initiated nothing, and skipped at least one
    campaign as ``ineligible``. Cause attribution keys off the shared input the
    seeded campaigns gate on: today's call list (both ``email_outreach`` and
    ``voice_consent_routed`` derive their candidate pool from it).
    """
    if not isinstance(actuation, dict):
        return StarvationDiagnosis(False, note="no actuation to diagnose")
    if actuation.get("channel") != "portfolio":
        return StarvationDiagnosis(False, note="non-portfolio actuation")
    if int(actuation.get("initiated") or 0) > 0:
        return StarvationDiagnosis(False, note="production flowing")

    skipped = actuation.get("skipped") or []
    ineligible = [
        (pair[0] if isinstance(pair, (list, tuple)) and pair else str(pair))
        for pair in skipped
        if isinstance(pair, (list, tuple)) and len(pair) > 1 and pair[1] == "ineligible"
    ]
    if not ineligible:
        return StarvationDiagnosis(False, note="nothing skipped as ineligible")

    exists = (call_list_exists or _call_list_exists)()
    causes: list[str] = []
    if not exists:
        causes.append("call_list_missing")
    note = (
        f"portfolio initiated 0; ineligible={ineligible}"
        + ("" if causes else "; call list present — starvation cause outside self-supply scope")
    )
    return StarvationDiagnosis(True, causes=causes, note=note)


def diagnose_pool_exhaustion(actuation: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """PURE: detect a DRY PRODUCTION POOL — the list exists but yields nothing.

    Distinct from starvation (missing input): here campaigns RAN and the
    email batch considered its hot rows, but every one was suppressed
    (already contacted) and nothing new sent — the 7/03 stall mode. Reads
    the enriched email tally (production_actuator surfaces the batch
    ledger's day counts). Returns an exhaustion record or None. Detection
    only: expanding supply scope (new verticals / next geo ring) costs real
    Places + LLM spend, so remediation stays a reasoned, operator-visible
    escalation rather than a blind auto-fire.
    """
    if not isinstance(actuation, dict) or actuation.get("channel") != "portfolio":
        return None
    results = actuation.get("results") or {}
    email = results.get("email_outreach") or {}
    tally = email.get("day_tally") or {}
    if not isinstance(tally, dict) or not tally:
        return None
    sent = int(tally.get("sent", 0) or 0)
    suppressed = int(tally.get("suppressed", 0) or 0)
    if sent == 0 and suppressed > 0:
        return {
            "exhausted": True,
            "sent_today": sent,
            "suppressed_today": suppressed,
            "message": (
                "hot email pool is DRY: every qualified prospect today was "
                "already contacted (suppression). New sends require fresh "
                "supply — expand industries on the current geo ring, advance "
                "the ring, or lower the hot threshold."
            ),
        }
    return None


# ---------------------------------------------------------------------------
# Attempt ledger — bounded retries, durable across restarts.
# ---------------------------------------------------------------------------
def _ledger_path():
    from backend.common import storage

    p = storage.root().joinpath(*_LEDGER_REL)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _business_day() -> str:
    try:
        from backend.common.us_timezones import business_today

        return business_today().isoformat()
    except Exception:  # noqa: BLE001
        from datetime import date

        return date.today().isoformat()


def _attempts_today(cause: str) -> int:
    day = _business_day()
    count = 0
    try:
        path = _ledger_path()
        if not path.is_file():
            return 0
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if row.get("day") == day and row.get("cause") == cause \
                    and row.get("kind") == "replenish_attempt":
                count += 1
    except Exception:  # noqa: BLE001
        return 0
    return count


def _append_ledger(row: dict[str, Any]) -> None:
    try:
        from backend.common.dates import iso_now

        row = {"ts": iso_now(), "day": _business_day(), **row}
        with _ledger_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 — ledger fault never blocks the drive
        _LOG.warning("self-supply ledger append failed: %s", exc)


def _write_escalation(cause: str, attempts: int, message: str | None = None) -> None:
    """Operator-visible alert artifact (audit_alerts pattern): self-heal is
    exhausted for today; a human needs to look at the producing workcell."""
    try:
        from backend.common import storage
        from backend.common.dates import iso_now

        d = storage.root() / "cash_engine" / "supply_alerts"
        d.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": iso_now(),
            "kind": "self_supply_exhausted",
            "cause": cause,
            "attempts_today": attempts,
            "message": message or (
                f"Self-supply exhausted {attempts} replenish attempts for "
                f"'{cause}' today and the input still has not appeared. "
                "Production is starved — check the prospecting workcell "
                "(Places quota/key, geo-ring state, container logs)."
            ),
        }
        (d / f"alert_{_business_day()}_{cause}.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8",
        )
        _LOG.error("SELF-SUPPLY EXHAUSTED for %s (%d attempts today) — operator alert written",
                   cause, attempts)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("self-supply escalation write failed: %s", exc)


def _default_dispatch(service: str, action: str) -> dict[str, Any]:
    """Fire the in-stack producer over the signed mesh. Returns its ACK."""
    from backend.common.config import get_settings
    from backend.common.http_client import signed_post_json_sync
    import os as _os
    import uuid

    url = _os.environ.get(f"{service.upper()}_URL") or (
        get_settings().gateway_urls or {}
    ).get(service)
    if not url:
        raise RuntimeError(f"{service}_url_unset")
    envelope = {
        "task_id": f"self-supply-{uuid.uuid4().hex[:12]}",
        "payload": {},
        "metadata": {"action": action, "source": "cash_engine.self_supply"},
    }
    resp = signed_post_json_sync(url, "/work", envelope, timeout=20.0)
    if resp.status_code >= 400:
        raise RuntimeError(f"{service}_http_{resp.status_code}: {(resp.text or '')[:120]}")
    return resp.json()


def replenish(
    causes: list[str],
    *,
    dispatch: Optional[Callable[[str, str], dict[str, Any]]] = None,
    max_attempts: int = _MAX_ATTEMPTS_PER_DAY,
) -> dict[str, Any]:
    """Fire the in-stack producer for each self-healable cause, capped per day.

    Never raises. Per cause the outcome is one of: ``dispatched`` (with the
    producer's ACK), ``attempts_exhausted`` (escalation written), ``error``,
    or ``not_replenishable``.
    """
    out: dict[str, Any] = {}
    do_dispatch = dispatch or _default_dispatch
    for cause in causes:
        spec = _REPLENISHABLE.get(cause)
        if spec is None:
            out[cause] = {"outcome": "not_replenishable"}
            continue
        attempts = _attempts_today(cause)
        if attempts >= max_attempts:
            _write_escalation(cause, attempts)
            out[cause] = {"outcome": "attempts_exhausted", "attempts_today": attempts}
            continue
        try:
            ack = do_dispatch(spec["service"], spec["action"])
            _append_ledger({
                "kind": "replenish_attempt", "cause": cause,
                "service": spec["service"], "action": spec["action"],
                "ack": ack,
            })
            out[cause] = {"outcome": "dispatched", "ack": ack,
                          "attempt": attempts + 1}
            _LOG.info("self-supply dispatched %s.%s for %s (attempt %d)",
                      spec["service"], spec["action"], cause, attempts + 1)
        except Exception as exc:  # noqa: BLE001
            _append_ledger({
                "kind": "replenish_attempt", "cause": cause,
                "service": spec["service"], "action": spec["action"],
                "error": str(exc),
            })
            out[cause] = {"outcome": "error", "error": str(exc)}
            _LOG.warning("self-supply dispatch failed for %s: %s", cause, exc)
    return out


def _enabled() -> bool:
    """Canonical runtime-flag idiom (matches prospecting cadence): a persisted
    operator flip wins, else the boot-time Settings field (binds
    SAMUS_SELF_SUPPLY_ENABLED, default OFF), else the raw env as last resort."""
    try:
        from backend.common.config import get_settings
        from backend.common.flags.runtime import is_enabled

        fallback = bool(getattr(get_settings(), "self_supply_enabled", False))
        return is_enabled("self_supply_enabled", fallback)
    except Exception:  # noqa: BLE001
        return (os.environ.get("SAMUS_SELF_SUPPLY_ENABLED", "") or "").strip().lower() \
            in ("1", "true", "yes", "on")


def run_self_supply(
    actuation: Optional[dict[str, Any]],
    *,
    dispatch: Optional[Callable[[str, str], dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Idle-drive hook: diagnose -> (if armed) replenish. Never raises.

    Diagnosis always runs and is always reported — starvation AWARENESS costs
    nothing and belongs on every tick record. Actuation is flag-gated.
    """
    try:
        diagnosis = diagnose_starvation(actuation)
    except Exception as exc:  # noqa: BLE001
        return {"ok": True, "starved": False, "error": f"diagnose-error: {exc}"}

    summary: dict[str, Any] = {"ok": True, **diagnosis.to_dict()}

    # Pool-exhaustion awareness (detection + escalation only — see
    # diagnose_pool_exhaustion). Ledgered + alerted once per business day so
    # the operator and the EOD review both see WHY sends flatlined.
    try:
        exhaustion = diagnose_pool_exhaustion(actuation)
        if exhaustion:
            summary["pool_exhaustion"] = exhaustion
            day = _business_day()
            marker_logged = False
            try:
                path = _ledger_path()
                if path.is_file():
                    for line in path.read_text(encoding="utf-8").splitlines():
                        try:
                            row = json.loads(line)
                        except Exception:  # noqa: BLE001
                            continue
                        if row.get("day") == day and row.get("kind") == "pool_exhaustion":
                            marker_logged = True
                            break
            except Exception:  # noqa: BLE001
                pass
            if not marker_logged:
                _append_ledger({"kind": "pool_exhaustion", **exhaustion})
                _write_escalation("hot_pool_exhausted", 0,
                                  message=exhaustion.get("message"))
    except Exception as exc:  # noqa: BLE001
        summary["pool_exhaustion_error"] = str(exc)

    if not diagnosis.starved or not diagnosis.causes:
        return summary

    enabled = _enabled()
    summary["enabled"] = enabled
    if not enabled:
        summary["note"] = (diagnosis.note + "; self-supply DISARMED — replenish held").strip("; ")
        _LOG.warning("production starved (%s) but self-supply is disarmed", diagnosis.causes)
        return summary

    try:
        summary["replenish"] = replenish(diagnosis.causes, dispatch=dispatch)
    except Exception as exc:  # noqa: BLE001
        summary["error"] = f"replenish-error: {exc}"
    return summary
