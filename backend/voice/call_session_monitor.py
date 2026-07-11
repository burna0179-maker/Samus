"""Intraday call session monitor — detect failing patterns early.

Samples completed calls throughout the dial session. After every
``check_interval`` calls, compares the intraday running stats against
the morning's strategy brief and fires a mid-day recalculation when
the session is trending poorly.

Design:
  - Reads voice_events.jsonl for today's outbound end_of_call events.
  - Computes a running IntraDayReport (outcome distribution, positive
    rate, objection frequency, consecutive-negative streak).
  - When a trigger fires (streak, outcome-rate collapse, new objection
    spike), calls the strategy reasoner for a mid-session adjustment
    and writes an updated callsheet intel file so the next call in the
    queue picks up the corrected approach.

Trigger conditions (any one fires):
  1. STREAK    — N consecutive non-positive outcomes (default 3)
  2. RATE_DROP — intraday positive rate drops below morning baseline
                 by more than threshold (default 50% relative)
  3. NEW_OBJ   — an objection appears 2+ times today that has no
                 handler in the current callsheet intel

Wiring: called from the Vapi webhook handler after each end_of_call
event when SAMUS_VOICE_SESSION_MONITOR=1. Dormant by default.

Fully offline — all reasoning runs through local_llm.chat().
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from backend.common import storage
from backend.common.dates import iso_now
from backend.common.local_llm import chat as llm_chat

_LOG = logging.getLogger("samus.voice.call_session_monitor")

_ADJUSTMENT_FILE = "voice/session_adjustment.json"
_SESSION_STATE_FILE = "voice/session_state.json"

_EVENTS_PATH_DEFAULT = "/opt/samus/data/voice/voice_events.jsonl"

_POSITIVE_OUTCOMES = frozenset({
    "converted", "warm_lead", "follow_up_scheduled", "call_back_requested",
})
_NEGATIVE_OUTCOMES = frozenset({
    "no_answer", "voicemail_left", "not_interested", "gatekeeper",
    "objection_unresolved", "dnc_requested",
})


def _is_enabled() -> bool:
    return os.getenv("SAMUS_VOICE_SESSION_MONITOR", "0").strip().lower() in ("1", "true", "yes", "on")


# ── Configuration (env-tunable) ───────────────────────────────────

def _streak_threshold() -> int:
    return int(os.getenv("SAMUS_SESSION_STREAK_THRESHOLD", "3"))

def _rate_drop_threshold() -> float:
    return float(os.getenv("SAMUS_SESSION_RATE_DROP_THRESHOLD", "0.5"))

def _min_calls_before_check() -> int:
    return int(os.getenv("SAMUS_SESSION_MIN_CALLS", "3"))

def _cooldown_seconds() -> float:
    return float(os.getenv("SAMUS_SESSION_COOLDOWN_S", "1800"))  # 30 min


# ── Data structures ───────────────────────────────────────────────

@dataclass
class IntraDayReport:
    session_date: str
    total_calls: int = 0
    positive_count: int = 0
    negative_count: int = 0
    positive_rate: float = 0.0
    consecutive_negatives: int = 0
    outcome_distribution: dict[str, int] = field(default_factory=dict)
    objection_counts: dict[str, int] = field(default_factory=dict)
    calls: list[dict] = field(default_factory=list)


@dataclass
class SessionAdjustment:
    generated_ts: str
    trigger: str
    trigger_detail: str
    intraday_report: dict = field(default_factory=dict)
    recommendations: list[str] = field(default_factory=list)
    synthesis: str = ""
    llm_error: str | None = None
    applied_to_callsheet: bool = False


# ── Core monitor ──────────────────────────────────────────────────

def check_session(*, force: bool = False) -> SessionAdjustment | None:
    """Sample today's calls and fire a mid-session adjustment if warranted.

    Returns a SessionAdjustment if a trigger fired, None otherwise.
    Called after each end_of_call webhook. No-ops when disabled or in cooldown.
    """
    if not force and not _is_enabled():
        return None

    # Cooldown: don't fire adjustments more often than every N minutes
    state = _load_session_state()
    now = time.time()
    if not force and state.get("last_adjustment_ts"):
        elapsed = now - state["last_adjustment_ts"]
        if elapsed < _cooldown_seconds():
            _LOG.debug("session_monitor: in cooldown (%.0fs remaining)", _cooldown_seconds() - elapsed)
            return None

    # Build intraday report from today's events
    report = _build_intraday_report()

    if report.total_calls < _min_calls_before_check():
        _LOG.debug("session_monitor: only %d calls today, need %d", report.total_calls, _min_calls_before_check())
        return None

    # Check triggers
    trigger, detail = _evaluate_triggers(report)
    if trigger is None:
        return None

    _LOG.info("session_monitor: trigger fired — %s: %s", trigger, detail)

    # Generate mid-session adjustment
    adjustment = _generate_adjustment(report, trigger, detail)

    # Apply to callsheet if we got recommendations
    if adjustment.recommendations and not adjustment.llm_error:
        _apply_to_callsheet(adjustment)
        adjustment.applied_to_callsheet = True

    # Update cooldown state
    state["last_adjustment_ts"] = now
    state["last_trigger"] = trigger
    state["adjustments_today"] = state.get("adjustments_today", 0) + 1
    _save_session_state(state)

    _persist_adjustment(adjustment)
    return adjustment


def _build_intraday_report() -> IntraDayReport:
    """Read today's outbound end_of_call events from the events ledger."""
    today_str = date.today().isoformat()
    report = IntraDayReport(session_date=today_str)

    events_path = Path(os.getenv("SAMUS_VOICE_EVENTS_PATH", _EVENTS_PATH_DEFAULT))
    if not events_path.exists():
        return report

    today_calls: list[dict] = []
    try:
        for line in events_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            if rec.get("kind") != "end_of_call":
                continue

            ts_raw = rec.get("ts", "")
            if not ts_raw.startswith(today_str):
                continue

            today_calls.append(rec)
    except OSError as exc:
        _LOG.warning("session_monitor: failed to read events: %s", exc)
        return report

    # Sort by timestamp
    today_calls.sort(key=lambda r: r.get("ts", ""))

    report.total_calls = len(today_calls)
    report.calls = today_calls

    # Compute running stats
    consecutive_neg = 0
    outcome_dist: dict[str, int] = {}
    objection_counts: dict[str, int] = {}

    for call in today_calls:
        # Determine outcome from the event record
        outcome = _infer_outcome(call)
        outcome_dist[outcome] = outcome_dist.get(outcome, 0) + 1

        if outcome in _POSITIVE_OUTCOMES:
            report.positive_count += 1
            consecutive_neg = 0
        elif outcome in _NEGATIVE_OUTCOMES:
            report.negative_count += 1
            consecutive_neg += 1

        # Track objections if present
        for obj in call.get("objections", []):
            obj_text = str(obj).lower()[:120]
            objection_counts[obj_text] = objection_counts.get(obj_text, 0) + 1

    report.consecutive_negatives = consecutive_neg
    report.outcome_distribution = outcome_dist
    report.objection_counts = objection_counts
    report.positive_rate = report.positive_count / report.total_calls if report.total_calls else 0.0

    return report


