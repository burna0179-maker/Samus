"""Stage handlers — the staged sequence behind the front door.

Each stage is a handler ``(StageContext) -> StageResult``. The default
handlers run the *self-contained* leaves for real in-process — the SEO audit
(Gap Report), the callsheet, the voicemail draft, the follow-up schedule —
because those need only data we already hold (the prospect row, the Stake
Sentence). The *heavy, separately-owned* leaves — full proposal generation
(needs an OnboardingIntake) and the live email send (needs the outreach
workcell + SES) — are NOT re-implemented here (no parallel systems); they are
delegated to their workcells via the same dispatch seam the gateway's
``/dispatch/{target}`` uses. When no route is wired (the logic-first / mock
default), the stage *parks* honestly rather than faking a result.

Every revenue-critical handoff re-validates through the Codex
(:func:`backend.common.codex.validator.check_action`) — a blocked verdict
halts that job and escalates, exactly like the front door.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from backend.common.codex.exceptions import CodexUnavailable
from backend.common.codex.models import ProposedAction, Verdict
from backend.common.codex.validator import check_action
from backend.common.dates import hours_from_now
from backend.common.settings import get_settings
from backend.crm.models import AdvanceOpportunityRequest, CallState, CreateArtifactRequest

from .state import CashEngineState

_LOG = logging.getLogger("samus.cash_engine.stages")

# How long after the initial outreach the follow-up call is scheduled.
_FOLLOWUP_HOURS = 72


@dataclass
class StageContext:
    """Everything a stage handler needs to do its work."""

    state: CashEngineState
    opportunity: Any  # crm Opportunity
    prospect: Any  # crm Prospect | None
    stake_sentence: str
    crm: Any  # crm service module
    settings: Any = None
    # SIMULATE mode (HOTL T5): when True the external-effect stages
    # (outreach, deliver) return their PREDICTED effect + cost instead of
    # performing the real send / builder handoff. Internal stages ignore it.
    dry_run: bool = False


@dataclass
class StageResult:
    """Outcome of one stage."""

    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)
    # A stage that needs an external route/input not yet wired -> resumable.
    parked: bool = False
    park_reason: str = ""
    # A stage whose Codex gate blocked -> operator must resolve.
    codex_blocked: bool = False
    violated_rule_id: str | None = None
    reason: str | None = None
    drafted_adr_path: str | None = None


def codex_gate(
    *,
    action_kind: str,
    payload: dict[str, Any],
    capability: str,
) -> Verdict:
    """Run a handoff through the Codex; fail-closed on an unavailable registry."""
    action = ProposedAction(
        service="cash_engine",
        capability=capability,
        action_kind=action_kind,  # type: ignore[arg-type]
        payload=payload,
        proposed_by="cash_engine",
    )
    try:
        return check_action(action)
    except CodexUnavailable as exc:
        return Verdict(
            allowed=False,
            violated_rule_id="CODEX_UNAVAILABLE",
            reason=f"Codex registry unavailable; refusing handoff (fail-closed): {exc}",
        )


def _blocked_result(stage: str, verdict: Verdict) -> StageResult:
    _LOG.info(
        "cash_engine stage %s BLOCKED by Codex rule=%s",
        stage,
        verdict.violated_rule_id,
    )
    return StageResult(
        ok=False,
        codex_blocked=True,
        violated_rule_id=verdict.violated_rule_id,
        reason=verdict.reason,
        drafted_adr_path=verdict.drafted_adr_path,
    )


# --------------------------------------------------------------------------
# Default stage handlers
# --------------------------------------------------------------------------


def _audit_stage(ctx: StageContext) -> StageResult:
    """Gap Report — real, self-contained SEO audit on the prospect's site."""
    url = str(getattr(ctx.prospect, "website_url", "") or "") if ctx.prospect else ""
    if not url:
        return StageResult(ok=False, parked=True, park_reason="no_website_url")

    from backend.seo.audit import audit_url  # local import: heavy module

    audit = audit_url(url, industry=str(getattr(ctx.prospect, "industry", "") or ""))
    evidence = {k: str(v) for k, v in (audit.evidence_sources or {}).items()}

    verdict = codex_gate(
        action_kind="gap_report_render",
        capability="gap_report",
        payload={"evidence_sources": evidence, "url": url},
    )
    if not verdict.allowed:
        return _blocked_result("audit", verdict)

    artifact = ctx.crm.create_artifact(
        CreateArtifactRequest(
            kind="seo_audit",
            owner_entity_kind="opportunity",
            owner_entity_id=ctx.opportunity.opportunity_id,
            title=f"Gap Report: {url}",
            inline_data={
                "url": url,
                "seo_score": audit.seo_score,
                "issue_count": len(audit.issues),
                "evidence_sources": evidence,
            },
            source="cash_engine",
            created_by="cash_engine",
        )
    )
    return StageResult(
        ok=True,
        detail={
            "gap_report_artifact_id": artifact.artifact_id,
            "seo_score": audit.seo_score,
        },
    )


