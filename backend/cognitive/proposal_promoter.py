"""Phase G — promote propose-only cognitive proposals INTO the authoritative gates.

This is the **first path** by which the cognitive loop's output can influence
live revenue behaviour. It is still propose-only at the loop boundary: ACT
only ever appends a structured proposal to
``state_path("cognition","proposals.jsonl")``. Phase G then PICKS UP those
unactioned rows and **routes them THROUGH the existing authoritative gates** —
specifically the cash-engine front door
:func:`backend.cash_engine.service.review_opportunity`, which itself runs:

  * the Stake-Sentence read + presence check
    (:func:`backend.cash_engine.gate.evaluate_gate`),
  * the Codex validator (:func:`backend.common.codex.validator.check_action`),
  * the karma gate (:mod:`backend.governance.karma.engine`), and
  * (downstream of an ``enqueued`` verdict) the outreach live-send double-gate
    in :mod:`backend.cash_engine.stages` and the compliance guard
    (:mod:`backend.common.compliance_guard`).

The EFH (:mod:`backend.governance.efh_evaluator`) is also authoritative — it
fires inside the cognitive loop's CLASSIFY stage, so an EFH-blocked cycle never
emits a proposal Phase G could pick up in the first place.

**Phase G does NOT bypass any of these gates.** It calls ``review_opportunity``
as the front-door front-door, honours its verdict (PASS / BLOCK / DEFER /
DISABLED / INVALID — anything other than ``status=='enqueued'`` is treated as
NOT-PROMOTED), and records the disposition back onto the source proposal row.
A BLOCK is sticky: Phase G NEVER retries past it. The cycle's next proposal is
a fresh chance.

Wire-not-arm posture — the operator's lever:

  * **Flag (kill-switch, default True per operator direction):**
    ``cognition_proposal_promotion_enabled`` in
    :class:`backend.common.config.Settings` (env
    ``SAMUS_COGNITION_PROPOSAL_PROMOTION_ENABLED``, default ``True``).
  * When False, :meth:`ProposalPromoter.run` and :meth:`promote_one` are PURE
    no-ops: zero ledger reads, zero ``review_opportunity`` calls, zero
    proposal-row mutations. Disabling Phase G is one env flag away.

Idempotency: a re-run must not re-promote already-actioned rows. The promoter
uses each row's own ``actioned`` field as the authority (no side-channel), and
rewrites the JSONL file ONLY by appending a flipped copy of each just-processed
row to the same ledger (the latest entry for a given ``proposal_id`` wins on
read). This preserves the append-only invariant of the ledger AND keeps the
historical "proposed" row intact — we never overwrite history.

Fail-closed envelope: ANY exception out of ``review_opportunity`` (including
``LlmCallError`` / ``BudgetExceeded`` / ``CodexUnavailable`` / network /
Pydantic validation), and any other unexpected error inside the promoter, is
caught at the promotion-attempt boundary and converted to
``promotion_result.status="error"`` with a reason string. The promoter NEVER
lets an exception escape — the cadence keeps running.

DORMANCY: this module has NO live importer at the time it lands. The runner
seam (:mod:`backend.cognitive.runner`) optionally invokes :func:`promote_one`
right after a successful ``run_one_cycle`` writes a proposal, gated by the
Phase-G flag. No cadence is scheduled; activation is wholly operator-driven.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional

from backend.common.dates import iso_now
from backend.common.persistence import JsonlLedger
from backend.common.state_paths import state_path

log = logging.getLogger(__name__)

# Telemetry/ledger event name for an attempted promotion (PASS, BLOCK, error,
# or skipped). One event per attempt — the audit trail when this is armed.
EVENT_NAME = "samus.cognition.proposal_promoted"

# Trigger source we stamp on the synthesised RevenueTriggerRequest. ``manual_review``
# is the conventional "external/operator/peer caller asked for a review" source;
# we then record the cognition origin in ``trigger_reason`` for the audit trail.
# (Adding a new TriggerSource would touch the cash-engine front door's Pydantic
#  schema — intentionally additive-only to that module is out of scope here.)
_PROMOTION_TRIGGER_SOURCE = "manual_review"

# Promotion verdict statuses written under proposal["promotion_result"]["status"].
STATUS_PASS = "pass"  # review_opportunity returned status=="enqueued"
STATUS_BLOCKED = "blocked"  # review_opportunity returned escalated / invalid / disabled
STATUS_SKIPPED = "skipped"  # nothing actionable in the proposal (e.g. no prospect_id)
STATUS_ERROR = "error"  # review_opportunity raised; fail-closed

# Reason stamped on a SKIP that fired because the proposal's prospect_id is
# still inside its post-BLOCK cooldown window (see _BLOCK_COOLDOWN below).
REASON_BLOCK_COOLDOWN = "block_cooldown"

# ---------------------------------------------------------------------------
# Per-prospect BLOCK cooldown — MODULE-LEVEL so it persists across the per-cycle
# ProposalPromoter instances the runner builds every tick (promote_one_for_runner
# constructs a fresh promoter each call). Maps prospect_id -> time.monotonic()
# AT THE LAST BLOCK. Absence from the dict means "never blocked" — we NEVER store
# a 0.0 sentinel (house rule: monotonic() never-ran must not be 0.0; only real
# block timestamps are stored, and membership is the never-blocked test).
# Resets on gateway restart (acceptable — a restart is a fresh chance).
_BLOCK_COOLDOWN: dict[str, float] = {}


def _block_cooldown_sec() -> int:
    """Resolve ``cognition_promotion_block_cooldown_sec``, fail-closed to default.

    Any settings hiccup falls back to the 600s default (the field's own
    default) rather than raising — a settings outage must never crash a tick
    nor accidentally disable the anti-spam suppression.
    """
    try:
        from backend.common.config import get_settings

        return int(getattr(get_settings(), "cognition_promotion_block_cooldown_sec", 600))
    except Exception:  # noqa: BLE001 — fail-closed to the default window
        return 600


@dataclass
class PromotionResult:
    """One promotion attempt's verdict, mirroring what we stamp on the row."""

    status: str  # pass | blocked | skipped | error
    reason: Optional[str] = None
    review_status: Optional[str] = None  # echoed cash_engine ReviewStatus
    required_protocol: Optional[str] = None  # gate's escalation hint
    violated_rule_id: Optional[str] = None  # Codex rule id when applicable
    task_id: Optional[str] = None  # cash-engine task id on PASS
    prospect_id: Optional[str] = None

    def to_dict(self) -> dict:
        out: dict = {"status": self.status}
        for key in (
            "reason",
            "review_status",
            "required_protocol",
            "violated_rule_id",
            "task_id",
            "prospect_id",
        ):
            val = getattr(self, key)
            if val is not None:
                out[key] = val
        return out


