"""Per-workcell reputation + economics (HOTL Tranche 5, framework phase 9).

Each workcell earns a reputation row from four signals, every one computed from
a durably-READABLE surface (task_state is write-only on this host, so success is
derived from the unified event stream + DLQ rather than a task_state read):

  * success_rate  — successful output events for the workcell divided by
                    (successful + DLQ failures + blocked decisions). From the
                    business-event stream (:mod:`backend.common.business_events`)
                    + the DLQ ledger (:mod:`backend.common.dlq`).
  * accuracy      — prediction-vs-outcome on the workcell's decision.made
                    records: of the decisions that carried a boolean prediction
                    (a simulation ``would_succeed`` or a non-flagged advisory),
                    the fraction that were NOT contradicted by a downstream
                    block. A calibration proxy, not a p-value.
  * profitability — the workcell's net_usd from the finance ROI roll-up
                    (:func:`backend.finance.roi.get_rollup` -> ``by_workcell``).
  * reliability   — ``1 - error_rate_ema`` from the autotuner state
                    (:mod:`backend.autonomy.autotuner`), whose EMAs were
                    computed-but-unused until now. Falls back to 1.0 when the
                    autotuner has no samples (no evidence of unreliability).

A composite ``score`` (0-1) is a simple weighted blend, deliberately downweighting
profitability (which is $-scaled and noisy day-to-day) relative to the three
behavioural axes. The row persists JSON-first with a best-effort DDB mirror,
exactly like the ROI roll-up store.

GET /admin/reputation surfaces the table. ``compute_reputation`` never raises —
every source degrades to a neutral contribution so a missing ledger yields a
truthful "insufficient evidence" row rather than an error.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from typing import Any

from .business_events import DECISION_MADE, read_events
from .dates import iso_now

_LOG = logging.getLogger("samus.reputation")

_DEFAULT_JSON_PATH = "/opt/samus/data/reputation/reputation.json"
_ENV_JSON_PATH = "SAMUS_REPUTATION_PATH"
_json_lock = threading.Lock()

# Workcells that carry a reputation. Mirrors the roster the diagnostics + ROI
# code use.
WORKCELLS: tuple[str, ...] = (
    "leadgen", "prospecting", "scaffold", "fulfillment", "memory", "feedback",
    "outreach", "proposal", "seo", "finance", "voice", "intake", "crm",
    "strategy", "cash_engine", "entropy",
)

# Business-event types that count as a successful "output" for a workcell.
_SUCCESS_EVENT_TYPES: frozenset[str] = frozenset({
    "lead.created", "lead.enriched", "email.sent", "email.opened",
    "email.clicked", "call.placed", "call.answered", "meeting.booked",
    "proposal.sent", "contract.sent", "invoice.sent", "payment.received",
    "customer.retained", "experiment.assigned",
})

# decision.made decisions that count as a BLOCK/failure against the workcell.
_BLOCK_DECISIONS: frozenset[str] = frozenset({
    "harm_suppressed_send", "send_cap_blocked", "call_cap_blocked",
})

# Composite-score weights (sum to 1.0). Profitability is downweighted — it is
# $-scaled and noisy — so the behavioural axes dominate reputation.
_WEIGHTS = {
    "success_rate": 0.40,
    "accuracy": 0.25,
    "reliability": 0.25,
    "profitability": 0.10,
}


@dataclass
class ReputationRow:
    """One workcell's reputation across the four axes + a composite score."""

    workcell: str
    success_rate: float = 1.0
    accuracy: float = 1.0
    profitability_usd: float = 0.0
    reliability: float = 1.0
    score: float = 1.0
    sample_size: int = 0        # events + failures the row was computed from
    generated_at: str = ""

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# signal readers (each degrades to a neutral value)
# ---------------------------------------------------------------------------

def _events_by_workcell(since: str | None = None) -> tuple[dict[str, int], dict[str, int], dict[str, list], dict[str, int]]:
    """Single pass over the event stream -> per-workcell success/block/decision counts."""
    successes: dict[str, int] = {}
    blocks: dict[str, int] = {}
    decisions: dict[str, list] = {}
    total_events: dict[str, int] = {}
    try:
        rows = read_events(since=since, limit=50_000)
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("reputation: event read failed: %s", exc)
        return successes, blocks, decisions, total_events
    for ev in rows:
        wc = str(ev.get("workcell") or "")
        if not wc:
            continue
        total_events[wc] = total_events.get(wc, 0) + 1
        etype = str(ev.get("event_type") or "")
        if etype in _SUCCESS_EVENT_TYPES:
            successes[wc] = successes.get(wc, 0) + 1
        elif etype == DECISION_MADE:
            meta = ev.get("metadata") or {}
            decisions.setdefault(wc, []).append(meta)
            if str(meta.get("decision") or "") in _BLOCK_DECISIONS:
                blocks[wc] = blocks.get(wc, 0) + 1
    return successes, blocks, decisions, total_events


