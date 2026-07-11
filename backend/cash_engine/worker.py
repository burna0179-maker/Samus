"""Worker — walks an enqueued job through the staged sequence, then rests.

``process_job`` is the consumer side of the front door: it loads (or creates)
the deal's :class:`~backend.cash_engine.state.CashEngineState`, then runs each
remaining stage in order — audit -> proposal -> contact -> outreach ->
deliver — halting on a Codex block (escalate) or an unwired external route /
not-yet-won deal (park), and flipping to ``dormant`` once the walk parks or
completes. The ``deliver`` stage only fires for a won deal; until then it parks
and the deal rests dormant awaiting re-engagement. It is idempotent: a
redelivered job skips stages already recorded as complete.

``drain`` is the mock-first pump: it processes every job sitting in the local
JSONL queue. ``reengage`` is the money loop's other half — fired by a feedback
signal (reply / open / visit), it compiles the deal's history and drafts a
fresh voicemail script for the operator to record.

Leaf execution lives in :mod:`backend.cash_engine.stages`; this module owns
the walk, the persistence, and the dormancy/re-engagement transitions.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from backend.common.dates import iso_now
from backend.common.decision_record import record_decision
from backend.common.settings import get_settings
from backend.crm.models import CreateArtifactRequest, CreateOperatorTaskRequest

from . import queue as cash_queue
from .stages import DEFAULT_HANDLERS, StageContext, StageResult
from .state import (
    DORMANT,
    STAGE_SEQUENCE,
    CashEngineState,
    load_state,
    save_state,
)

_LOG = logging.getLogger("samus.cash_engine.worker")

# Fields a StageResult may set that map straight onto CashEngineState.
_STATE_FIELDS = (
    "gap_report_artifact_id",
    "proposal_ref",
    "callsheet_artifact_id",
    "voicemail_artifact_id",
    "outreach_ref",
    "outreach_scheduled_for",
    "website_order_id",
)


def _record_walk_decision(
    state: CashEngineState,
    *,
    decision_kind: str,
    why: str,
    risk_level: str = "normal",
    stage: str = "",
    alternatives: list[Any] | None = None,
    data_used: list[Any] | None = None,
    expected_outcome: str = "",
) -> None:
    """Join a cash-engine walk transition to the unified decision spine.

    ADDITIVE telemetry only — the state-machine decision is already made and
    persisted (``state.escalation`` / ``state.park`` + ``state.log``). This
    mints a matching ``decision.made`` record so a revenue-blocking escalate /
    park (the loop-terminating decisions that decide whether a prospect
    advances toward revenue or rests) is reconstructable in the journey view
    (``list_decisions`` / ``GET /admin/journey``) with a rationale, alongside
    the planner + arbiter decisions — not just a bare state.log line.
    ``record_decision`` never raises; the extra guard is belt-and-suspenders so
    a telemetry fault can never break the walk.
    """
    try:
        record_decision(
            actor="cash_engine_worker",
            why=why,
            workcell="cash_engine",
            risk_level=risk_level,
            alternatives_considered=alternatives or [],
            data_used=data_used or [],
            expected_outcome=expected_outcome,
            prospect_id=state.prospect_id or None,
            opportunity_id=state.opportunity_id or None,
            extra={"decision_kind": decision_kind, "stage": stage or state.stage},
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must never break the walk
        _LOG.debug("cash_engine walk decision-record skipped: %s", exc)


def process_job(
    job: dict[str, Any],
    *,
    handlers: dict[str, Callable[[StageContext], StageResult]] | None = None,
    crm: Any = None,
    settings: Any = None,
) -> CashEngineState | None:
    """Walk one enqueued job through the remaining stages. Idempotent.

    ``job`` is a mock-queue record (``{"payload": {...}, ...}``) or a bare
    payload dict. ``handlers`` / ``crm`` are injectable for tests.
    """
    handlers = handlers or DEFAULT_HANDLERS
    if crm is None:
        from backend.crm import service as crm  # local import: avoid cycle
    settings = settings or get_settings()

    payload = job.get("payload") if isinstance(job.get("payload"), dict) else job
    opportunity_id = str(payload.get("opportunity_id") or "")
    prospect_id = str(payload.get("prospect_id") or "")
    if not opportunity_id:
        _LOG.warning("cash_engine process_job: missing opportunity_id; dropping")
        return None

    opp = crm.get_opportunity(opportunity_id)
    if opp is None:
        _LOG.warning("cash_engine process_job: opportunity %s not found", opportunity_id)
        return None
    stake = str(getattr(opp, "stake_sentence", "") or "").strip()
    if not stake:
        # Defence in depth: the front door already gated this, but state can
        # outlive a stake being cleared. Refuse to act un-staked.
        state = load_state(opportunity_id) or CashEngineState(
            opportunity_id=opportunity_id,
            prospect_id=prospect_id,
        )
        state.status = "escalated"
        state.escalation = {"stage": state.stage, "reason": "stake_sentence_missing"}
        state.log("escalated", reason="stake_sentence_missing")
        _record_walk_decision(
            state,
            decision_kind="escalate",
            why="stake_sentence_missing",
            risk_level="high",
            alternatives=["proceed (rejected: no stake = unauthorized revenue action)"],
            expected_outcome="operator resolves; deal holds un-staked",
        )
        save_state(state)
        return state

    state = load_state(opportunity_id)
    if state is None:
        state = CashEngineState(
            opportunity_id=opportunity_id,
            prospect_id=prospect_id,
            task_id=str(payload.get("task_id") or job.get("task_id") or ""),
            trigger_source=str(payload.get("trigger_source") or ""),
            stage=STAGE_SEQUENCE[0],
        )
        state.gap_report_artifact_id = str(payload.get("gap_report_artifact_id") or "")
        save_state(state)

    if state.status in (DORMANT, "done"):
        return state  # already walked — idempotent no-op

    prospect = crm.get_prospect(prospect_id) if prospect_id else None
    ctx = StageContext(
        state=state,
        opportunity=opp,
        prospect=prospect,
        stake_sentence=stake,
        crm=crm,
        settings=settings,
    )

    start = STAGE_SEQUENCE.index(state.stage) if state.stage in STAGE_SEQUENCE else 0
    for stage in STAGE_SEQUENCE[start:]:
        if stage in state.completed_stages:
            continue
        state.stage = stage
        handler = handlers.get(stage)
        if handler is None:
            # No handler wired for this stage (e.g. a partial injected map or a
            # newly-added stage an older caller doesn't know about). Park
            # honestly rather than crash — resumable once a handler is supplied.
            state.status = "parked"
            state.park = {"stage": stage, "reason": "no_handler"}
            state.log("parked", stage=stage, reason="no_handler")
            _record_walk_decision(
                state,
                decision_kind="park",
                why="no_handler",
                stage=stage,
                expected_outcome="deal rests; resumes when a handler is wired",
            )
            save_state(state)
            _LOG.info(
                "cash_engine job parked opp=%s stage=%s reason=no_handler", opportunity_id, stage
            )
            return state
        result = handler(ctx)

        if result.codex_blocked:
            state.status = "escalated"
            state.escalation = {
                "stage": stage,
                "violated_rule_id": result.violated_rule_id,
                "reason": result.reason,
                "drafted_adr_path": result.drafted_adr_path,
            }
            state.log("escalated", stage=stage, rule=result.violated_rule_id)
            _record_walk_decision(
                state,
                decision_kind="escalate",
                why=result.reason or "codex_blocked",
                risk_level="high",
                stage=stage,
                alternatives=["proceed (rejected: codex rule violation)"],
                data_used=[f"violated_rule_id={result.violated_rule_id}"],
                expected_outcome="operator resolves the codex block; deal holds",
            )
            save_state(state)
            _LOG.info("cash_engine job escalated opp=%s stage=%s", opportunity_id, stage)
            return state

        if result.parked:
            state.status = "parked"
            state.park = {"stage": stage, "reason": result.park_reason}
            state.log("parked", stage=stage, reason=result.park_reason)
            _record_walk_decision(
                state,
                decision_kind="park",
                why=result.park_reason or "parked",
                stage=stage,
                expected_outcome="deal rests; resumes on re-engagement or a resolved block",
            )
            save_state(state)
            _LOG.info(
                "cash_engine job parked opp=%s stage=%s reason=%s",
                opportunity_id,
                stage,
                result.park_reason,
            )
            return state

        for key in _STATE_FIELDS:
            if key in result.detail:
                setattr(state, key, str(result.detail[key]))
        state.completed_stages.append(stage)
        state.log("stage_complete", stage=stage, **result.detail)
        save_state(state)

    # Every forward stage done -> rest until a re-engagement signal arrives.
    state.status = DORMANT
    state.stage = DORMANT
    state.dormant_since = iso_now()
    state.log("dormant")
    _record_walk_decision(
        state,
        decision_kind="complete",
        why="walk complete through all stages; resting until re-engagement",
        stage="deliver",
        data_used=[f"completed_stages={list(state.completed_stages)}"],
        expected_outcome="deal rests dormant until a re-engagement signal (reply/open/visit)",
    )
    save_state(state)
    _LOG.info("cash_engine job dormant opp=%s", opportunity_id)
    return state


def drain(
    *,
    handlers: dict[str, Callable[[StageContext], StageResult]] | None = None,
    crm: Any = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Process every job in the local JSONL mock queue. Idempotent per deal."""
    jobs = cash_queue.read_mock_jobs(limit=limit)
    states: list[CashEngineState] = []
    for job in jobs:
        try:
            st = process_job(job, handlers=handlers, crm=crm)
        except Exception as exc:  # noqa: BLE001 — one bad job must not stop the drain
            _LOG.error("cash_engine drain: job failed: %s", exc)
            continue
        if st is not None:
            states.append(st)
    summary = {
        "processed": len(states),
        "dormant": sum(1 for s in states if s.status == DORMANT),
        "escalated": sum(1 for s in states if s.status == "escalated"),
        "parked": sum(1 for s in states if s.status == "parked"),
        "running": sum(1 for s in states if s.status == "running"),
    }
    _LOG.info("cash_engine drain: %s", summary)
    return summary