def _proposal_stage(ctx: StageContext) -> StageResult:
    """Proposal — synthesize an intake + run the deterministic generator real.

    The proposal pipeline is pure template logic (no LLM, no network), so it
    runs safely in-process. The OnboardingIntake it needs is synthesized from
    the prospect's signals by the proposal workcell's own ``intake_synth`` (no
    parallel input system). ``generate_proposal`` is called with empty CRM ids
    so it does NOT self-dispatch an artifact over SQS — the cash engine
    registers the artifact itself, the single reliable source of truth.
    """
    verdict = codex_gate(
        action_kind="proposal_generate",
        capability="proposal",
        payload={"stake_sentence": ctx.stake_sentence},
    )
    if not verdict.allowed:
        return _blocked_result("proposal", verdict)

    from backend.proposal.intake_synth import synthesize_intake
    from backend.proposal.models import ProposalRequest
    from backend.proposal.service import generate_proposal

    intake = synthesize_intake(
        company_name=str(getattr(ctx.prospect, "company_name", "") or "") or ctx.state.prospect_id,
        industry=str(getattr(ctx.prospect, "industry", "") or ""),
        business_description=str(getattr(ctx.prospect, "business_description", "") or ""),
        stake_sentence=ctx.stake_sentence,
    )
    result = generate_proposal(
        ProposalRequest(
            task_id=f"{ctx.state.task_id or ctx.opportunity.opportunity_id}-proposal",
            intake=intake,
        )
    )
    total_steps = result.workflow.total_steps if result.workflow else 0
    artifact = ctx.crm.create_artifact(
        CreateArtifactRequest(
            kind="proposal",
            owner_entity_kind="opportunity",
            owner_entity_id=ctx.opportunity.opportunity_id,
            title=f"Proposal: {intake.client_name}",
            inline_data={
                "status": result.status,
                "stage": getattr(result.stage, "value", str(result.stage)),
                "total_steps": total_steps,
                "client_name": intake.client_name,
            },
            source="cash_engine",
            created_by="cash_engine",
        )
    )
    # G-REC-001 (Gamma) / Beta-VALIDATED gate-1 2026-07-08: the proposal artifact
    # was created but the opportunity stage was never advanced, so the funnel
    # `proposal` counter + PROPOSAL_SENT event never fired (funnel showed
    # proposal=0 while proposals were really being generated). Advance
    # new->proposal here — AFTER the artifact is created, so a failed proposal
    # never advances the stage. Idempotent: a re-run hits
    # validate_transition('proposal','proposal') -> invalid_transition and
    # returns before the funnel emit (no double-count). Non-"advanced" outcomes
    # are logged, not raised — the artifact still stands and the stage is honest.
    advance = ctx.crm.advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id=ctx.opportunity.opportunity_id,
            target_stage="proposal",
        )
    )
    if advance.status != "advanced":
        _LOG.info(
            "cash_engine proposal: opp %s not advanced to 'proposal' "
            "(status=%s prior=%s) — artifact %s still created",
            ctx.opportunity.opportunity_id,
            advance.status,
            getattr(advance, "prior_stage", ""),
            artifact.artifact_id,
        )
    return StageResult(
        ok=True,
        detail={
            "proposal_ref": artifact.artifact_id,
            "proposal_status": result.status,
            "opp_advanced": advance.status == "advanced",
        },
    )