# ---------------------------------------------------------------------------
# Flag — kill-switch, default True (per operator direction).
# ---------------------------------------------------------------------------
def promotion_enabled() -> bool:
    """Resolve the Phase-G kill-switch ``cognition_proposal_promotion_enabled``.

    Default True (the operator's chosen "wire ON" stance) — but reading off
    settings is wrapped so any settings hiccup fails CLOSED to disabled. A
    settings outage during boot must NEVER accidentally arm an unproven path
    that DOES reach the gates: disabling Phase G is the safer degrade.
    """
    try:
        from backend.common.config import get_settings

        return bool(getattr(get_settings(), "cognition_proposal_promotion_enabled", False))
    except Exception:  # noqa: BLE001 — fail-closed to OFF
        return False


# ---------------------------------------------------------------------------
# JSONL ledger helpers — read unactioned rows + append flipped marker.
# ---------------------------------------------------------------------------
def _proposals_path():
    """Same path the Phase-E ACT stage writes to."""
    return state_path("cognition", "proposals.jsonl")


def _telemetry_ledger() -> JsonlLedger:
    """Phase-G ledger for the audit trail of every promotion attempt."""
    return JsonlLedger(state_path("cognition", "proposal_promotions.jsonl"))


def _stable_proposal_id(row: dict) -> str:
    """Derive a stable id for a proposal row.

    The Phase-E proposal record doesn't carry an explicit id; we synthesise one
    from the (ts, plan_token) pair so the latest-wins read can identify which
    historical row a flipped marker belongs to. If the row already carries an
    explicit ``proposal_id`` (forward compat) that wins.
    """
    explicit = row.get("proposal_id")
    if explicit:
        return str(explicit)
    return f"{row.get('ts', '')}|{row.get('plan_token', '')}"


