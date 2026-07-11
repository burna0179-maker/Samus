"""Orchestrator — the supervised + autonomous entry to the build walk.

This is the one module callers touch. It enforces the engine-level dormancy
gate, builds the Wix transport from settings (or leaves it None so stages
park), and walks an order through the staged sequence honouring BOTH gates:

  * **Supervised** (default): ``advance`` runs exactly the next stage, and only
    if the operator has approved it (``approve_stage``). An unapproved next
    stage flips the order to ``awaiting_approval`` and returns — the walk
    pauses for the human. This is the walk-through loop: show -> approve ->
    advance -> record -> repeat.
  * **Autonomous** (``website_autonomous_enabled`` flipped on once the sequence
    is proven): ``run`` walks every remaining stage unattended, approval
    implied, halting only on a Codex block (escalate), a park (resumable), or
    completion. Same handlers, same state, same gates — the only difference is
    where approval comes from.

Nothing outward fires without ``website_live_publish_enabled``; no Wix call
fires without ``wix_api_key`` + ``wix_account_id``; the whole capability is off
unless ``website_builder_enabled``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable

from backend.common.settings import get_settings

from .gate import approval_ok, outward_ok
from .models import WebsiteOrder
from .stages import DEFAULT_HANDLERS, StageContext, StageResult
from .state import (
    DONE,
    STAGE_SEQUENCE,
    WebsiteBuildState,
    load_state,
    save_state,
)
from .wix_client import WixClient

_LOG = logging.getLogger("samus.website.service")

# StageResult.detail keys that map straight onto WebsiteBuildState.
_SCALAR_FIELDS = ("site_id", "site_url", "settlement_ref")


class WebsiteBuilderDisabled(Exception):
    """Raised when the capability is invoked while ``website_builder_enabled`` is off."""


def _build_wix_client(settings: Any) -> WixClient | None:
    """Construct the Wix transport from settings, or None if creds are unseeded."""
    api_key = str(getattr(settings, "wix_api_key", "") or "")
    if not api_key:
        return None
    return WixClient(api_key, account_id=str(getattr(settings, "wix_account_id", "") or ""))


def _apply_detail(state: WebsiteBuildState, result: StageResult) -> None:
    """Fold a successful (or partially-progressed) stage's detail into state."""
    for key in _SCALAR_FIELDS:
        if key in result.detail:
            setattr(state, key, str(result.detail[key]))
    if "site_id" in result.detail and not state.site_id:
        state.site_id = str(result.detail["site_id"])
    if "content_item_ids" in result.detail:
        state.content_item_ids = [i for i in result.detail["content_item_ids"] if i]
    if "preview_checklist" in result.detail:
        state.preview_checklist = result.detail["preview_checklist"]
    if "generation_report" in result.detail:
        state.generation_report = result.detail["generation_report"]
    if "seo_package" in result.detail:
        state.seo_package = result.detail["seo_package"]
    if "live_audit" in result.detail and result.detail["live_audit"]:
        state.live_audit = result.detail["live_audit"]
    if "media_report" in result.detail:
        state.media_report = result.detail["media_report"]


def start_order(
    order: WebsiteOrder,
    *,
    settings: Any = None,
) -> WebsiteBuildState:
    """Open (or reload) the build for ``order``. Idempotent on order_id."""
    settings = settings or get_settings()
    if not getattr(settings, "website_builder_enabled", False):
        raise WebsiteBuilderDisabled("website_builder_enabled is off")

    if not order.order_id:
        order.order_id = f"wb-{uuid.uuid4().hex[:12]}"

    existing = load_state(order.order_id)
    if existing is not None:
        return existing

    state = WebsiteBuildState(
        order_id=order.order_id,
        customer_name=order.customer_name,
        order=order,
        stage=STAGE_SEQUENCE[0],
        status="running",
    )
    state.log(
        "order_opened",
        customer=order.customer_name,
        settlement=order.settlement_kind,
        source=order.source,
    )
    save_state(state)
    return state


def approve_stage(order_id: str, stage: str) -> WebsiteBuildState | None:
    """Record an operator sign-off for ``stage`` (the supervised gate).

    This is the only way a stage is cleared to run in supervised mode. It never
    runs the stage — call :func:`advance` after approving.
    """
    state = load_state(order_id)
    if state is None:
        return None
    if stage not in STAGE_SEQUENCE:
        raise ValueError(f"unknown stage {stage!r}")
    if stage not in state.approved_stages:
        state.approved_stages.append(stage)
        state.log("stage_approved", stage=stage)
        # An approval clears an awaiting_approval pause.
        if state.status == "awaiting_approval" and state.stage == stage:
            state.status = "running"
        save_state(state)
    return state


def _next_stage(state: WebsiteBuildState) -> str | None:
    """The first stage in sequence not yet completed, or None if all done."""
    for stage in STAGE_SEQUENCE:
        if stage not in state.completed_stages:
            return stage
    return None