def _contact_stage(ctx: StageContext) -> StageResult:
    """Contact artifacts — real callsheet + voicemail draft for the operator."""
    if ctx.prospect is None:
        return StageResult(ok=False, parked=True, park_reason="no_prospect")

    from backend.prospecting.callsheet import _voicemail, build_call_sheet

    opp_id = ctx.opportunity.opportunity_id
    sheet = build_call_sheet(
        ctx.prospect,
        stake_sentence=ctx.stake_sentence,
        opportunity_id=opp_id,
    )
    voicemail = _voicemail(ctx.prospect, ctx.stake_sentence)

    # G2 banned-phrase scan over the drafted voicemail script before it is
    # ever put in front of the operator to record.
    verdict = codex_gate(
        action_kind="other",
        capability="contact_artifacts",
        payload={"voicemail_script": voicemail, "stake_sentence": ctx.stake_sentence},
    )
    if not verdict.allowed:
        return _blocked_result("contact", verdict)

    sheet_fields = {
        k: v
        for k, v in sheet.model_dump().items()
        if k.startswith("callsheet") or k in ("company_name", "phone", "website_url")
    }
    callsheet_art = ctx.crm.create_artifact(
        CreateArtifactRequest(
            kind="call_sheet",
            owner_entity_kind="opportunity",
            owner_entity_id=opp_id,
            title="Callsheet",
            inline_data=sheet_fields,
            source="cash_engine",
            created_by="cash_engine",
        )
    )
    voicemail_art = ctx.crm.create_artifact(
        CreateArtifactRequest(
            kind="voicemail",
            owner_entity_kind="opportunity",
            owner_entity_id=opp_id,
            title="Voicemail script draft",
            inline_data={"script": voicemail, "stake_sentence": ctx.stake_sentence},
            source="cash_engine",
            created_by="cash_engine",
        )
    )
    return StageResult(
        ok=True,
        detail={
            "callsheet_artifact_id": callsheet_art.artifact_id,
            "voicemail_artifact_id": voicemail_art.artifact_id,
        },
    )


