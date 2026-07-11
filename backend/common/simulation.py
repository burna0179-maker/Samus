"""Mandatory simulation stage — dry-run predicted effect before external action.

HOTL Tranche 5, deliverable 1. The autonomy cycle gains a SIMULATE phase
between plan and execute: before an action with an EXTERNAL EFFECT (email send,
voice call, payment link, publish) actually fires, it is run in dry-run mode to
produce a *predicted* effect + cost. The prediction is recorded against the
action's ``decision_id`` so the gateway dispatch gate can refuse any
external-effect action that never went through simulation — a hard,
audit-backed "look before you leap".

WHAT COUNTS AS AN EXTERNAL EFFECT
---------------------------------
:data:`EXTERNAL_EFFECT_ACTIONS` — the closed set of action identifiers that
leave the building (reach a real prospect / move money / publish). Everything
else (internal scoring, planning, telemetry) is NOT gated: simulating a pure
in-process compute step would be theatre.

REGISTRY
--------
Simulations persist to a small JSONL registry (durable across the dispatch
that consumes them — an in-process dict would forget a simulation recorded by
a different worker/process). ``record`` appends; ``get`` / ``has`` read the
most recent simulation for a ``decision_id``. Same env-overridable durable
pattern as :mod:`backend.common.send_ramp` / :mod:`backend.common.daily_counter`.

GRACEFUL DEGRADATION
--------------------
``simulate_action`` never raises — an unknown action type yields a
conservative "unknown effect, unknown cost, would_succeed unknown" result
(which still records, so the gate sees *a* simulation). Registry I/O is
best-effort; a read failure means ``has`` returns False (fail-CLOSED for the
gate: no proof of simulation -> refuse).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .dates import iso_now
from .state_paths import state_path

_LOG = logging.getLogger("samus.simulation")

ENV_LEDGER = "SAMUS_SIMULATION_LEDGER_PATH"

_LOCK = threading.Lock()

# Closed set of external-effect action identifiers. Matched against a
# dispatch/plan-step action name. Kept small + explicit so the gate never
# over-fires on an internal compute step. Extend deliberately as new
# building-leaving actions are wired.
EXTERNAL_EFFECT_ACTIONS: frozenset[str] = frozenset(
    {
        "send_message",  # outreach — email/voicemail send
        "outreach_send",  # outreach — alt name at some call sites
        "initiate_call",  # voice — Vapi outbound dial
        "voice_dial",  # voice — alt name
        "payment_link",  # finance — create a payable link
        "create_invoice",  # finance — issue an invoice
        "publish",  # website / social — publish live
        "publish_post",  # social — publish a post
        "cash_engine_step",  # cash_engine — advances a revenue sequence
    }
)


def is_external_effect(action: str | None) -> bool:
    """True when ``action`` is in the closed external-effect set."""
    return bool(action) and action in EXTERNAL_EFFECT_ACTIONS


@dataclass
class SimulationResult:
    """Predicted effect of one action, produced by its dry-run path."""

    decision_id: str
    action: str
    target: str = ""
    would_succeed: bool | None = None  # None = unknown (unmodelled)
    predicted_cost_usd: float = 0.0
    predicted_effect: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    ts: str = ""

    def to_record(self) -> dict[str, Any]:
        d = asdict(self)
        if not d.get("ts"):
            d["ts"] = iso_now()
        return d


# ---------------------------------------------------------------------------
# Durable registry
# ---------------------------------------------------------------------------


def ledger_path() -> Path:
    explicit = os.environ.get(ENV_LEDGER, "").strip()
    if explicit:
        return Path(explicit)
    return state_path("simulation", "simulations.jsonl")


def record(result: SimulationResult) -> dict[str, Any]:
    """Append a simulation result to the registry. Returns the record dict."""
    rec = result.to_record()
    path = ledger_path()
    with _LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except Exception as exc:  # noqa: BLE001 — record is best-effort
            _LOG.warning("simulation record failed (%s)", exc)
    return rec


def _read_rows() -> list[dict]:
    path = ledger_path()
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError as exc:
        _LOG.warning("simulation ledger read failed (%s)", exc)
        return []
    return rows


def get(decision_id: str) -> dict | None:
    """Most recent recorded simulation for ``decision_id`` (or None)."""
    if not decision_id:
        return None
    match: dict | None = None
    for row in _read_rows():
        if row.get("decision_id") == decision_id:
            match = row  # keep scanning — last write wins
    return match


def has(decision_id: str) -> bool:
    """Whether a simulation was recorded for ``decision_id`` (fail-closed)."""
    return get(decision_id) is not None


# ---------------------------------------------------------------------------
# Dry-run action simulators
# ---------------------------------------------------------------------------


def _simulate_send(payload: dict[str, Any]) -> tuple[bool | None, float, dict, str]:
    """Predict an outreach send: deliverable iff a recipient is present."""
    to = str(payload.get("to") or payload.get("email") or "").strip()
    has_body = bool(payload.get("body") or payload.get("subject"))
    would = bool(to) and has_body
    # Per-send email cost is negligible but non-zero (provider list price).
    cost = 0.0 if not would else 0.0001
    effect = {
        "channel": str(payload.get("channel") or "email"),
        "recipient_present": bool(to),
        "recipient_tail": to[-12:] if to else "",
        "has_content": has_body,
    }
    note = "would send" if would else "would NOT send (missing recipient/content)"
    return would, cost, effect, note


def _simulate_call(payload: dict[str, Any]) -> tuple[bool | None, float, dict, str]:
    """Predict a voice dial: placeable iff a destination number is present."""
    to = str(
        payload.get("customer_number") or payload.get("to") or payload.get("phone") or ""
    ).strip()
    would = bool(to)
    # Rough per-call Vapi estimate (a short connect). Real cost is metered post-hoc.
    cost = 0.0 if not would else 0.03
    effect = {"destination_present": would, "destination_tail": to[-4:] if to else ""}
    note = "would dial" if would else "would NOT dial (no destination number)"
    return would, cost, effect, note


def _simulate_publish(payload: dict[str, Any]) -> tuple[bool | None, float, dict, str]:
    """Predict a publish: possible iff there is content/target to publish."""
    target = str(payload.get("url") or payload.get("page") or payload.get("target") or "").strip()
    has_content = bool(payload.get("content") or payload.get("body") or target)
    would = has_content
    effect = {"target": target, "has_content": has_content}
    note = "would publish" if would else "would NOT publish (nothing to publish)"
    return would, 0.0, effect, note


def _simulate_payment(payload: dict[str, Any]) -> tuple[bool | None, float, dict, str]:
    """Predict a payment link / invoice: valid iff a positive amount is set."""
    try:
        amount = float(payload.get("amount_usd") or payload.get("amount") or 0.0)
    except (TypeError, ValueError):
        amount = 0.0
    would = amount > 0
    effect = {"amount_usd": amount, "amount_valid": would}
    note = "would create payable" if would else "would NOT create (non-positive amount)"
    return would, 0.0, effect, note


_SIMULATORS = {
    "send_message": _simulate_send,
    "outreach_send": _simulate_send,
    "initiate_call": _simulate_call,
    "voice_dial": _simulate_call,
    "publish": _simulate_publish,
    "publish_post": _simulate_publish,
    "payment_link": _simulate_payment,
    "create_invoice": _simulate_payment,
}


def simulate_action(
    action: str,
    *,
    decision_id: str,
    target: str = "",
    payload: dict[str, Any] | None = None,
    record_result: bool = True,
) -> SimulationResult:
    """Run the dry-run path for ``action`` and (optionally) record it.

    Returns a :class:`SimulationResult` with the predicted effect + cost. An
    unmodelled action yields ``would_succeed=None`` with a note — still a
    valid, recorded simulation so the gate sees the look-before-leap happened.
    Never raises.
    """
    payload = payload or {}
    sim = _SIMULATORS.get(action)
    if sim is not None:
        try:
            would, cost, effect, note = sim(payload)
        except Exception as exc:  # noqa: BLE001 — a simulator bug must not raise out
            _LOG.warning("simulator for %s raised: %s", action, exc)
            would, cost, effect, note = None, 0.0, {}, f"simulator_error: {exc}"
    else:
        would, cost, effect, note = None, 0.0, {}, "unmodelled action (effect unknown)"

    result = SimulationResult(
        decision_id=decision_id,
        action=action,
        target=target,
        would_succeed=would,
        predicted_cost_usd=round(float(cost), 6),
        predicted_effect=effect,
        notes=note,
        ts=iso_now(),
    )
    if record_result:
        record(result)
    return result


# ---------------------------------------------------------------------------
# Dispatch gate — the mandatory "no external effect without simulation" check
# ---------------------------------------------------------------------------


class SimulationRequired(RuntimeError):
    """Raised when an external-effect dispatch lacks a passing simulation.

    ``reason`` distinguishes the two refusal modes:
      * ``"missing_simulation"`` — no simulation recorded for the decision_id.
      * ``"simulation_failed"``  — a simulation exists but predicted failure on
        a HIGH/CRITICAL action (approval must not even be requested).
    """

    def __init__(self, message: str, *, reason: str, decision_id: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.decision_id = decision_id


def gate_dispatch(
    action: str | None,
    *,
    decision_id: str,
    risk_level: str = "normal",
) -> dict | None:
    """Enforce the simulation precondition for an external-effect dispatch.

    Returns the recorded simulation dict when the dispatch is cleared (or
    ``None`` for a non-external-effect action, which is not gated). Raises
    :class:`SimulationRequired` when:

      * the action is external-effect but has NO recorded simulation for
        ``decision_id`` (fail-closed: no proof of look-before-leap -> refuse);
      * the action is HIGH/CRITICAL risk and its simulation predicted failure
        (``would_succeed is False``) — such an action must not even reach an
        approval request.
    """
    if not is_external_effect(action):
        return None
    sim = get(decision_id)
    if sim is None:
        raise SimulationRequired(
            f"external-effect action {action!r} dispatched without a recorded "
            f"simulation for decision_id {decision_id!r}",
            reason="missing_simulation",
            decision_id=decision_id,
        )
    if risk_level in ("high", "critical") and sim.get("would_succeed") is False:
        raise SimulationRequired(
            f"{risk_level}-risk action {action!r} failed simulation "
            f"({sim.get('notes')}); approval will not be requested",
            reason="simulation_failed",
            decision_id=decision_id,
        )
    return sim


__all__ = [
    "EXTERNAL_EFFECT_ACTIONS",
    "is_external_effect",
    "SimulationResult",
    "simulate_action",
    "record",
    "get",
    "has",
    "gate_dispatch",
    "SimulationRequired",
    "ledger_path",
    "ENV_LEDGER",
]
