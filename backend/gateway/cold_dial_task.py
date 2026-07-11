"""Gateway-internal cold-dial driver — Samus dials its own daily call list in-container.

Design rationale (operator directive; dial-lane dead 5 days, 2026-07-07):
  The live cold-dial pass was fired by a Windows HOST CRON that invoked
  ``backend/voice/dialer.py`` against the daily ``call_list_<date>.csv``. That
  cron was removed 2026-07-03 as part of the "move off Windows tasks / reason
  in-container" initiative — and NOTHING in-container replaced the dial
  EXECUTION. The in-container idle-drive (``cash_engine/idle_production``) does
  run governed campaigns, but its voice lane is *consent-routed*: a cold,
  no-consent Google-Places prospect is routed to a voicemail DRAFT (ADR-002),
  never a live dial (VR-G5 / ``consent_ok``). So the cold prospects on the daily
  call list stopped being dialed the day the cron was removed, and stayed
  un-dialed for five days while the list kept generating fine.

  This module is the in-container replacement for that host cron — the same
  shape as ``control_tick_task`` and ``morning_ritual_task``: an asyncio loop in
  the gateway lifespan that, during the dial window, when autonomous production
  is ARMED, and when the business day is ATTESTED, runs the *governed cold
  dialer* (``dialer.dial_call_list``) against today's call list with
  ``dry_run=False`` — delegating the actual dial to the samus-voice workcell
  (see "Where the dial runs" below).

Adaptive cadence (operator directive 2026-07-07 — "not a hard number"):
  The interval + volume are NOT fixed; they are a sliding heuristic driven by
  Samus's own urgency signal and scaled by what the company can afford — the
  SAME two signals the idle-drive already uses to size production intensity
  (``intensity = min(urgency, affordability)``), so there is ONE urgency monitor,
  no parallel system:

    * URGENCY (``cash_engine.goal_pace.default_urgency_score``, 0..1) DRIVES the
      cadence: more behind on revenue / closer to the goal deadline => shorter
      interval + bigger batches + higher daily ceiling. On/ahead of pace => back
      off. It is the graded sibling of ``behind_pace`` (max of the burn-coverage
      gap and the deadline run-rate gap).
    * AFFORDABILITY (``cash_engine.affordability.assess_affordability`` posture,
      ``conserve``/``lean``/``invest``) SCALES the per-batch + daily VOLUME down
      under tight cash — but never to zero (dialing is the primary revenue lever;
      urgency leads, affordability only trims). The interval itself stays
      urgency-driven.

  Every value maps continuously between a low-urgency FLOOR and a peak-urgency
  ceiling; the ``SAMUS_COLD_DIAL_*`` knobs set those BOUNDS, not a fixed cadence.
  A reputation/compliance floor caps how fast it may ever dial (single caller ID).

Why this reuses ``dialer.dial_call_list`` and NOT ``voice.governed_dial``:
  ``place_governed_dial`` (ADR-016/017) requires a ``consent_ok`` fence and, by
  design, BLOCKS a cold no-consent number under VR-G5 — routing it to a
  voicemail draft instead of a live call. The host cron never went through that
  path; it called ``dialer.dial_call_list`` directly, the TCPA/DNC/cooldown-gated
  *cold* path. This loop restores exactly that path (the operator's proven
  capability), in-container. It does NOT fork a parallel dialer and does NOT
  bypass a single gate: every individual dial is still decided by
  ``dialer.dial_call_list``'s own fences —

    * per-run ``max_calls`` cap (blast radius),
    * TCPA call-hours gate (prospect-local 8–21),
    * DNC / suppression list (fail-CLOSED),
    * warm-prospect exclusion (mid-conversation on a hotter track),
    * per-number cooldown (7–14 day floor),
    * already-called-today (one live dial per prospect per day),
    * scheduled-callback defer + due-callback prepend.

Where the dial runs (gateway decides, voice executes):
  The gateway container has NO Vapi credentials — by design (per-workcell secret
  isolation): the Vapi assistant / number / key live ONLY in the samus-voice
  workcell. So this loop makes the DECISION here (arm + attestation + window +
  urgency + cap — all readable in the gateway) and DELEGATES the execution to
  voice over the signed mesh: a ``POST /voice/dial_call_list`` (see
  :func:`_dial_via_voice`) where ``dialer.dial_call_list`` runs with voice's creds
  and returns its DialRunResult. No creds spread to the gateway, no parallel
  dialer.

Gates (ALL must hold for a tick to dial), each fail-closed toward NOT dialing:
  1. loop armed        — ``SAMUS_COLD_DIAL_LOOP_ENABLED`` (default ON).
  2. production armed  — ``settings.idle_production_drive_enabled`` — the SAME
                         master arm the idle-drive reads (one arm, no parallel).
  3. attested          — ``preshift_attestation.is_attested()`` (ADR-018).
  4. dial window       — a weekday inside ``[HOUR_START, HOUR_END)`` Pacific
                         (default 8–21). The dialer's per-prospect TCPA gate still
                         trims to each prospect's local hours.
  5. daily headroom    — under the URGENCY-SCALED daily ceiling (counted from the
                         dial-run ledgers) — a per-day blast-radius bound on top
                         of the per-run cap and the dialer's once-a-day gate.
  6. call list present — today's ``call_list_<date>.csv`` exists.

Knobs — BOUNDS for the sliding cadence (composable; sane defaults):
  * ``SAMUS_COLD_DIAL_LOOP_ENABLED``        — master arm for THIS loop (default ON)
  * ``SAMUS_COLD_DIAL_INTERVAL_MIN_SEC``    — interval at PEAK urgency (default 600 = 10 min)
  * ``SAMUS_COLD_DIAL_INTERVAL_MAX_SEC``    — interval at ZERO urgency (default 1800 = 30 min)
  * ``SAMUS_COLD_DIAL_RECHECK_SEC``         — poll cadence when NOT dialing (default 600)
  * ``SAMUS_COLD_DIAL_MAX_CALLS_PEAK``      — per-run cap at peak urgency (default 15; dialer hard 50)
  * ``SAMUS_COLD_DIAL_MAX_CALLS_FLOOR``     — per-run cap at zero urgency (default 4)
  * ``SAMUS_COLD_DIAL_DAILY_CAP_PEAK``      — daily live-dial ceiling at peak (default 100)
  * ``SAMUS_COLD_DIAL_DAILY_CAP_FLOOR``     — daily ceiling at zero urgency (default 30)
  * ``SAMUS_COLD_DIAL_HOUR_START`` / ``_END`` — dial window, Pacific 24h (default 8 / 21)
  * ``SAMUS_COLD_DIAL_DELAY_SEC``           — inter-call pacing seconds (default 30)
  * ``SAMUS_COLD_DIAL_PHONE_NUMBER_IDS``    — comma-separated Vapi number pool to
                                              round-robin (default: single number)

The dial pass is synchronous and paces itself between calls, so the loop runs it
in a worker thread (``asyncio.to_thread``) — the gateway event loop is never
blocked while a batch is dialing.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

_LOG = logging.getLogger("samus.gateway.cold_dial_task")

_INITIAL_DELAY_SEC = 150.0  # let boot churn (and the morning ritual attest) settle

ENV_ENABLED = "SAMUS_COLD_DIAL_LOOP_ENABLED"
ENV_INTERVAL_MIN = "SAMUS_COLD_DIAL_INTERVAL_MIN_SEC"
ENV_INTERVAL_MAX = "SAMUS_COLD_DIAL_INTERVAL_MAX_SEC"
ENV_RECHECK = "SAMUS_COLD_DIAL_RECHECK_SEC"
ENV_HOUR_START = "SAMUS_COLD_DIAL_HOUR_START"
ENV_HOUR_END = "SAMUS_COLD_DIAL_HOUR_END"
ENV_MAX_CALLS_PEAK = "SAMUS_COLD_DIAL_MAX_CALLS_PEAK"
ENV_MAX_CALLS_FLOOR = "SAMUS_COLD_DIAL_MAX_CALLS_FLOOR"
ENV_DAILY_CAP_PEAK = "SAMUS_COLD_DIAL_DAILY_CAP_PEAK"
ENV_DAILY_CAP_FLOOR = "SAMUS_COLD_DIAL_DAILY_CAP_FLOOR"
ENV_DELAY = "SAMUS_COLD_DIAL_DELAY_SEC"
ENV_PHONE_IDS = "SAMUS_COLD_DIAL_PHONE_NUMBER_IDS"

_DEFAULT_HOUR_START = 8
_DEFAULT_HOUR_END = 21
_DEFAULT_DELAY_SEC = 30.0

# Adaptive-cadence bounds. Interval slides between the CALM (zero-urgency) and
# PEAK (max-urgency) ends; volume between FLOOR and PEAK. Peak values are the
# operator's "strong but bounded" ceiling (2026-07-07).
_DEFAULT_INTERVAL_MIN = 600.0     # 10 min at peak urgency
_DEFAULT_INTERVAL_MAX = 1800.0    # 30 min at zero urgency
_DEFAULT_RECHECK_SEC = 600.0      # re-check every 10 min when not dialing
_DEFAULT_MAX_CALLS_PEAK = 15
_DEFAULT_MAX_CALLS_FLOOR = 4
_DEFAULT_DAILY_CAP_PEAK = 100
_DEFAULT_DAILY_CAP_FLOOR = 30

# Hard reputation/compliance floor: never dial faster than this, no matter the
# env or urgency — a single caller ID hammered too fast gets spam-flagged. Raise
# throughput safely with a multi-number pool (SAMUS_COLD_DIAL_PHONE_NUMBER_IDS),
# not by dropping this.
_INTERVAL_HARD_FLOOR_SEC = 300.0  # 5 min

# Priority buckets the cold pass targets, in dial order. Mirrors the dialer's
# own default (hot + warm) — "low" is excluded so the pass works qualified leads.
_ONLY_PRIORITIES = ["hot", "warm"]


# ---------------------------------------------------------------------------
# Env helpers (same idiom as morning_ritual_task / control_tick_task)
# ---------------------------------------------------------------------------

def _flag_on(name: str, default_on: bool = True) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default_on
    return raw not in ("0", "false", "no", "off")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _lerp(lo: float, hi: float, t: float) -> float:
    """Linear interpolate lo->hi by t, t clamped to [0, 1]."""
    t = max(0.0, min(1.0, t))
    return lo + (hi - lo) * t


# ---------------------------------------------------------------------------
# Gate inputs — the impure reads, kept out of the pure decision core
# ---------------------------------------------------------------------------

def _production_armed() -> bool:
    """True iff autonomous production is armed — the SAME flag the idle-drive
    reads (``settings.idle_production_drive_enabled``). Read fresh each tick so a
    runtime arm/disarm needs no gateway restart. Fail-closed (disarmed) on any
    settings fault: no arm read, no autonomous dial."""
    try:
        from backend.common.settings import get_settings
        return bool(getattr(get_settings(), "idle_production_drive_enabled", False))
    except Exception as exc:  # noqa: BLE001 — an unreadable arm means DO NOT dial
        _LOG.warning("cold_dial arm read failed — treating as disarmed: %s", exc)
        return False


def _now_local_pt() -> datetime:
    """Current time in the operator's business timezone (America/Los_Angeles).
    Falls back to UTC on any tz fault (a wrong-by-hours window is better than a
    crash; the dialer's per-prospect TCPA gate is the real legal fence)."""
    try:
        from backend.common.us_timezones import state_to_timezone
        return datetime.now(timezone.utc).astimezone(state_to_timezone("CA"))
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc)