def _infer_outcome(event: dict) -> str:
    """Extract outcome from an end_of_call event record."""
    # The event record may carry outcome directly, or we infer from ended_reason
    outcome = event.get("outcome") or ""
    if outcome and outcome != "(unknown)":
        return outcome

    # Fallback: map ended_reason to outcome
    ended = (event.get("ended_reason") or "").lower()
    if ended in ("no-answer", "machine_detected_silence"):
        return "no_answer"
    if ended in ("voicemail", "machine_end_beep", "machine_end_silence", "machine_end_other"):
        return "voicemail_left"
    if "customer" in ended and "ended" in ended:
        return "not_interested"

    # Check recommended_action from lead summary
    action = (event.get("recommended_action") or "").lower()
    if "follow" in action:
        return "follow_up_scheduled"
    if "book" in action or "convert" in action:
        return "converted"

    return outcome or "no_answer"


def _evaluate_triggers(report: IntraDayReport) -> tuple[str | None, str]:
    """Check trigger conditions. Returns (trigger_name, detail) or (None, "")."""
    streak_thresh = _streak_threshold()

    # T1: Consecutive negative streak
    if report.consecutive_negatives >= streak_thresh:
        return (
            "STREAK",
            f"{report.consecutive_negatives} consecutive non-positive outcomes "
            f"(threshold: {streak_thresh})",
        )

    # T2: Rate collapse vs morning baseline
    from .call_strategy_reasoner import load_strategy_brief
    brief = load_strategy_brief()
    if brief and brief.trend_deltas:
        for delta in brief.trend_deltas:
            if delta.metric == "positive_outcome_rate" and delta.current > 0:
                morning_rate = delta.current
                if report.positive_rate < morning_rate * (1 - _rate_drop_threshold()):
                    return (
                        "RATE_DROP",
                        f"Intraday rate {report.positive_rate:.1%} is below morning "
                        f"baseline {morning_rate:.1%} by >{_rate_drop_threshold():.0%}",
                    )

    # T3: Unhandled objection spike
    from .callsheet_updater import load_dynamic_callsheet_intel
    intel = load_dynamic_callsheet_intel()
    handled_texts = set()
    if intel:
        for h in intel.get("objection_handlers", []):
            handled_texts.add(str(h).lower()[:60])

    for obj_text, count in report.objection_counts.items():
        if count >= 2 and not any(obj_text[:40] in h for h in handled_texts):
            return (
                "NEW_OBJECTION",
                f'Objection "{obj_text}" hit {count}x today with no handler in callsheet',
            )

    return (None, "")