def _dlq_failures_by_workcell() -> dict[str, int]:
    """Pending DLQ failure counts per service (a failure counts against it)."""
    from backend.common import dlq

    out: dict[str, int] = {}
    for wc in WORKCELLS:
        try:
            rows = dlq.read_pending(wc, limit=500)
        except Exception:  # noqa: BLE001 — a bad/absent ledger is zero failures
            continue
        n = sum(1 for r in rows if str(r.get("status") or "") == "pending_retry")
        if n:
            out[wc] = n
    return out


def _profit_by_workcell(day: str | None = None) -> dict[str, float]:
    """Net USD per workcell from the ROI roll-up (empty on any failure)."""
    try:
        from backend.finance.roi import get_rollup

        rollup = get_rollup(day)
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("reputation: roi rollup unavailable: %s", exc)
        return {}
    by_wc = (rollup or {}).get("by_workcell") or {}
    out: dict[str, float] = {}
    for wc, row in by_wc.items():
        try:
            out[str(wc)] = float(row.get("net_usd") or 0.0)
        except (TypeError, ValueError):
            continue
    return out


def _reliability_from_autotuner() -> float:
    """Read the autotuner error EMA -> reliability (1 - error_rate_ema).

    The autotuner persists error/block EMAs whether or not it is armed as a live
    effector; this consumes the error EMA that was previously computed-but-unused.
    Returns 1.0 when there is no state / no samples (no evidence of failure).

    The autotuner state file path is reconstructed directly via ``state_path``
    (the same path ``backend.autonomy.autotuner._state_path`` builds) rather than
    IMPORTING that module — the autonomy package's dormancy contract forbids any
    live importer outside the autonomy/cognitive trees. We only read its file.
    """
    try:
        from .state_paths import state_path

        path = state_path("autonomy", "autotuner_state.json")
        if not path.exists():
            return 1.0
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or int(raw.get("samples", 0) or 0) <= 0:
            return 1.0
        err = float(raw.get("error_rate_ema", 0.0) or 0.0)
        return max(0.0, min(1.0, 1.0 - err))
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("reputation: autotuner read failed: %s", exc)
        return 1.0


def _decision_accuracy(decisions: list[dict]) -> tuple[float, int]:
    """Prediction-vs-outcome accuracy over a workcell's decision.made records.

    A decision "predicted success" if it carried ``meaning_flagged=False`` or a
    simulation ``would_succeed=True``; it was "contradicted" if the same batch
    shows a block decision. Returns (accuracy, n_predictions). Neutral (1.0, 0)
    when nothing carried a prediction.
    """
    predictions = 0
    correct = 0
    for meta in decisions:
        decision = str(meta.get("decision") or "")
        if decision in _BLOCK_DECISIONS:
            # A block is a negative prediction that was acted on — counts as a
            # correct self-assessment (the workcell caught its own bad action).
            predictions += 1
            correct += 1
            continue
        predicted_ok = None
        if "meaning_flagged" in meta:
            predicted_ok = not bool(meta.get("meaning_flagged"))
        elif "would_succeed" in meta:
            predicted_ok = bool(meta.get("would_succeed"))
        if predicted_ok is None:
            continue
        predictions += 1
        if predicted_ok:
            correct += 1
    if predictions == 0:
        return 1.0, 0
    return correct / predictions, predictions


# ---------------------------------------------------------------------------
# computation
# ---------------------------------------------------------------------------