def _urgency() -> float:
    """Graded revenue urgency in [0, 1] from Samus's pace monitor
    (``goal_pace.default_urgency_score``). Fail-soft to 0.5 (moderate) on any
    fault so a finance-read glitch never floors or maxes the cadence."""
    try:
        from backend.cash_engine.goal_pace import default_urgency_score
        return max(0.0, min(1.0, float(default_urgency_score())))
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("cold_dial urgency read failed (%s) — moderate 0.5", exc)
        return 0.5


def _affordability_scale() -> float:
    """Volume scale in [0.5, 1.0] from the CODB affordability posture
    (``affordability.assess_affordability().intensity_factor`` — 0.3 conserve /
    0.6 lean / 1.0 invest). Floored at 0.5 so tight cash TRIMS volume but never
    blocks the primary revenue lever (operator: urgency drives, affordability
    scales). Fail-soft to 0.6 (lean) on any fault."""
    try:
        from backend.cash_engine.affordability import assess_affordability
        factor = float(assess_affordability().intensity_factor)
        return max(0.5, min(1.0, factor))
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("cold_dial affordability read failed (%s) — lean 0.6", exc)
        return 0.6


def _phone_number_pool() -> list[str]:
    """Comma-separated Vapi phone-number ids to round-robin, or empty. Empty ⇒
    the dialer falls back to the single ``settings.vapi_phone_number_id``."""
    raw = os.environ.get(ENV_PHONE_IDS, "") or ""
    return [p.strip() for p in raw.split(",") if p.strip()]