def _read_jsonl_rows(path) -> List[dict]:
    """Read every row off the proposals ledger, oldest first. [] when absent."""
    rows: List[dict] = []
    if not path.exists():
        return rows
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        log.warning("proposal_promoter: read failed: %s", exc)
    return rows


def _select_unactioned(rows: Iterable[dict]) -> List[dict]:
    """Return rows whose latest historical entry has ``actioned=False``.

    The ledger is append-only: a flipped marker is just another row with the
    same ``_stable_proposal_id``. Latest-wins reduces the history to the
    current state without rewriting the file. Rows of kinds other than
    ``cognition_proposal`` are ignored (forward-compat: the promoter only ever
    acts on rows it knows about).
    """
    # Walk in order so the LAST occurrence of each id wins.
    latest: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("kind") != "cognition_proposal":
            continue
        latest[_stable_proposal_id(row)] = row
    return [r for r in latest.values() if not r.get("actioned", False)]


# ---------------------------------------------------------------------------
# Routing — derive a RevenueTriggerRequest from a proposal (when possible).
# ---------------------------------------------------------------------------
def _extract_prospect_id(proposal: dict) -> Optional[str]:
    """Best-effort: pull a prospect id off the proposal's ``intent``.

    Phase-E intent records carry counts but not prospect_ids today; forward
    callers may stamp ``intent.prospect_id`` explicitly. When absent, Phase G
    cannot synthesise a ``RevenueTriggerRequest`` (it requires a prospect_id),
    so the row is SKIPPED (not BLOCKED — the gates would have nothing to gate).
    """
    intent = proposal.get("intent")
    if not isinstance(intent, dict):
        return None
    pid = intent.get("prospect_id")
    return str(pid) if pid else None


def _build_revenue_request(proposal: dict) -> Optional[Any]:
    """Build a ``RevenueTriggerRequest`` from a proposal, or None to skip.

    Lazy-imports the Pydantic model so importing this module touches no
    pydantic / cash_engine machinery at all.
    """
    pid = _extract_prospect_id(proposal)
    if not pid:
        return None
    # Local import keeps this module lightweight + dormant on import.
    from backend.cash_engine.models import RevenueTriggerRequest

    intent = proposal.get("intent") or {}
    intent_type = str(intent.get("type") or "")
    trigger_reason = f"cognition_promotion:{intent_type}".strip(":")
    # Echo the originating cognitive plan_token + a compact perception note so
    # the cash-engine review ledger carries the audit trail back to the cycle.
    summary = proposal.get("perception_summary") or {}
    state_note = (
        f"cognition_proposal plan_token={proposal.get('plan_token', '')!r} "
        f"intent={intent_type!r} reflect_score={proposal.get('reflect_score')}"
    )
    return RevenueTriggerRequest(
        prospect_id=pid,
        trigger_source=_PROMOTION_TRIGGER_SOURCE,
        current_samus_state=state_note[:512],
        trigger_reason=trigger_reason[:512],
        gap_report_artifact_id="",
    )


def _verdict_from_review(result: Any) -> PromotionResult:
    """Project a cash-engine ``RevenueTriggerResult`` onto a ``PromotionResult``."""
    status = str(getattr(result, "status", "") or "")
    pid = str(getattr(result, "prospect_id", "") or "") or None
    if status == "enqueued":
        return PromotionResult(
            status=STATUS_PASS,
            reason=getattr(result, "reason", None),
            review_status=status,
            task_id=str(getattr(result, "task_id", "") or "") or None,
            prospect_id=pid,
        )
    # Everything else is treated as NOT-PROMOTED (BLOCKED / DEFER / DISABLED /
    # INVALID). The gates' verdict is authoritative — Phase G honours it.
    return PromotionResult(
        status=STATUS_BLOCKED,
        reason=getattr(result, "reason", None),
        review_status=status or None,
        required_protocol=getattr(result, "required_protocol", None),
        violated_rule_id=getattr(result, "violated_rule_id", None),
        prospect_id=pid,
    )