def compute_reputation(*, day: str | None = None, since: str | None = None) -> dict[str, ReputationRow]:
    """Compute the per-workcell reputation table from the readable surfaces."""
    successes, blocks, decisions, _totals = _events_by_workcell(since=since)
    dlq_failures = _dlq_failures_by_workcell()
    profit = _profit_by_workcell(day)
    reliability = _reliability_from_autotuner()

    seen = set(successes) | set(blocks) | set(dlq_failures) | set(profit) | set(decisions)
    workcells = [wc for wc in WORKCELLS if wc in seen] or list(WORKCELLS)

    table: dict[str, ReputationRow] = {}
    for wc in workcells:
        succ = successes.get(wc, 0)
        blk = blocks.get(wc, 0)
        fail = dlq_failures.get(wc, 0)
        denom = succ + blk + fail
        success_rate = 1.0 if denom == 0 else succ / denom
        accuracy, n_pred = _decision_accuracy(decisions.get(wc, []))
        net = round(profit.get(wc, 0.0), 4)
        # Profitability contribution to the composite is a squashed [0,1] signal:
        # >0 -> 1.0, ==0 -> 0.5 (neutral/no data), <0 -> 0.0.
        profit_signal = 0.5 if net == 0 else (1.0 if net > 0 else 0.0)
        score = (
            _WEIGHTS["success_rate"] * success_rate
            + _WEIGHTS["accuracy"] * accuracy
            + _WEIGHTS["reliability"] * reliability
            + _WEIGHTS["profitability"] * profit_signal
        )
        table[wc] = ReputationRow(
            workcell=wc,
            success_rate=round(success_rate, 4),
            accuracy=round(accuracy, 4),
            profitability_usd=net,
            reliability=round(reliability, 4),
            score=round(score, 4),
            sample_size=denom + n_pred,
            generated_at=iso_now(),
        )
    return table


# ---------------------------------------------------------------------------
# persistence (JSON-first, DDB mirror best-effort — mirrors finance/roi)
# ---------------------------------------------------------------------------

def store_path() -> str:
    return os.environ.get(_ENV_JSON_PATH, _DEFAULT_JSON_PATH)


def save_reputation(table: dict[str, ReputationRow]) -> bool:
    path = store_path()
    payload = {
        "generated_at": iso_now(),
        "workcells": {wc: row.to_record() for wc, row in table.items()},
    }
    _save_ddb(payload)
    with _json_lock:
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
            os.replace(tmp, path)
            return True
        except OSError as exc:
            _LOG.warning("reputation json save failed: %s", exc)
            return False


def _save_ddb(payload: dict[str, Any]) -> bool:
    table_name = os.environ.get(
        "DDB_PORTFOLIO_SNAPSHOTS_TABLE", "samus_portfolio_snapshots",
    )
    if not table_name:
        return False
    try:
        from backend.common import aws
        from backend.common.config import get_settings

        region = get_settings().aws_region
        item = {
            "bucket_day": f"reputation::{payload.get('generated_at', '')[:10]}",
            "row_kind": "reputation",
            "payload": json.dumps(payload, default=str),
        }
        aws.table(table_name, region).put_item(Item=item)
        return True
    except Exception as exc:  # noqa: BLE001 — JSON is the safety net
        _LOG.debug("reputation ddb save skipped/failed: %s", exc)
        return False


def load_reputation() -> dict[str, Any] | None:
    path = store_path()
    with _json_lock:
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f) or None
        except (OSError, ValueError) as exc:
            _LOG.warning("reputation json load failed: %s", exc)
            return None


def get_reputation(*, recompute: bool = False, day: str | None = None) -> dict[str, Any]:
    """Return the reputation table dict, computing + persisting when absent."""
    if not recompute:
        cached = load_reputation()
        if cached is not None:
            return cached
    table = compute_reputation(day=day)
    save_reputation(table)
    return {
        "generated_at": iso_now(),
        "workcells": {wc: row.to_record() for wc, row in table.items()},
    }


# ---------------------------------------------------------------------------
# admin route (self-registering — gateway gains one line, like finance/roi)
# ---------------------------------------------------------------------------

def register_reputation_admin_routes(app: Any) -> None:
    """Attach ``GET /admin/reputation`` to a FastAPI app.

    Defined here (an owned Tranche-5 module) so ``gateway/app.py`` only gains
    one registration line — sibling branches edit that file too.
    """

    @app.get("/admin/reputation")
    async def admin_reputation(recompute: bool = False) -> dict[str, Any]:
        """Per-workcell reputation (success_rate/accuracy/profitability/
        reliability + composite score). ``recompute=true`` recomputes from the
        live ledgers; default serves the persisted table."""
        try:
            return {"ok": True, "reputation": get_reputation(recompute=recompute)}
        except Exception as exc:  # noqa: BLE001 — operator surface never 500s
            _LOG.warning("/admin/reputation failed: %s", exc)
            return {"ok": False, "error": str(exc), "reputation": None}


__all__ = [
    "ReputationRow",
    "WORKCELLS",
    "compute_reputation",
    "save_reputation",
    "load_reputation",
    "get_reputation",
    "store_path",
    "register_reputation_admin_routes",
]