def _outreach_stage(ctx: StageContext) -> StageResult:
    """Outreach -- compose the packet (real, Codex-gated), draft + schedule it.

    Composition runs through outreach's own ``compose_body`` (stake guard +
    the authoritative ``outreach_send`` Codex gate G1/G8/G2 + CAN-SPAM
    footer); a block raises and we cascade to the next ring.  The composed
    packet is registered as a reviewable draft and the follow-up is scheduled
    in the dialer FSM -- both durable, self-contained.  The LIVE SES send
    happens only behind the explicit ``cash_engine_live_send_enabled`` flag
    with a configured sender, so the sequence completes end-to-end without
    ever firing real email by default.

    Ring cascade (2026-07-07): when email is blocked (no cold-sendable
    address) or the daily email cap is reached, the stage advances the
    prospect to the next reachable outreach ring (voice/voicemail/social)
    instead of parking dormant.  Park only when EVERY ring is exhausted.
    """
    if ctx.prospect is None:
        return StageResult(ok=False, parked=True, park_reason="no_prospect")

    # SIMULATE (HOTL T5): predict the outreach effect + cost without composing,
    # sending, or scheduling. A recipient present => would send at ~list price.
    if ctx.dry_run:
        to = str(getattr(ctx.prospect, "email", "") or "").strip()
        would_send = bool(to)
        return StageResult(
            ok=True,
            detail={
                "dry_run": True,
                "predicted_action": "outreach_send",
                "would_send": would_send,
                "recipient_present": would_send,
                "predicted_cost_usd": 0.0001 if would_send else 0.0,
            },
        )

    from backend.cash_engine.ring_cascade import cascade_from_email, is_email_cap_reached

    # Quota-aware capacity shifting: when the daily EMAIL cap is already
    # reached, skip email entirely and cascade to non-email rings so total
    # outreach continues to the system's overall capacity.
    if is_email_cap_reached():
        _LOG.info("cash_engine outreach: daily email cap reached, cascading")
        cascade = cascade_from_email(
            ctx.prospect,
            ctx.opportunity,
            stake_sentence=ctx.stake_sentence,
            crm=ctx.crm,
            reason="email_daily_cap_reached",
        )
        if cascade.get("cascaded"):
            return StageResult(
                ok=True,
                detail={
                    "ring_cascade": True,
                    "ring_cascade_reason": "email_daily_cap_reached",
                    **cascade.get("enrollment", {}),
                },
            )
        return StageResult(
            ok=False,
            parked=True,
            park_reason="all_outreach_rings_exhausted",
        )

    # The open Opportunity IS the warmth signal G8 requires -- prior engagement,
    # not a cold-from-nowhere blast.
    legitimacy = f"existing_opportunity:{getattr(ctx.opportunity, 'stage', '')}"
    # Soft-no resurfacing path: state.trigger_source == "reengagement" means
    # the re-engagement sweep (backend/crm/reengagement_sweep.py) requeued
    # this prospect after the cooldown. Route compose through the promotional
    # template so the recipient doesn't get a duplicate of the cold opener
    # they already declined.
    is_reengagement = ctx.state.trigger_source == "reengagement"

    from backend.outreach.cash_engine_entry import (
        OutreachStakeMissing,
        compose_initial_packet,
        compose_reengagement_packet,
    )

    try:
        if is_reengagement:
            packet = compose_reengagement_packet(
                prospect=ctx.prospect,
                stake_sentence=ctx.stake_sentence,
                legitimacy_signal=legitimacy,
            )
        else:
            packet = compose_initial_packet(
                prospect=ctx.prospect,
                stake_sentence=ctx.stake_sentence,
                legitimacy_signal=legitimacy,
            )
    except OutreachStakeMissing as exc:
        _LOG.info("cash_engine outreach compose blocked: %s", exc)
        cascade = cascade_from_email(
            ctx.prospect,
            ctx.opportunity,
            stake_sentence=ctx.stake_sentence,
            crm=ctx.crm,
            reason=f"email_compose_blocked: {exc}",
        )
        if cascade.get("cascaded"):
            return StageResult(
                ok=True,
                detail={
                    "ring_cascade": True,
                    "ring_cascade_reason": "email_compose_blocked",
                    **cascade.get("enrollment", {}),
                },
            )
        return StageResult(
            ok=False,
            parked=True,
            park_reason="all_outreach_rings_exhausted",
        )

    opp_id = ctx.opportunity.opportunity_id
    draft = ctx.crm.create_artifact(
        CreateArtifactRequest(
            kind="content_draft",
            owner_entity_kind="opportunity",
            owner_entity_id=opp_id,
            title="Initial outreach draft",
            inline_data={
                "subject": packet["subject"],
                "body": packet["body"],
                "to": packet["to"],
                "legitimacy_signal": legitimacy,
            },
            source="cash_engine",
            created_by="cash_engine",
        )
    )

    # Index this recipient so a future SES bounce/complaint on this address
    # resolves back to the deal (feedback -> halt). Best-effort; never blocks
    # outreach. Activates once a real send + a real bounce occur.
    if packet.get("to"):
        try:
            from backend.common.recipient_index import record_recipient

            record_recipient(
                email=packet["to"],
                prospect_id=ctx.state.prospect_id,
                opportunity_id=opp_id,
                source="cash_engine_outreach",
            )
        except Exception as exc:  # noqa: BLE001 — index write never blocks outreach
            _LOG.warning("cash_engine recipient_index record failed: %s", exc)

    # Schedule the next follow-up in the dialer FSM — durable, self-contained.
    scheduled_for = hours_from_now(_FOLLOWUP_HOURS)
    prior = None
    try:
        prior = ctx.crm.get_call_state(ctx.state.prospect_id)
    except Exception:  # noqa: BLE001 — optional prior state
        prior = None
    attempts = (getattr(prior, "attempt_count", 0) or 0) + 1
    ctx.crm.upsert_call_state(
        CallState(
            prospect_id=ctx.state.prospect_id,
            state="outreach_sent",
            attempt_count=attempts,
            next_attempt_at=scheduled_for,
            last_outcome="cash_engine: initial outreach drafted + scheduled",
            notes=f"cash_engine outreach for opp {opp_id}",
        )
    )

    # Live send behind the explicit flag AND a configured sender. Gate on
    # the ACTIVE backend's sender, not SES specifically: the stack's primary
    # backend is SendGrid (sendgrid_from_email) and requiring an empty
    # ses_from_email here silently skipped every autonomous send (2026-06-13
    # day-one zero-email root cause; rediscovered 2026-06-22). send_message
    # routes through the email_backend selector.
    ref = draft.artifact_id
    settings = ctx.settings or get_settings()
    _active_sender = str(getattr(settings, "ses_from_email", "") or "") or str(
        getattr(settings, "sendgrid_from_email", "") or ""
    )
    # Heat Field safe-mode: when the reputation band is critical AND the field
    # is armed, pause autonomous live sends (the draft + follow-up are still
    # written below). No-op (False) when heat_field_enabled is off.
    _heat_paused = False
    try:
        from backend.heat import service as _heat

        _heat_paused = _heat.is_send_paused_now()
    except Exception:  # noqa: BLE001 — heat must never break the send path
        _heat_paused = False
    # Select a variant arm for the UCB1 bandit before the send so every email
    # is tagged and outcomes can flow back to the attribution engine.
    _chosen_arm = ""
    try:
        from backend.attribution.engine import build_arm_id, select_variant

        _tpl = "cash_engine_reengagement" if is_reengagement else "cash_engine_initial"
        _choice = select_variant([build_arm_id(_tpl)])
        _chosen_arm = _choice.arm_id or ""
    except Exception as exc:  # noqa: BLE001 — attribution must never block sends
        _LOG.debug("cash_engine arm selection skipped: %s", exc)

    if (
        getattr(settings, "cash_engine_live_send_enabled", False)
        and _active_sender
        and packet["to"]
        and not _heat_paused
    ):
        # Render the vibrant HTML flyer for this prospect so the autonomous
        # send matches the morning_batch path (both now ship rich HTML, not
        # plain text). Fail-SOFT: any render fault leaves html_body empty and
        # the plain-text body still sends exactly as before — a flyer is
        # additive, never a send blocker. (2026-07-07 operator directive:
        # promo emails were text-heavy; this closes the text-only leak on the
        # autonomous revenue path.)
        from backend.outreach.flyer import flyer_html_for

        _html_body = flyer_html_for(
            prospect_id=ctx.state.prospect_id,
            to_email=packet["to"],
            prospect=ctx.prospect,
            stake=str(ctx.stake_sentence or ""),
            settings=settings,
        )

        try:
            from backend.outreach.models import OutreachMessageRequest
            from backend.outreach.service import send_message

            sent = send_message(
                OutreachMessageRequest(
                    prospect_id=ctx.state.prospect_id,
                    channel="email",
                    template_id=(
                        "cash_engine_reengagement" if is_reengagement else "cash_engine_initial"
                    ),
                    body=packet["body"],
                    html_body=_html_body,
                    to=packet["to"],
                    subject=packet["subject"],
                    company=str(getattr(ctx.prospect, "company_name", "") or ""),
                    phone=str(getattr(ctx.prospect, "phone", "") or ""),
                    variant_arm_id=_chosen_arm,
                )
            )
            ref = str(sent.get("message_id") or ref)
        except Exception as exc:  # noqa: BLE001 — draft + schedule already durable
            _LOG.warning("cash_engine live send failed (draft retained): %s", exc)

    return StageResult(
        ok=True,
        detail={
            "outreach_scheduled_for": scheduled_for,
            "outreach_ref": ref,
            "variant_arm_id": _chosen_arm,
        },
    )