# ---------------------------------------------------------------------------
# The promoter itself — sink/iterator-friendly for testability.
# ---------------------------------------------------------------------------
class ProposalPromoter:
    """Route propose-only cognitive proposals through ``review_opportunity``.

    All collaborators are injectable for test isolation:

    * ``review`` — callable ``(req) -> RevenueTriggerResult``. Default = the
      live :func:`backend.cash_engine.service.review_opportunity` (lazy-imported
      so this module's import is cheap).
    * ``ledger_path`` — where the proposals JSONL lives. Default =
      ``state_path("cognition","proposals.jsonl")``. Tests point at a tmp file.
    * ``telemetry`` — duck-typed ledger exposing ``append(dict)``. Default =
      the durable Phase-G audit ledger; tests inject a spy.
    """

    def __init__(
        self,
        *,
        review: Optional[Any] = None,
        ledger_path: Optional[Any] = None,
        telemetry: Optional[Any] = None,
    ) -> None:
        self._review = review
        # ``ledger_path`` may be a pathlib.Path or a str; resolve lazily.
        self._ledger_path = ledger_path
        self._telemetry = telemetry

    # ---- public entry points -------------------------------------------
    def run(self) -> List[PromotionResult]:
        """Promote every unactioned proposal in the JSONL ledger.

        Pure no-op when the kill-switch is OFF (zero reads, zero gate calls,
        zero telemetry writes). Otherwise iterates the unactioned rows oldest
        first and calls :meth:`promote_one` on each. Returns the list of
        :class:`PromotionResult` verdicts (empty when disabled). NEVER raises.
        """
        if not promotion_enabled():
            return []
        path = self._resolve_ledger_path()
        rows = _select_unactioned(_read_jsonl_rows(path))
        results: List[PromotionResult] = []
        for row in rows:
            try:
                results.append(self.promote_one(row, _bypass_flag_check=True))
            except Exception as exc:  # noqa: BLE001 — outer fail-closed envelope
                log.exception("proposal_promoter: unexpected error during run()")
                results.append(
                    self._record_outcome(
                        row,
                        PromotionResult(
                            status=STATUS_ERROR, reason=f"run:{type(exc).__name__}:{exc}"
                        ),
                    )
                )
        return results

    def promote_one(
        self,
        proposal: dict,
        *,
        _bypass_flag_check: bool = False,
    ) -> PromotionResult:
        """Promote a single proposal row. Fail-closed; never raises.

        ``_bypass_flag_check`` is an internal helper used by :meth:`run` so it
        does not re-resolve the flag on every row (it already gated at the top).
        External callers (the runner seam) always check the flag — but we also
        re-check here so direct callers can't accidentally arm a disabled path.
        """
        if not _bypass_flag_check and not promotion_enabled():
            # Disabled => pure no-op. Do NOT write a telemetry row (a disabled
            # path leaves no trace by design — easier to prove it didn't fire).
            return PromotionResult(status=STATUS_SKIPPED, reason="disabled")

        if not isinstance(proposal, dict) or proposal.get("kind") != "cognition_proposal":
            # Defensive: only act on cognition proposals.
            return PromotionResult(status=STATUS_SKIPPED, reason="not_a_cognition_proposal")

        # Idempotency: already-actioned row -> no double promotion.
        if proposal.get("actioned", False):
            return PromotionResult(status=STATUS_SKIPPED, reason="already_actioned")

        # Per-prospect BLOCK cooldown. After a sticky BLOCK for this prospect_id
        # the active cadence would otherwise re-promote the SAME prospect every
        # ~60s tick and re-run review_opportunity for the same verdict, spamming
        # the promotion ledger. Inside the window we SKIP WITHOUT calling
        # review_opportunity. Fully fail-closed: any error here means we proceed
        # as if there were no cooldown (never crash the tick). Only a BLOCK ever
        # populates _BLOCK_COOLDOWN, so this never suppresses a PASS path.
        try:
            cooldown_pid = _extract_prospect_id(proposal)
        except Exception:  # noqa: BLE001 — cooldown lookup must never crash
            cooldown_pid = None
        if cooldown_pid:
            try:
                last_block = _BLOCK_COOLDOWN.get(cooldown_pid)
                if last_block is not None:
                    cooldown_sec = _block_cooldown_sec()
                    if (time.monotonic() - last_block) < cooldown_sec:
                        # Suppress: mark actioned + write the ledger event so the
                        # audit trail shows the cooldown skip, but do NOT call
                        # the gate. (review_opportunity is never reached.)
                        return self._record_outcome(
                            proposal,
                            PromotionResult(
                                status=STATUS_SKIPPED,
                                reason=REASON_BLOCK_COOLDOWN,
                                prospect_id=cooldown_pid,
                            ),
                        )
            except Exception:  # noqa: BLE001 — any cooldown fault -> proceed normally
                log.warning(
                    "proposal_promoter: cooldown check failed for %s; proceeding",
                    cooldown_pid,
                )

        # Build the request. Missing prospect_id -> SKIP (gates would have
        # nothing to gate; this is honest "nothing actionable" not a BLOCK).
        try:
            req = _build_revenue_request(proposal)
        except Exception as exc:  # noqa: BLE001 — fail-closed on construction
            return self._record_outcome(
                proposal,
                PromotionResult(
                    status=STATUS_ERROR,
                    reason=f"build_request:{type(exc).__name__}:{exc}",
                ),
            )
        if req is None:
            return self._record_outcome(
                proposal,
                PromotionResult(
                    status=STATUS_SKIPPED,
                    reason="no_prospect_id",
                ),
            )

        # Call the authoritative front door. Every gate (Stake Sentence, Codex,
        # karma) runs INSIDE this call. We honour whatever it returns.
        review_fn = self._resolve_review_fn()
        try:
            result = review_fn(req)
        except Exception as exc:  # noqa: BLE001 — fail-closed on ANY gate error
            # Includes LlmCallError, BudgetExceeded, CodexUnavailable, network,
            # Pydantic validation, anything else. The cadence keeps running.
            log.warning(
                "proposal_promoter: review_opportunity raised %s: %s",
                type(exc).__name__,
                exc,
            )
            return self._record_outcome(
                proposal,
                PromotionResult(
                    status=STATUS_ERROR,
                    reason=f"review:{type(exc).__name__}:{exc}",
                    prospect_id=getattr(req, "prospect_id", None),
                ),
            )

        verdict = _verdict_from_review(result)
        # A BLOCK verdict (re)arms the per-prospect cooldown so the next ticks
        # SKIP this prospect without re-running the gate. ONLY a BLOCK does this
        # — PASS / ERROR / no-prospect_id SKIP never populate the cache. We store
        # a real time.monotonic() (never a 0.0 sentinel); fail-closed so a cache
        # write fault never crashes the tick.
        if verdict.status == STATUS_BLOCKED:
            block_pid = verdict.prospect_id or getattr(req, "prospect_id", None)
            if block_pid:
                try:
                    _BLOCK_COOLDOWN[str(block_pid)] = time.monotonic()
                except Exception:  # noqa: BLE001 — cooldown arm is best-effort
                    log.warning(
                        "proposal_promoter: failed to arm cooldown for %s",
                        block_pid,
                    )

        return self._record_outcome(proposal, verdict)

    # ---- bookkeeping ----------------------------------------------------
    def _record_outcome(self, proposal: dict, verdict: PromotionResult) -> PromotionResult:
        """Flip the proposal to ``actioned=True`` (durable) + emit telemetry.

        The proposal row is preserved in history. We append a NEW row to the
        same JSONL ledger carrying the SAME stable id with ``actioned=True``
        and ``promotion_result`` populated — latest-wins on read. The
        historical "proposed" entry is never rewritten.
        """
        try:
            flipped = self._build_actioned_marker(proposal, verdict)
            self._append_to_proposals(flipped)
        except Exception as exc:  # noqa: BLE001 — bookkeeping is best-effort
            log.warning("proposal_promoter: durable mark failed: %s", exc)

        try:
            self._emit_telemetry(proposal, verdict)
        except Exception as exc:  # noqa: BLE001 — telemetry is best-effort
            log.warning("proposal_promoter: telemetry append failed: %s", exc)

        return verdict

    @staticmethod
    def _build_actioned_marker(proposal: dict, verdict: PromotionResult) -> dict:
        """A flipped copy of the row with ``actioned=True`` + ``promotion_result``.

        Preserves the stable id (so latest-wins identifies it as the same
        proposal) and the originating plan_token / intent for the audit trail.
        """
        return {
            "kind": "cognition_proposal",
            "ts": iso_now(),
            "proposal_id": _stable_proposal_id(proposal),
            "plan_token": proposal.get("plan_token", "") or "",
            "channel": proposal.get("channel", "") or "",
            "intent": proposal.get("intent") or {},
            "status": "actioned",  # historical "proposed" -> "actioned"
            "actioned": True,  # load-bearing for idempotency
            "promotion_result": verdict.to_dict(),
            "note": (
                "PROMOTION RECORD — Phase G routed this cognitive proposal "
                "through review_opportunity (which runs Stake-Sentence + "
                "Codex + karma + downstream compliance/EFH gates). The "
                "verdict in promotion_result is authoritative; a BLOCK is "
                "sticky and NEVER retried."
            ),
        }

    def _append_to_proposals(self, row: dict) -> None:
        path = self._resolve_ledger_path()
        JsonlLedger(path).append(row)

    def _emit_telemetry(self, proposal: dict, verdict: PromotionResult) -> None:
        """One structured ledger row per attempt (the operator-audit trail)."""
        event = {
            "event": EVENT_NAME,
            "ts": iso_now(),
            "event_id": uuid.uuid4().hex,
            "proposal_id": _stable_proposal_id(proposal),
            "plan_token": proposal.get("plan_token", "") or "",
            "intent_type": (proposal.get("intent") or {}).get("type"),
            "verdict": verdict.to_dict(),
        }
        sink = self._telemetry if self._telemetry is not None else _telemetry_ledger()
        sink.append(event)

    # ---- collaborator resolvers ----------------------------------------
    def _resolve_review_fn(self):
        """Lazy import of ``review_opportunity`` so the module is cheap to import."""
        if self._review is not None:
            return self._review
        from backend.cash_engine.service import review_opportunity

        return review_opportunity

    def _resolve_ledger_path(self):
        if self._ledger_path is not None:
            return self._ledger_path
        return _proposals_path()