def _run_stage(
    state: WebsiteBuildState,
    stage: str,
    *,
    handlers: dict[str, Callable[[StageContext], StageResult]],
    settings: Any,
    crm: Any,
) -> WebsiteBuildState:
    """Execute one stage and fold its outcome into state. Persists."""
    order = state.order
    if order is None:
        state.status = "escalated"
        state.escalation = {"stage": stage, "reason": "order_missing_from_state"}
        save_state(state)
        return state

    ctx = StageContext(
        state=state,
        order=order,
        settings=settings,
        wix=_build_wix_client(settings),
        crm=crm,
        live_publish_enabled=bool(getattr(settings, "website_live_publish_enabled", False)),
    )
    state.stage = stage
    result = handlers[stage](ctx)

    if result.codex_blocked:
        state.status = "escalated"
        state.escalation = {
            "stage": stage,
            "violated_rule_id": result.violated_rule_id,
            "reason": result.reason,
            "drafted_adr_path": result.drafted_adr_path,
        }
        state.log("escalated", stage=stage, rule=result.violated_rule_id)
        save_state(state)
        _LOG.info("website order escalated id=%s stage=%s", state.order_id, stage)
        return state

    if result.parked:
        # Fold any partial progress before parking (durable resume).
        _apply_detail(state, result)
        state.status = "parked"
        state.park = {"stage": stage, "reason": result.park_reason}
        state.log("parked", stage=stage, reason=result.park_reason)
        save_state(state)
        _LOG.info(
            "website order parked id=%s stage=%s reason=%s",
            state.order_id,
            stage,
            result.park_reason,
        )
        return state

    _apply_detail(state, result)
    state.completed_stages.append(stage)
    state.status = "running"
    state.park = None
    state.log("stage_complete", stage=stage, **result.detail)
    save_state(state)

    if _next_stage(state) is None:
        state.status = DONE
        state.stage = DONE
        state.log("done")
        save_state(state)
        _LOG.info("website order done id=%s", state.order_id)
    return state


def advance(
    order_id: str,
    *,
    handlers: dict[str, Callable[[StageContext], StageResult]] | None = None,
    settings: Any = None,
    crm: Any = None,
) -> WebsiteBuildState | None:
    """Run exactly the next stage (supervised single-step), gates enforced.

    Returns the updated state. If the next stage is not yet operator-approved
    (and autonomous mode is off) the order is flipped to ``awaiting_approval``
    and NO stage runs. If the next stage is outward and the live flag is off,
    it parks. This is the walk-through's per-step call.
    """
    handlers = handlers or DEFAULT_HANDLERS
    settings = settings or get_settings()
    if not getattr(settings, "website_builder_enabled", False):
        raise WebsiteBuilderDisabled("website_builder_enabled is off")

    state = load_state(order_id)
    if state is None:
        return None
    if state.status in (DONE,):
        return state

    stage = _next_stage(state)
    if stage is None:
        state.status = DONE
        save_state(state)
        return state

    autonomous = bool(getattr(settings, "website_autonomous_enabled", False))

    # Operator-approval gate.
    if not approval_ok(state, stage, autonomous=autonomous):
        state.status = "awaiting_approval"
        state.stage = stage
        state.log("awaiting_approval", stage=stage)
        save_state(state)
        return state

    # Outward gate (defence-in-depth; the stage handler also parks on this).
    if not outward_ok(
        stage, live_publish_enabled=bool(getattr(settings, "website_live_publish_enabled", False))
    ):
        state.status = "parked"
        state.stage = stage
        state.park = {"stage": stage, "reason": "live_publish_disabled"}
        state.log("parked", stage=stage, reason="live_publish_disabled")
        save_state(state)
        return state

    return _run_stage(state, stage, handlers=handlers, settings=settings, crm=crm)


def run(
    order_id: str,
    *,
    handlers: dict[str, Callable[[StageContext], StageResult]] | None = None,
    settings: Any = None,
    crm: Any = None,
    max_stages: int = len(STAGE_SEQUENCE) + 1,
) -> WebsiteBuildState | None:
    """Walk every remaining stage until a stop condition (autonomous path).

    Stops on completion, a park, an escalation, or an awaiting_approval pause
    (the latter only happens when autonomous mode is off — so ``run`` in
    supervised mode walks only the already-approved prefix, then pauses). The
    ``max_stages`` bound is a belt-and-braces guard against a non-advancing loop.
    """
    handlers = handlers or DEFAULT_HANDLERS
    settings = settings or get_settings()
    state: WebsiteBuildState | None = None
    for _ in range(max_stages):
        state = advance(order_id, handlers=handlers, settings=settings, crm=crm)
        if state is None:
            return None
        if state.status in (DONE, "parked", "escalated", "awaiting_approval"):
            return state
    return state


def from_cash_engine_deal(
    *,
    opportunity_id: str,
    brief: Any,
    settings: Any = None,
    crm: Any = None,
) -> WebsiteBuildState:
    """Bridge: open a website order from a won Cash Engine deal.

    The seam a future autonomous run uses — the cash engine's dormant deal,
    once won, hands its opportunity + a derived brief here. Kept thin and
    explicit so wiring it into the cash_engine forward walk (a ``deliver``
    stage) is an additive change, not a rewrite.
    """
    if crm is None:
        from backend.crm import service as crm  # local import: avoid cycle
    opp = crm.get_opportunity(opportunity_id)
    customer = str(getattr(opp, "company_name", "") or opportunity_id) if opp else opportunity_id
    order = WebsiteOrder(
        customer_name=customer,
        brief=brief,
        opportunity_id=opportunity_id,
        prospect_id=str(getattr(opp, "prospect_id", "") or "") if opp else "",
        source="cash_engine",
    )
    return start_order(order, settings=settings)


__all__ = [
    "WebsiteBuilderDisabled",
    "start_order",
    "approve_stage",
    "advance",
    "run",
    "from_cash_engine_deal",
]