def _deliver_stage(ctx: StageContext) -> StageResult:
    """Deliver — bridge a WON deal to the website builder (the leaf deliverable).

    This is the seam :func:`backend.website.service.from_cash_engine_deal`
    exists for. It fires only when the Opportunity has actually closed
    (``stage == "closed_won"`` / ``"closed_won_retainer"``): a deal that has
    only been outreached is not yet ours to build, so the stage PARKS honestly
    and the deal rests dormant until a re-engagement signal (a reply / a manual
    close) advances it. No stub: when the deal is won AND the website builder is
    enabled, it opens a real WebsiteBuildState; when the builder is disabled it
    parks rather than faking an order.
    """
    opp = ctx.opportunity
    stage = str(getattr(opp, "stage", "") or "")
    if stage not in ("closed_won", "closed_won_retainer"):
        # Not won yet — honest park. The deal goes dormant; a future close
        # (re-engagement) re-runs the walk and this stage will fire then.
        return StageResult(ok=False, parked=True, park_reason=f"deal_not_won:{stage or 'unknown'}")

    # SIMULATE (HOTL T5): predict the deliver hand-off without opening a real
    # WebsiteBuildState. A won deal => would open a build (no direct $ cost;
    # the build's own spend is metered downstream).
    if ctx.dry_run:
        return StageResult(
            ok=True,
            detail={
                "dry_run": True,
                "predicted_action": "website_deliver",
                "would_open_build": True,
                "stage": stage,
                "predicted_cost_usd": 0.0,
            },
        )

    # Codex gate on the handoff to the website workcell (revenue-critical).
    verdict = codex_gate(
        action_kind="other",
        capability="website_deliver",
        payload={"opportunity_id": opp.opportunity_id, "stage": stage},
    )
    if not verdict.allowed:
        return _blocked_result("deliver", verdict)

    # Derive a thin brief from what we hold; the website workcell's own content
    # generation expands it. Kept explicit so no parallel intake system appears.
    brief = {
        "company_name": str(getattr(ctx.prospect, "company_name", "") or "")
        or str(getattr(opp, "company_name", "") or ctx.state.prospect_id),
        "industry": str(getattr(ctx.prospect, "industry", "") or ""),
        "business_description": str(getattr(ctx.prospect, "business_description", "") or ""),
        "stake_sentence": ctx.stake_sentence,
        "source_opportunity_id": opp.opportunity_id,
    }

    from backend.website import service as website_service

    try:
        build = website_service.from_cash_engine_deal(
            opportunity_id=opp.opportunity_id,
            brief=brief,
            settings=ctx.settings,
            crm=ctx.crm,
        )
    except website_service.WebsiteBuilderDisabled as exc:
        # Builder off -> park honestly (the won deal is recorded; the build
        # opens once the operator arms website_builder_enabled).
        _LOG.info("cash_engine deliver parked (builder disabled): %s", exc)
        return StageResult(ok=False, parked=True, park_reason="website_builder_disabled")

    return StageResult(
        ok=True,
        detail={
            "website_order_id": str(getattr(build, "order_id", "") or ""),
        },
    )


# Stage name -> handler. Injectable wholesale in tests / future HTTP fan-out.
DEFAULT_HANDLERS: dict[str, Callable[[StageContext], StageResult]] = {
    "audit": _audit_stage,
    "proposal": _proposal_stage,
    "contact": _contact_stage,
    "outreach": _outreach_stage,
    "deliver": _deliver_stage,
}


__all__ = [
    "StageContext",
    "StageResult",
    "codex_gate",
    "DEFAULT_HANDLERS",
]