def _resolve_call_list_path() -> Optional[Path]:
    """Return today's ``call_list_<date>.csv`` path if it exists, else None.

    Checks BOTH the Pacific business date and the UTC ``date.today()`` name (the
    dialer's own ``default_csv_path_for_today`` convention), so a list written
    under either convention is found — and the found path is what we hand to the
    dialer, so the presence gate and the dial can never disagree on which file.
    Fully defensive: any read fault resolves to None (⇒ "call list missing" ⇒
    no dial this tick)."""
    try:
        from backend.common import storage
        base = storage.root() / "daily_calls"
    except Exception:  # noqa: BLE001
        return None
    names: list[str] = []
    try:
        from backend.common.us_timezones import business_today
        names.append(business_today().isoformat())
    except Exception:  # noqa: BLE001
        pass
    names.append(date.today().isoformat())  # UTC — the dialer's default
    seen: set[str] = set()
    for d in names:
        if d in seen:
            continue
        seen.add(d)
        try:
            p = base / f"call_list_{d}.csv"
            if p.is_file():
                return p
        except Exception:  # noqa: BLE001
            continue
    return None


def _dials_today(now_utc: Optional[datetime] = None) -> int:
    """Count LIVE dials already placed today, from the dial-run ledgers.

    Scans ``<root>/voice/dial_runs/dial_run_<YYYYMMDD>*.json`` for the current
    UTC date (the same date basis + filename convention the dialer's own
    per-prospect ``_already_called_today`` gate uses) and sums attempts whose
    ``outcome == 'initiated'`` (a real placed call — dry-runs and skips do not
    count). Fully defensive: any read fault contributes 0, so a corrupt ledger
    can never *raise* the counter and wrongly suppress dialing."""
    now = now_utc or datetime.now(timezone.utc)
    day_str = now.strftime("%Y%m%d")
    total = 0
    try:
        from backend.common import storage
        runs_dir = storage.root() / "voice" / "dial_runs"
        files = list(runs_dir.glob(f"dial_run_{day_str}*.json"))
    except Exception:  # noqa: BLE001
        return 0
    import json
    for fpath in files:
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for attempt in data.get("attempts", []):
            if (attempt.get("outcome") or "").lower() == "initiated":
                total += 1
    return total