_ADJUSTMENT_SYSTEM = """\
You are a real-time cold-calling coach monitoring a live dial session.
A trigger has fired indicating the current approach is failing.

Produce 2-3 IMMEDIATE, specific adjustments the caller should make
on the VERY NEXT CALL. These are mid-session corrections, not end-of-day
strategy — be concrete and urgent.

Format:
DIAGNOSIS:
<1-2 sentences: what's going wrong right now>

ADJUSTMENTS:
1. <specific change for the next call>
2. <specific change for the next call>
[up to 3]
"""


def _generate_adjustment(
    report: IntraDayReport, trigger: str, detail: str,
) -> SessionAdjustment:
    """Call LM Studio for a mid-session correction."""
    ts = iso_now()

    context_lines = [
        f"TRIGGER: {trigger} — {detail}",
        f"SESSION SO FAR: {report.total_calls} calls, {report.positive_rate:.1%} positive",
        f"Outcomes: {json.dumps(report.outcome_distribution)}",
        f"Consecutive negatives (latest): {report.consecutive_negatives}",
    ]

    if report.objection_counts:
        context_lines.append(f"Objections today: {json.dumps(report.objection_counts)}")

    # Include morning strategy for context
    from .call_strategy_reasoner import load_strategy_brief
    brief = load_strategy_brief()
    if brief and brief.recommendations:
        recs = [r for r in brief.recommendations if not r.startswith("[KEEP]")]
        context_lines.append(f"\nMORNING STRATEGY (not working): {json.dumps(recs[:3])}")

    # Last 3 call summaries
    recent = report.calls[-3:] if report.calls else []
    if recent:
        context_lines.append("\nLAST 3 CALLS:")
        for c in recent:
            context_lines.append(
                f"  {c.get('company', '?')} — {_infer_outcome(c)} "
                f"(ended: {c.get('ended_reason', '?')}, "
                f"dur: {c.get('duration_sec', 0)}s)"
            )

    user_msg = "\n".join(context_lines)

    raw = llm_chat(
        system=_ADJUSTMENT_SYSTEM,
        user=user_msg,
        max_tokens=800,
        temperature=0.2,
        timeout=60.0,
    )

    synthesis = ""
    recommendations: list[str] = []
    llm_error: str | None = None

    if not raw.strip():
        llm_error = "lm_studio_empty_response"
    else:
        synthesis, recommendations = _parse_adjustment_response(raw)

    return SessionAdjustment(
        generated_ts=ts,
        trigger=trigger,
        trigger_detail=detail,
        intraday_report=asdict(report) if report.total_calls <= 50 else {
            "session_date": report.session_date,
            "total_calls": report.total_calls,
            "positive_rate": report.positive_rate,
            "consecutive_negatives": report.consecutive_negatives,
            "outcome_distribution": report.outcome_distribution,
        },
        recommendations=recommendations,
        synthesis=synthesis,
        llm_error=llm_error,
    )


def _parse_adjustment_response(raw: str) -> tuple[str, list[str]]:
    synthesis = ""
    recommendations: list[str] = []

    sections = raw.split("ADJUSTMENTS:")
    if len(sections) >= 2:
        pre = sections[0]
        post = sections[1]

        if "DIAGNOSIS:" in pre:
            synthesis = pre.split("DIAGNOSIS:", 1)[1].strip()
        else:
            synthesis = pre.strip()

        for line in post.strip().splitlines():
            line = line.strip()
            if line and line[0].isdigit() and "." in line[:4]:
                rec_text = line.split(".", 1)[1].strip() if "." in line[:4] else line
                if rec_text:
                    recommendations.append(rec_text)
    else:
        synthesis = raw.strip()[:400]

    return synthesis, recommendations