def reengage(
    *,
    opportunity_id: str,
    event: str,
    crm: Any = None,
) -> dict[str, Any]:
    """Re-engagement loop — draft a fresh voicemail from the deal's history.

    Fired by a feedback signal (``event`` = reply / open / visit / manual).
    Compiles what we know — the Gap Report, the outreach history — and drafts
    a new voicemail script the operator reviews and records. Codex-gated so a
    re-engagement draft can't smuggle banned language either.
    """
    if crm is None:
        from backend.crm import service as crm

    state = load_state(opportunity_id)
    if state is None:
        return {"ok": False, "reason": "no_state"}
    # Only a deal that has completed the initial walk and is RESTING is eligible
    # for re-engagement. This makes the loop idempotent against an open-storm
    # (many opens in a row) and refuses to re-engage a deal that is still mid-
    # walk, parked on an operator, escalated, or halted.
    if state.status != DORMANT:
        return {"ok": False, "reason": f"not_dormant:{state.status}"}

    opp = crm.get_opportunity(opportunity_id)
    if opp is None:
        return {"ok": False, "reason": "opportunity_not_found"}
    stake = str(getattr(opp, "stake_sentence", "") or "").strip()
    prospect = crm.get_prospect(state.prospect_id) if state.prospect_id else None
    if prospect is None:
        return {"ok": False, "reason": "prospect_not_found"}

    from backend.cash_engine.stages import codex_gate
    from backend.prospecting.callsheet import _voicemail

    script = _voicemail(prospect, stake)
    verdict = codex_gate(
        action_kind="other",
        capability="reengage",
        payload={"voicemail_script": script, "stake_sentence": stake},
    )
    if not verdict.allowed:
        state.log("reengage_blocked", rule=verdict.violated_rule_id)
        save_state(state)
        return {
            "ok": False,
            "reason": "codex_blocked",
            "violated_rule_id": verdict.violated_rule_id,
        }

    history_summary = {
        "trigger_event": event,
        "gap_report_artifact_id": state.gap_report_artifact_id,
        "proposal_ref": state.proposal_ref,
        "outreach_ref": state.outreach_ref,
        "outreach_scheduled_for": state.outreach_scheduled_for,
        "prior_events": [h.get("event") for h in state.history],
    }
    artifact = crm.create_artifact(
        CreateArtifactRequest(
            kind="voicemail",
            owner_entity_kind="opportunity",
            owner_entity_id=opportunity_id,
            title=f"Re-engagement voicemail ({event})",
            inline_data={"script": script, "history": history_summary},
            source="cash_engine_reengage",
            created_by="cash_engine",
        )
    )

    operator_task_id = ""
    try:
        task = crm.create_operator_task(
            CreateOperatorTaskRequest(
                kind="call",
                title=f"Record re-engagement voicemail for {getattr(prospect, 'company_name', '') or state.prospect_id}",
                description=f"Triggered by {event}. Draft artifact {artifact.artifact_id}.",
                related_entity_kind="opportunity",
                related_entity_id=opportunity_id,
                source="cash_engine_reengage",
                source_ref=artifact.artifact_id,
            )
        )
        operator_task_id = task.operator_task_id
    except Exception as exc:  # noqa: BLE001 — the draft is the deliverable; task is a bonus
        _LOG.warning("cash_engine reengage: operator task creation failed: %s", exc)

    state.status = "running"  # re-opened from dormant by the signal
    state.log("reengaged", trigger_event=event, voicemail_artifact_id=artifact.artifact_id)
    save_state(state)
    return {
        "ok": True,
        "voicemail_artifact_id": artifact.artifact_id,
        "operator_task_id": operator_task_id,
    }