# ---------------------------------------------------------------------------
# Adaptive cadence — urgency drives, affordability scales the volume
# ---------------------------------------------------------------------------

def compute_cadence(urgency: float, aff_scale: float) -> tuple[float, int, int]:
    """Map (urgency, affordability-scale) to ``(interval_sec, max_calls,
    daily_cap)``. Pure given its inputs + the env bounds.

    URGENCY drives everything: interval slides from the CALM end (low urgency)
    to the PEAK end (high urgency); per-run + daily volume slide from FLOOR to
    PEAK. AFFORDABILITY-scale (in [0.5, 1.0]) then multiplies the two VOLUME
    numbers down under tight cash (never the interval, and never below one call).
    All three are hard-bounded (interval never under the reputation floor;
    max_calls never over the dialer's 50 ceiling)."""
    u = max(0.0, min(1.0, urgency))
    af = max(0.0, min(1.0, aff_scale))

    imin = _float_env(ENV_INTERVAL_MIN, _DEFAULT_INTERVAL_MIN)
    imax = _float_env(ENV_INTERVAL_MAX, _DEFAULT_INTERVAL_MAX)
    interval = _lerp(imax, imin, u)                 # u=1 => imin (fast)
    interval = max(_INTERVAL_HARD_FLOOR_SEC, interval)

    mc_peak = _int_env(ENV_MAX_CALLS_PEAK, _DEFAULT_MAX_CALLS_PEAK)
    mc_floor = _int_env(ENV_MAX_CALLS_FLOOR, _DEFAULT_MAX_CALLS_FLOOR)
    max_calls = int(round(_lerp(mc_floor, mc_peak, u) * af))
    max_calls = max(1, min(50, max_calls))          # dialer hard [1, 50]

    dc_peak = _int_env(ENV_DAILY_CAP_PEAK, _DEFAULT_DAILY_CAP_PEAK)
    dc_floor = _int_env(ENV_DAILY_CAP_FLOOR, _DEFAULT_DAILY_CAP_FLOOR)
    daily_cap = int(round(_lerp(dc_floor, dc_peak, u) * af))
    daily_cap = max(max_calls, daily_cap)           # a day is at least one batch

    return interval, max_calls, daily_cap