def _apply_to_callsheet(adjustment: SessionAdjustment) -> None:
    """Merge mid-session adjustments into the live callsheet intel."""
    from .callsheet_updater import load_dynamic_callsheet_intel

    intel = load_dynamic_callsheet_intel()
    if not intel:
        return

    # Add/replace the session_adjustment section
    intel["session_adjustment"] = {
        "generated_ts": adjustment.generated_ts,
        "trigger": adjustment.trigger,
        "diagnosis": adjustment.synthesis,
        "immediate_changes": adjustment.recommendations,
        "calls_at_time_of_adjustment": adjustment.intraday_report.get("total_calls", 0),
    }

    try:
        from backend.common import storage as _storage
        target = _storage.root() / "voice" / "dynamic_callsheet_intel.json"
        target.write_text(
            json.dumps(intel, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        _LOG.info(
            "session_monitor: applied %d adjustments to callsheet (trigger: %s)",
            len(adjustment.recommendations),
            adjustment.trigger,
        )
    except OSError as exc:
        _LOG.warning("session_monitor: callsheet apply failed: %s", exc)

    # ── Patch Vapi assistant system prompt (live, immediate) ──────
    # This is the real-time channel: the callsheet intel file only affects
    # the NEXT call's variableValues (objections, WTLF). But the system
    # prompt controls Morgan's overall behavior — opener style, tone,
    # gatekeeper handling. Patching it here means the very next Vapi call
    # uses the corrected approach without waiting for a full tune cycle.
    _patch_vapi_system_prompt(adjustment)


def _patch_vapi_system_prompt(adjustment: SessionAdjustment) -> None:
    """Append a mid-session directive block to the Vapi assistant's system prompt."""
    try:
        from backend.common.config import get_settings
        settings = get_settings()
        assistant_id = settings.vapi_assistant_id
        api_key = settings.vapi_api_key
        if not assistant_id or not api_key:
            _LOG.debug("session_monitor: no Vapi assistant configured, skipping prompt patch")
            return

        from .client import VapiClient
        client = VapiClient(api_key=api_key)
        assistant = client.get_assistant(assistant_id)
        current_prompt = ""
        try:
            messages = assistant.get("model", {}).get("messages", [])
            for msg in messages:
                if isinstance(msg, dict) and msg.get("role") == "system":
                    current_prompt = str(msg.get("content") or "")
                    break
        except Exception:  # noqa: BLE001
            pass

        if not current_prompt:
            _LOG.warning("session_monitor: could not read current system prompt")
            return

        # Strip any prior mid-session directive block
        marker = "## MID-SESSION ADJUSTMENT"
        if marker in current_prompt:
            current_prompt = current_prompt[:current_prompt.index(marker)].rstrip()

        # Build the directive block
        directive_lines = [
            f"\n\n{marker}",
            f"(auto-generated {adjustment.generated_ts} — trigger: {adjustment.trigger})",
            "",
            adjustment.synthesis,
            "",
            "APPLY THESE CHANGES ON YOUR NEXT CALL:",
        ]
        for i, rec in enumerate(adjustment.recommendations, 1):
            directive_lines.append(f"{i}. {rec}")

        patched_prompt = current_prompt + "\n".join(directive_lines)

        client.patch_assistant_config(assistant_id, system_prompt=patched_prompt)
        _LOG.info("session_monitor: patched Vapi assistant %s system prompt", assistant_id)

    except Exception as exc:  # noqa: BLE001
        _LOG.warning("session_monitor: Vapi prompt patch failed (non-fatal): %s", exc)


def _persist_adjustment(adj: SessionAdjustment) -> None:
    try:
        target = storage.root() / _ADJUSTMENT_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(asdict(adj), indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except OSError as exc:
        _LOG.warning("session_adjustment persist failed: %s", exc)


def _load_session_state() -> dict:
    target = storage.root() / _SESSION_STATE_FILE
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        # Reset if it's a new day
        if data.get("session_date") != date.today().isoformat():
            return {"session_date": date.today().isoformat()}
        return data
    except Exception:  # noqa: BLE001
        return {"session_date": date.today().isoformat()}


def _save_session_state(state: dict) -> None:
    state["session_date"] = date.today().isoformat()
    try:
        target = storage.root() / _SESSION_STATE_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def load_session_adjustment() -> SessionAdjustment | None:
    """Load the last session adjustment."""
    target = storage.root() / _ADJUSTMENT_FILE
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return SessionAdjustment(**data)
    except Exception:  # noqa: BLE001
        return None