def halt(*, opportunity_id: str, reason: str) -> dict[str, Any]:
    """Stop a deal's cash-engine sequence on a negative signal.

    Fired when a prospect's outreach bounces, draws a complaint, or they
    unsubscribe — the engine must stop chasing (and never re-engage) that
    deal. Marks the state ``halted``; a no-op when no state exists.
    """
    state = load_state(opportunity_id)
    if state is None:
        return {"ok": False, "reason": "no_state"}
    state.status = "halted"
    state.log("halted", reason=reason)
    save_state(state)
    _LOG.info("cash_engine halted opp=%s reason=%s", opportunity_id, reason)
    return {"ok": True, "status": "halted", "reason": reason}


# --------------------------------------------------------------------------
# SQS consumer — the production transport for samus-cash-engine-jobs
# --------------------------------------------------------------------------
# The mock JSONL queue (drain()) is the logic-first path. In production the
# front door enqueues to the SQS queue and this consumer pulls each job and
# walks it through process_job. BaseSqsWorker owns receive -> validate ->
# idempotency -> handle -> delete (+ DLQ redrive on failure); process_job is
# itself idempotent per deal, so an SQS redelivery is safe. Wrapped in
# try/except (matching the other workcell workers) so cash_engine.worker still
# imports cleanly — for the gateway drain route + the feedback bridge — even if
# the worker base / boto layer is unavailable.