# ---------------------------------------------------------------------------
# Pure reasoning core — should the cold pass fire on this tick?
# ---------------------------------------------------------------------------

def should_fire_now(
    *,
    armed: bool,
    attested: bool,
    now_local: datetime,
    dials_today: int,
    call_list_present: bool,
    daily_cap: int,
    hour_start: Optional[int] = None,
    hour_end: Optional[int] = None,
) -> tuple[bool, str]:
    """Decide whether to run a cold-dial pass this tick. Pure given its inputs
    (the impure reads + the ``daily_cap`` sizing happen in :func:`run_once`).

    Returns ``(decision, reason)`` — a decision plus a one-line narrative so the
    ops timeline is easy to audit. Order is fail-closed toward NOT dialing: the
    cheapest / hardest gates first, the call-list presence last."""
    if not _flag_on(ENV_ENABLED):
        return False, "cold-dial loop disabled"
    if not armed:
        return False, "production disarmed (idle_production_drive_enabled=False)"
    if not attested:
        return False, "business day not attested — cold pass self-suspended"
    if now_local.weekday() >= 5:
        return False, f"weekend (weekday={now_local.weekday()}) — no cold dialing"

    start = _int_env(ENV_HOUR_START, _DEFAULT_HOUR_START) if hour_start is None else hour_start
    end = _int_env(ENV_HOUR_END, _DEFAULT_HOUR_END) if hour_end is None else hour_end
    hour = now_local.hour
    if hour < start:
        return False, f"before dial window ({hour} < {start} PT)"
    if hour >= end:
        return False, f"after dial window ({hour} >= {end} PT)"

    if daily_cap > 0 and dials_today >= daily_cap:
        return False, f"daily dial cap reached ({dials_today} >= {daily_cap})"

    if not call_list_present:
        return False, "no call_list_<today>.csv (prospecting has not supplied it)"

    return True, (
        f"in dial window {start}-{end} PT, armed + attested, "
        f"{dials_today}/{daily_cap} dialed today"
    )


# ---------------------------------------------------------------------------
# The dial pass — DELEGATE to samus-voice (the Vapi cred-holder); never raise
# ---------------------------------------------------------------------------