# ---------------------------------------------------------------------------
# Runner-seam convenience: promote a SINGLE just-written proposal.
# ---------------------------------------------------------------------------
def promote_one_for_runner(
    proposal: Optional[dict],
    *,
    review: Optional[Any] = None,
    telemetry: Optional[Any] = None,
    ledger_path: Optional[Any] = None,
) -> Optional[PromotionResult]:
    """Phase-G hook the runner calls right after a successful cycle.

    Returns:
      * ``None`` when there is nothing to promote — either the flag is OFF,
        the cycle wrote no proposal (ACT disabled / compliance-blocked / no
        intent), or the proposal is already actioned.
      * a :class:`PromotionResult` otherwise (PASS / BLOCKED / SKIPPED /
        ERROR — all fail-closed).

    NEVER raises. NEVER promotes a compliance-blocked cycle's proposal (the
    runner checks ``compliance_blocked`` BEFORE invoking this — see the seam
    in :mod:`backend.cognitive.runner`). Idempotency comes for free: if the
    runner accidentally invokes this twice for the same row, the second call
    sees ``actioned=True`` and SKIPs.
    """
    if not promotion_enabled():
        return None
    if not isinstance(proposal, dict):
        return None
    promoter = ProposalPromoter(
        review=review,
        telemetry=telemetry,
        ledger_path=ledger_path,
    )
    return promoter.promote_one(proposal, _bypass_flag_check=True)


__all__ = [
    "EVENT_NAME",
    "REASON_BLOCK_COOLDOWN",
    "PromotionResult",
    "ProposalPromoter",
    "promotion_enabled",
    "promote_one_for_runner",
]