_SQS_IMPORT_ERROR: Exception | None = None
try:
    from backend.common.aws_runtime import AwsRuntime, AwsWorkerSettings
    from backend.common.worker_base import BaseSqsWorker, serve_worker

    class CashEngineSqsWorker(BaseSqsWorker):
        """SQS consumer for the ``samus-cash-engine-jobs`` queue."""

        service = "cash_engine"

        def handle(self, envelope: Any) -> dict[str, Any]:
            payload = getattr(envelope, "payload", None) or {}
            task_id = getattr(envelope, "task_id", "") or ""
            state = process_job({"payload": payload, "task_id": task_id})
            if state is None:
                return {"ok": False, "reason": "no_state_produced", "task_id": task_id}
            return state.model_dump()

except Exception as exc:  # pragma: no cover - placeholder branch
    _SQS_IMPORT_ERROR = exc
    _LOG.warning(
        "BaseSqsWorker import failed (%s); CashEngineSqsWorker is a placeholder",
        exc,
    )

    AwsRuntime = None  # type: ignore[assignment]
    AwsWorkerSettings = None  # type: ignore[assignment]
    serve_worker = None  # type: ignore[assignment]

    class CashEngineSqsWorker:  # type: ignore[no-redef]
        """Placeholder until backend.common.worker_base is importable."""

        service = "cash_engine"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(
                "CashEngineSqsWorker is a placeholder until "
                f"backend.common.worker_base is available; import failed: {_SQS_IMPORT_ERROR!r}"
            )

        def handle(self, envelope: Any) -> dict[str, Any]:  # pragma: no cover
            raise NotImplementedError


def main() -> None:
    """Module entrypoint — ``python -m backend.cash_engine.worker`` (SQS consumer)."""
    if _SQS_IMPORT_ERROR is not None or serve_worker is None:
        raise NotImplementedError(
            "backend.common.worker_base is not available; cannot serve the "
            f"cash_engine worker. Original import error: {_SQS_IMPORT_ERROR!r}"
        )
    # Codex Validation Layer must be loaded before any stage runs — the
    # outreach stage's compose-body gate refuses fail-closed without it
    # (cash_engine outreach compose blocked: codex unavailable). HTTP apps
    # get this via app_factory; SQS workers must do it explicitly.
    from backend.common.app_factory import _ensure_codex_loaded

    _ensure_codex_loaded("cash_engine")
    settings = AwsWorkerSettings.from_env("cash_engine", "SQS_CASH_ENGINE_QUEUE_URL")  # type: ignore[union-attr]
    runtime = AwsRuntime(settings)  # type: ignore[misc]
    serve_worker(CashEngineSqsWorker(runtime))  # type: ignore[misc]


__all__ = ["process_job", "drain", "reengage", "halt", "CashEngineSqsWorker", "main"]


if __name__ == "__main__":
    main()