def _build_config(*, max_calls: Optional[int] = None, dry_run: bool = False):
    """Build the per-run :class:`DialerConfig`. ``max_calls`` comes from the
    adaptive cadence (clamped to the dialer's hard [1, 50] range); ``dry_run`` is
    False in production. Other fields (pacing, priorities, window, phone pool)
    come from the env."""
    from backend.voice.models import DialerConfig

    mc = _DEFAULT_MAX_CALLS_PEAK if max_calls is None else max_calls
    mc = max(1, min(50, int(mc)))
    delay = _float_env(ENV_DELAY, _DEFAULT_DELAY_SEC)
    delay = max(0.0, min(600.0, delay))
    return DialerConfig(
        max_calls=mc,
        delay_between_calls_s=delay,
        dry_run=dry_run,
        only_priorities=list(_ONLY_PRIORITIES),
        call_hours_start=_int_env(ENV_HOUR_START, _DEFAULT_HOUR_START),
        call_hours_end=_int_env(ENV_HOUR_END, _DEFAULT_HOUR_END),
        phone_number_ids=_phone_number_pool(),
    )


# Every Samus workcell is reachable on the compose network at
# ``http://samus-<name>:8080`` (the gateway's own ``gateway_urls`` all use exactly
# this shape). Voice is simply NOT in the gateway's configured URL set — nothing in
# the gateway called it before the cold-dial loop — so the convention IS the normal
# path here, not an error. Verified reachable: samus-voice:8080/health -> 200.
_DEFAULT_VOICE_URL = "http://samus-voice:8080"


def _voice_url() -> str:
    """Base URL of the samus-voice workcell (holds the Vapi creds + the dialer).

    Prefers an explicit ``VOICE_URL`` env, then ``settings.gateway_urls['voice']``,
    then falls back to the compose-network convention ``http://samus-voice:8080``.
    The gateway does not wire a voice URL by default (it never called voice before
    this loop), so the convention is the normal resolution here, not a failure.
    Never raises — a settings fault just yields the convention."""
    url = (os.environ.get("VOICE_URL") or "").strip()
    if url:
        return url
    try:
        from backend.common.config import get_settings
        url = ((getattr(get_settings(), "gateway_urls", None) or {}).get("voice") or "").strip()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("cold_dial: gateway_urls read failed (%s); using %s",
                     exc, _DEFAULT_VOICE_URL)
        return _DEFAULT_VOICE_URL
    return url or _DEFAULT_VOICE_URL


def _dial_via_voice(csv_path: Path, config: Any) -> dict[str, Any]:
    """Execute ONE dial pass by POSTing to samus-voice ``POST /voice/dial_call_list``
    over the signed mesh, and return the DialRunResult as a dict.

    The gateway has NO Vapi credentials by design (per-workcell secret isolation),
    so the voice workcell — which owns the creds + the dialer — runs
    ``dial_call_list`` and honours every per-prospect fence (TCPA / DNC / cooldown
    / already-called-today / cap). Raises on transport / HTTP error; the caller
    (:func:`_run_dial_pass`) degrades that to a held tick.
    """
    from backend.common.http_client import signed_post_json_sync

    body = {
        "csv_path": str(csv_path) if csv_path else None,
        "config": config.model_dump(),
    }
    # A paced batch runs up to max_calls * delay seconds inside voice; give the
    # sync call generous headroom so a full batch is never cut off mid-dial.
    timeout = float(config.max_calls) * (float(config.delay_between_calls_s) + 20.0) + 60.0
    resp = signed_post_json_sync(_voice_url(), "/voice/dial_call_list", body, timeout=timeout)
    if resp.status_code >= 400:
        raise RuntimeError(
            f"voice /voice/dial_call_list HTTP {resp.status_code}: {(resp.text or '')[:200]}"
        )
    data = resp.json()
    return data if isinstance(data, dict) else {}


def _run_dial_pass(
    csv_path: Path,
    *,
    config: Any = None,
    executor: Optional[Callable[[Path, Any], dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Run ONE governed cold-dial pass over ``csv_path`` by DELEGATING the dial to
    the voice workcell. Never raises — a transport / preflight / dial fault
    degrades to a summary dict with ``error`` set, so the loop keeps looping.

    ``executor(csv_path, config) -> DialRunResult-dict`` is the injection seam:
    production uses :func:`_dial_via_voice` (signed-mesh POST to samus-voice);
    tests inject a fake so a pass can be driven with no running voice service.
    ``config`` overrides the env-built :class:`DialerConfig`.
    """
    cfg = config if config is not None else _build_config()
    do = executor or _dial_via_voice
    try:
        result = do(csv_path, cfg)
    except Exception as exc:  # noqa: BLE001 — a dial fault is a hold, not a crash
        _LOG.warning("cold_dial pass (via voice) faulted: %s", exc)
        return {"ok": False, "error": f"dial_error: {exc}"}

    if not isinstance(result, dict):
        result = {}
    cfg_out = result.get("config")
    dry_run = bool(cfg_out.get("dry_run")) if isinstance(cfg_out, dict) else bool(cfg.dry_run)
    return {
        "ok": True,
        "run_id": result.get("run_id"),
        "csv_path": result.get("csv_path"),
        "dry_run": dry_run,
        "eligible": result.get("eligible_count"),
        "initiated": result.get("initiated_count"),
        "skipped": result.get("skipped_count"),
        "errors": result.get("error_count"),
    }


def run_once(
    *,
    now_local: Optional[datetime] = None,
    executor: Optional[Callable[[Path, Any], dict[str, Any]]] = None,
    urgency: Optional[float] = None,
    aff_scale: Optional[float] = None,
) -> dict[str, Any]:
    """One cold-dial tick: read urgency + affordability → size the cadence →
    decide → (if firing) delegate one dial pass to samus-voice. Synchronous +
    injectable so the asyncio loop can offload it to a thread and tests can drive
    it deterministically. Never raises.

    Returns a summary dict that ALWAYS carries ``next_interval_sec`` — the loop
    sleeps that before the next tick, so the cadence itself is adaptive: the
    urgency-derived interval when a batch just dialed, else a fixed re-check.
    ``now_local`` pins the window gate; ``urgency`` / ``aff_scale`` override the
    live reads (for tests). ``fired`` is True only when a pass ran.
    """
    recheck = _float_env(ENV_RECHECK, _DEFAULT_RECHECK_SEC)
    try:
        from backend.common.preshift_attestation import is_attested

        nl = now_local or _now_local_pt()
        u = _urgency() if urgency is None else max(0.0, min(1.0, float(urgency)))
        af = _affordability_scale() if aff_scale is None else max(0.0, min(1.0, float(aff_scale)))
        interval, max_calls, daily_cap = compute_cadence(u, af)

        csv_path = _resolve_call_list_path()
        dials_today = _dials_today()
        fire, reason = should_fire_now(
            armed=_production_armed(),
            attested=is_attested(),
            now_local=nl,
            dials_today=dials_today,
            call_list_present=csv_path is not None,
            daily_cap=daily_cap,
        )
        base = {
            "urgency": round(u, 3),
            "aff_scale": round(af, 3),
            "max_calls": max_calls,
            "daily_cap": daily_cap,
        }
        if not fire:
            # Not dialing this tick — poll again at the fixed re-check so the loop
            # notices the window opening / arm flip / cap reset promptly.
            return {"fired": False, "reason": reason, "next_interval_sec": recheck, **base}

        # csv_path is guaranteed non-None here (call_list_present gate passed).
        result = _run_dial_pass(csv_path, config=_build_config(max_calls=max_calls), executor=executor)
        # Pace the NEXT batch by the urgency-derived interval.
        return {"fired": True, "reason": reason, "next_interval_sec": interval, **base, **result}
    except Exception as exc:  # noqa: BLE001 — a tick must never crash the loop
        _LOG.exception("cold_dial run_once faulted")
        return {"fired": False, "reason": f"run_once-error: {exc}", "next_interval_sec": recheck}


# ---------------------------------------------------------------------------
# The asyncio loop + lifespan hooks (same shape as control_tick_task)
# ---------------------------------------------------------------------------

async def _cold_dial_loop() -> None:
    """Self-pacing loop: each tick's ``run_once`` returns ``next_interval_sec``
    (the urgency-derived interval after a batch, else a fixed re-check), and the
    loop sleeps exactly that — so the cadence adapts to revenue urgency without a
    restart. Structure mirrors ``control_tick_task._control_tick_loop`` /
    ``morning_ritual_task._morning_ritual_loop``."""
    try:
        await asyncio.sleep(_INITIAL_DELAY_SEC)
    except asyncio.CancelledError:
        raise
    while True:
        interval = _float_env(ENV_RECHECK, _DEFAULT_RECHECK_SEC)
        try:
            # The dial pass is blocking (it paces itself between calls), so run
            # the whole gather→decide→dial tick in a worker thread — the gateway
            # event loop stays responsive while a batch dials.
            result = await asyncio.to_thread(run_once)
            interval = float(result.get("next_interval_sec") or interval)
            if result.get("fired"):
                _LOG.info(
                    "cold_dial fired: %s | urgency=%s aff=%s max_calls=%s daily_cap=%s "
                    "next=%.0fs run_id=%s dry_run=%s initiated=%s skipped=%s errors=%s",
                    result.get("reason"), result.get("urgency"), result.get("aff_scale"),
                    result.get("max_calls"), result.get("daily_cap"), interval,
                    result.get("run_id"), result.get("dry_run"),
                    result.get("initiated"), result.get("skipped"), result.get("errors"),
                )
            else:
                # INFO once per hour boundary so the ops timeline shows the loop
                # is alive without spamming a skip line every re-check.
                hour = _now_local_pt().hour
                if hour != getattr(_cold_dial_loop, "_last_skip_hour", None):
                    _LOG.info("cold_dial skip: %s (urgency=%s next=%.0fs)",
                              result.get("reason"), result.get("urgency"), interval)
                    _cold_dial_loop._last_skip_hour = hour  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — a tick fault never kills the loop
            _LOG.exception("cold_dial_loop tick faulted; continuing")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


async def start_cold_dial_loop(app: Any) -> Optional[asyncio.Task]:
    """Schedule the in-container cold-dial loop. Idempotent. Default ON. The loop
    self-paces (adaptive interval), so there is no fixed interval to configure
    here."""
    if not _flag_on(ENV_ENABLED):
        _LOG.info("cold_dial loop disabled (%s)", ENV_ENABLED)
        return None
    existing = getattr(app.state, "cold_dial_task", None)
    if existing is not None and not existing.done():
        return existing
    task = asyncio.create_task(_cold_dial_loop(), name="samus.cold_dial_loop")
    app.state.cold_dial_task = task
    _LOG.info(
        "cold_dial loop started (adaptive cadence: interval %.0f-%.0fs, "
        "max_calls %d-%d/run, daily %d-%d, urgency-driven)",
        _float_env(ENV_INTERVAL_MIN, _DEFAULT_INTERVAL_MIN),
        _float_env(ENV_INTERVAL_MAX, _DEFAULT_INTERVAL_MAX),
        _int_env(ENV_MAX_CALLS_FLOOR, _DEFAULT_MAX_CALLS_FLOOR),
        _int_env(ENV_MAX_CALLS_PEAK, _DEFAULT_MAX_CALLS_PEAK),
        _int_env(ENV_DAILY_CAP_FLOOR, _DEFAULT_DAILY_CAP_FLOOR),
        _int_env(ENV_DAILY_CAP_PEAK, _DEFAULT_DAILY_CAP_PEAK),
    )
    return task


async def stop_cold_dial_loop(app: Any) -> None:
    """Cancel + await the loop. Idempotent + best-effort."""
    task = getattr(app.state, "cold_dial_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 — teardown swallows
        pass
    app.state.cold_dial_task = None
    _LOG.info("cold_dial loop stopped")


__all__ = [
    "start_cold_dial_loop",
    "stop_cold_dial_loop",
    "run_once",
    "should_fire_now",
    "compute_cadence",
    "ENV_ENABLED",
    "ENV_INTERVAL_MIN",
    "ENV_INTERVAL_MAX",
    "ENV_RECHECK",
    "ENV_HOUR_START",
    "ENV_HOUR_END",
    "ENV_MAX_CALLS_PEAK",
    "ENV_MAX_CALLS_FLOOR",
    "ENV_DAILY_CAP_PEAK",
    "ENV_DAILY_CAP_FLOOR",
    "ENV_DELAY",
    "ENV_PHONE_IDS",
]
