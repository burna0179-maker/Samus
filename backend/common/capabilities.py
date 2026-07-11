"""Capability enforcement + caller→callee authorization (R-1 remediation).

Two layers live here:

1. ``SERVICE_CAPABILITIES`` + ``check_capability(service, capability)`` — the
   ORIGINAL static "does this workcell expose this capability" lookup. Every
   workcell route calls it at the top before business logic (doc §3.3). It is
   kept verbatim for backward compatibility — there are ~150 call sites.

2. ``CALLER_GRANTS`` + ``authorize(caller, callee, capability)`` — the REAL
   authorization layer added for finding R-1 / M1. ``check_capability`` only
   ever inspected the callee's *own* capability set, so it could never deny a
   genuine request and provided no least-privilege segmentation. The grant
   matrix declares, per *calling* service, which (callee, capability) pairs it
   is allowed to invoke. Deny-by-default.

Enforcement is feature-flagged (``SAMUS_AUTHZ_MODE``) so this code ships safe
and dormant — see ``authz_mode()`` and the three modes documented there.

``check_capability`` now ALSO consults the matrix, but ONLY when the authz
mode is ``audit`` or ``enforce`` AND a verified caller identity is available
on the request. In the default ``off`` mode it behaves exactly as before:
the static capability-set check and nothing else. That is the live-stack
safety valve — deploying this file changes no behaviour until the operator
opts in.
"""

from __future__ import annotations

import logging
import os
from typing import Final

from fastapi import HTTPException
from starlette.requests import Request


_LOG = logging.getLogger("samus.authz")


SERVICE_CAPABILITIES: Final[dict[str, set[str]]] = {
    "gateway": {
        "dispatch",
        "dlq_read",
        "autonomy_plan",
        "budget_admin",
        "list_tasks",
        "control_tick",
        "crm_stats",
        "journey_read",
    },
    "leadgen": {"score", "qualify"},
    "prospecting": {
        "discover",
        "build_call_sheet",
        "analyze_business",
        "score_deal",
        "generate_dynamic_script",
        "generate_dynamic_script_with_pivot",
    },
    "scaffold": {"generate_assets"},
    "fulfillment": {
        "plan_execution",
        "validate_plan",
        "execute_plan",
        "ingest_result",
        "resume_plan",
        "finalize_plan",
    },
    "memory": {
        "read",
        "write",
        "query",
        "delete",
        "stats",
        "graph",
        "customers",
        "ingest_knowledge",
    },
    "feedback": {"ingest"},
    "outreach": {
        "advance_call",
        "log_outcome",
        "send_message",
        "handle_objection",
        "advance_call_state",
        "send_social_post",
        "compose_and_send_social_post",
        # Growth-enrichment Phase D/E (dormant: DRY-RUN / plan-only).
        "repurpose_blog_post",
        "plan_social_calendar",
        "dispatch_social_calendar",
        "plan_nurture",
    },
    "optimizer": {"select_arm", "update_arm", "optimize_portfolio"},
    "proposal": {"generate_proposal", "validate_proposal"},
    "seo": {
        "audit_site",
        "optimize_page",
        "generate_content",
        "audit_and_report",
        # Growth-enrichment Phase B/C (GEO formatting + AIO measurement;
        # aio_probe is itself flag-gated default-OFF — no token spend).
        "geo_format",
        "aio_analyze",
        "aio_probe",
    },
    "finance": {
        "snapshot",
        "codb_summary",
        "runway",
        "liabilities",
        "declines",
        "debts",
        "actions",
        "info_gaps",
        "hardship",
        "payment_links",
        "stripe_webhook",
        "recent_payments",
        "customer_summary",
        "report_meter_event",
        "customer_exists",
    },
    "voice": {
        "initiate_call",
        "fetch_call",
        "list_calls",
        "handle_webhook",
        "adapt_call",
        "handle_inbound",
        "send_call_summary",
    },
    "intake": {"submit_lead", "list_leads", "poll_inbox"},
    "crm": {
        "read_prospects",
        "read_contacts",
        "read_conversations",
        "read_call_state",
        "read_opportunities",
        "read_tasks",
        "read_artifacts",
        "convert_lead",
        "write_conversation",
        "write_call_state",
        "write_task",
        "update_task",
        "write_opportunity",
        "advance_opportunity",
        "write_artifact",
        "find_opportunity_for_email",
        "log_feedback",
        "get_feedback_snapshot",
        "auto_create_opportunity",
        # Growth-enrichment Phase F (proof + referral; generation /
        # local-ledger only — no outward send, no payout).
        "generate_case_study",
        "build_proof_wall",
        "referral_code",
        "referral_record",
        "referral_qualify",
    },
    "strategy": {
        "evaluate",
        "dispatch_strategy_action",
        "record_outcome",
        "build_context",
        "rank_portfolio",
        "propose_allocation",
        "update_bandit_arm",
        "forecast_allocation",
        "compile_policy",
    },
    # Campaign Orchestrator — composes existing workcells from a declarative
    # graph. It exposes its own run-lifecycle + KPI + reporting capabilities;
    # the outward node work is dispatched THROUGH the gateway (see CALLER_GRANTS
    # below), never by reaching peer workcells directly.
    "campaigns": {
        "create_campaign",
        "start_campaign",
        "advance_campaign",
        "approve_node",
        "update_kpis",
        "ingest_kpi",
        "generate_report",
        "read_status",
    },
    # Autonomous workcell expansion — observe/decide/recover/coordinate layer.
    # All five register the single capability ``plan_execution`` (namespaced
    # per-service; reuse of the string the fulfillment workcell also owns is
    # intentional and collision-free).
    "signal_filter": {"plan_execution"},
    "path_optimizer": {"plan_execution"},
    "template_recovery": {"plan_execution"},
    "portfolio_controller": {"plan_execution"},
    "entropy": {"plan_execution"},
}


# ---------------------------------------------------------------------------
# R-1 caller→callee authorization matrix.
# ---------------------------------------------------------------------------
#
# Sentinel caller identities (set by VerifyHMACMiddleware on request.state):
#   "external"  — an exempt/unsigned path (/health, /metrics, Stripe / Vapi /
#                 SNS webhooks, the public intake form). These paths carry
#                 their OWN signature scheme or are intentionally public; the
#                 caller-authz matrix does not gate them (their handler is the
#                 boundary contract). They are listed below for documentation.
#   "gateway"   — the gateway workcell. It is the operator-facing dispatcher
#                 and the signer side of the mesh; it is granted EVERY callee
#                 capability via the "*" wildcard.
#   "unknown"   — caller could not be resolved (no X-Samus-Caller header on a
#                 signed legacy request). Deny-by-default applies.
#
# The grants below were seeded from the ACTUAL call graph — every
# cross-workcell ``signed_post_json`` / ``signed_post_json_sync`` site in
# backend/ was read to learn who legitimately calls whom:
#
#   gateway      -> every workcell        (it dispatches /work + /dispatch/*)
#   fulfillment  -> every workcell        (dag.py executes plan steps by
#                                          dispatching {service}.{action} to
#                                          the gateway, which fans out)
#   crm          -> strategy              (crm/service.py:1030 strategy decide)
#   outreach     -> crm                   (outreach/service.py:182 CRM dispatch)
#   voice        -> crm, memory, finance  (voice/service.py + voice/inbound.py)
#   finance      -> gateway (->crm)       (finance/webhook.py:449 /dispatch/crm)
#   proposal     -> gateway (->crm)       (proposal/service.py:68 dispatch)
#   seo          -> gateway (->crm)       (seo/service.py:79 dispatch)
#   strategy     -> gateway, crm          (strategy/dispatcher.py + crm_client)
#   retainer     -> crm                   (retainer/* enroll + monthly_cycle)
#
# Workcells that dispatch *through* the gateway (proposal/seo/finance/strategy
# -> POST /dispatch/crm) only need the "dispatch" grant on the gateway; the
# gateway then re-signs and calls CRM under its own identity. They do NOT need
# a direct CRM grant. Callers that hit a workcell *directly* (outreach->crm,
# voice->crm/memory/finance, crm->strategy, strategy->crm via crm_client) need
# the explicit callee grant.
#
# A grant value of {"*"} means "every capability the callee exposes".
# Each grant set is otherwise an explicit subset of the callee's
# SERVICE_CAPABILITIES — deny-by-default for everything not listed.
#
# Generosity vs tightness: the matrix is deliberately generous on the
# capabilities a legitimate caller needs (CRM reads + writes for the workers
# that feed the pipeline) but tight on the caller axis — a compromised
# `seo` workcell, for example, has NO direct grant to CRM at all; its only
# mesh privilege is "dispatch" on the gateway.
# ---------------------------------------------------------------------------

# Caller identities that are NOT subject to the matrix — their requests reach
# a handler that enforces its own signature/auth scheme, or are intentionally
# public. VerifyHMACMiddleware tags these requests with caller "external".
EXTERNAL_CALLER: Final[str] = "external"
UNKNOWN_CALLER: Final[str] = "unknown"
GATEWAY_CALLER: Final[str] = "gateway"

# Wildcard grant token.
_ALL: Final[frozenset[str]] = frozenset({"*"})

CALLER_GRANTS: Final[dict[str, dict[str, frozenset[str]]]] = {
    # The gateway is the dispatcher + signer side of the mesh. It legitimately
    # reaches every workcell's /work surface and every capability.
    "gateway": {
        "*": _ALL,
    },
    # The fulfillment DAG executor dispatches arbitrary {service}.{action}
    # plan steps. Those go through the gateway in production, but fulfillment
    # is also a direct caller in some recovery paths — grant it the full mesh
    # so a legitimate plan never wedges. (Plan content itself is governance-
    # gated upstream in gateway/app.py:/autonomy/plan.)
    "fulfillment": {
        "*": _ALL,
    },
    # The CRM workcell calls strategy to get a per-prospect decision.
    "crm": {
        "strategy": frozenset(
            {"evaluate", "dispatch_strategy_action", "record_outcome", "build_context"}
        ),
    },
    # Outreach writes call outcomes / conversations / tasks back to CRM.
    "outreach": {
        "crm": frozenset(
            {
                "read_prospects",
                "read_contacts",
                "read_call_state",
                "read_opportunities",
                "read_conversations",
                "write_conversation",
                "write_call_state",
                "write_task",
                "update_task",
                "advance_opportunity",
                "find_opportunity_for_email",
            }
        ),
    },
    # Voice writes transcripts to memory, conversations / call-state / tasks
    # to CRM, and meter events to finance. It also confirms a receptionist's
    # stripe_customer_id with finance (customer_exists) before metering (FIN-08).
    "voice": {
        "memory": frozenset({"write", "read", "query"}),
        "crm": frozenset(
            {
                "read_prospects",
                "read_call_state",
                "read_conversations",
                "read_opportunities",
                "write_conversation",
                "write_call_state",
                "write_task",
                "update_task",
                "write_artifact",
                "advance_opportunity",
                "find_opportunity_for_email",
            }
        ),
        "finance": frozenset({"report_meter_event", "customer_exists"}),
    },
    # Finance dispatches CRM close-the-loop work THROUGH the gateway, and
    # never touches CRM directly. Only the gateway dispatch grant is needed.
    "finance": {
        "gateway": frozenset({"dispatch"}),
    },
    # SEO + proposal are sync producers that dispatch CRM artifact writes
    # through the gateway only.
    "seo": {
        "gateway": frozenset({"dispatch"}),
    },
    "proposal": {
        "gateway": frozenset({"dispatch"}),
    },
    # Strategy dispatches engine actions through the gateway AND reads
    # prospect records straight from CRM via crm_client.
    "strategy": {
        "gateway": frozenset({"dispatch"}),
        "crm": frozenset({"read_prospects", "read_opportunities", "read_conversations"}),
    },
    # The retainer subsystem enrolls opportunities + files operator tasks in
    # CRM. It runs inside the finance/crm worker processes; treat it as its
    # own caller for least-privilege clarity. (Today retainer code signs as
    # whatever SAMUS_SERVICE its host process carries; this grant is here so
    # that if/when retainer ever runs standalone it is already covered.)
    "retainer": {
        "crm": frozenset({"write_opportunity", "advance_opportunity", "write_task", "update_task"}),
    },
    # Prospecting reads CRM state to enrich its own pipeline output:
    # _backfill_recent_contact (call-state -> last_contact_* on the call
    # list; silently empty since authz enforce armed — found 2026-07-03) and
    # the recycle pass's prior-touch digest (touch_context: conversations +
    # call-state -> follow-up context on returning prospects). READ-ONLY —
    # prospect persistence goes through crm.persistence (DDB), not this seam.
    "prospecting": {
        "crm": frozenset({"read_prospects", "read_call_state", "read_conversations"}),
    },
    # The five autonomous workcells are coordinated by the gateway/portfolio
    # controller; they do not originate cross-workcell calls today. No grants
    # -> deny-by-default if one ever does without the matrix being updated.
    # The Campaign Orchestrator fans campaign nodes out to seo/prospecting/
    # outreach/scaffold/proposal/crm etc. — but ALWAYS through the gateway's
    # /dispatch/{target} seam (which re-signs under the gateway identity), so
    # its only mesh privilege is "dispatch" on the gateway. A compromised
    # campaigns workcell therefore has NO direct grant to any peer workcell.
    "campaigns": {
        "gateway": frozenset({"dispatch"}),
    },
}


def check_capability(service: str, capability: str) -> None:
    """Raise HTTPException(403) if `capability` is not in `service`'s allowed set.

    BACKWARD-COMPATIBLE. This is the original signature and the original
    behaviour: it validates that the *callee* workcell exposes the named
    capability. ~150 call sites across backend/ pass hard-coded literals to
    it, so this contract is frozen.

    The R-1 caller→callee authorization is layered in via ``authorize()`` and
    the request-aware ``check_capability_for`` below; this two-arg form does
    NOT and CANNOT see the caller, so it remains a pure static check. Callers
    that want real authorization use ``check_capability_for(request, ...)``.
    """
    allowed = SERVICE_CAPABILITIES.get(service, set())
    if capability not in allowed:
        raise HTTPException(status_code=403, detail=f"capability denied: {capability}")


# ---------------------------------------------------------------------------
# Authz mode (the live-stack safety valve).
# ---------------------------------------------------------------------------

_VALID_MODES: Final[frozenset[str]] = frozenset({"off", "audit", "enforce"})


def authz_mode() -> str:
    """Resolve the caller-authorization enforcement mode.

    ``SAMUS_AUTHZ_MODE`` env var, one of:

    - ``off`` — the caller-authz matrix is NOT evaluated. Behaviour
      is exactly the pre-R-1 behaviour: the static capability-set check only.
    - ``audit`` — the matrix IS evaluated; on a would-be denial a structured
      WARNING is logged (caller, callee, capability, path) but the request is
      ALLOWED. This lets the operator validate the matrix against real
      traffic before enforcing — the WDAC audit-mode philosophy.
    - ``enforce`` (DEFAULT — armed HOTL Tranche 1, operator decision
      2026-07-05) — the matrix is evaluated and a denial raises HTTP 403.

    An unrecognised value falls back to ``enforce`` (the armed default) and
    logs a warning; an operator stands the gate down only by an explicit
    ``off``/``audit``.

    Read live from ``os.environ`` rather than cached settings so an operator
    can flip the mode and have the next process / reload pick it up.
    """
    raw = (os.environ.get("SAMUS_AUTHZ_MODE", "") or "").strip().lower()
    if not raw:
        return "enforce"
    if raw not in _VALID_MODES:
        _LOG.warning(
            "samus.authz.bad_mode: SAMUS_AUTHZ_MODE=%r is not one of "
            "off|audit|enforce — falling back to 'enforce'",
            raw,
        )
        return "enforce"
    return raw


def is_authorized(caller: str, callee: str, capability: str) -> bool:
    """Pure predicate: is ``caller`` granted ``capability`` on ``callee``?

    Does NOT consult the mode and does NOT raise — it is the matrix lookup
    only. ``authorize()`` wraps this with the mode + logging + raise logic.

    Rules:
    - ``external`` callers are always authorized HERE — exempt paths carry
      their own auth and are never matrix-gated (the caller of this function
      should normally not even reach it for an external request, but the
      explicit allow keeps the predicate total).
    - ``gateway`` (and any caller whose grant table contains ``"*"``) is
      granted everything.
    - Otherwise the caller must have an entry for ``callee`` whose grant set
      contains either ``"*"`` or the exact ``capability``.
    - Everything else is denied (deny-by-default).
    """
    if caller == EXTERNAL_CALLER:
        return True
    grants = CALLER_GRANTS.get(caller)
    if not grants:
        return False
    # A caller-level "*" callee key (gateway/fulfillment) grants the whole mesh.
    callee_grant = grants.get("*")
    if callee_grant and "*" in callee_grant:
        return True
    callee_grant = grants.get(callee)
    if not callee_grant:
        return False
    return "*" in callee_grant or capability in callee_grant


def caller_reaches_callee(caller: str, callee: str) -> bool:
    """Coarse caller→callee predicate: has ``caller`` ANY grant on ``callee``?

    This is the workcell-granularity gate the HMAC middleware can apply
    without knowing which capability a path maps to. It answers "is this
    caller allowed to talk to this workcell at all" — a `True` here still
    leaves the per-capability ``authorize()`` / ``check_capability_for`` check
    to the route handler for fine-grained control.

    Rules mirror :func:`is_authorized`:
    - ``external`` always reaches (exempt paths carry their own auth).
    - a caller with a ``"*"`` callee entry (gateway/fulfillment) reaches all.
    - otherwise the caller must have a non-empty grant set for ``callee``.
    - deny-by-default for everything else, including the ``unknown`` caller.
    """
    if caller == EXTERNAL_CALLER:
        return True
    grants = CALLER_GRANTS.get(caller)
    if not grants:
        return False
    wildcard = grants.get("*")
    if wildcard and "*" in wildcard:
        return True
    callee_grant = grants.get(callee)
    return bool(callee_grant)


def authorize_caller_to_callee(
    caller: str | None,
    callee: str,
    *,
    path: str = "",
    mode: str | None = None,
) -> bool:
    """Coarse caller→callee boundary check used by VerifyHMACMiddleware.

    Evaluates :func:`caller_reaches_callee` under the current authz mode.

    Returns ``True`` when the request should be allowed to proceed, ``False``
    only in ``enforce`` mode on a denial (the middleware turns that into a
    403 response). In ``off`` mode it always returns ``True`` without
    evaluating; in ``audit`` mode it logs a structured warning on a would-be
    denial but still returns ``True``.

    The callee axis is the workcell that OWNS this middleware instance
    (``settings.service_name``). The caller axis is the verified
    ``X-Samus-Caller`` identity. This is the boundary that subsumes the
    cross-workcell IDOR (finding M2): once enforced, a workcell with no grant
    to CRM cannot reach ANY CRM route, object-id or not.
    """
    resolved_mode = mode if mode is not None else authz_mode()
    if resolved_mode == "off":
        return True

    caller_id = (caller or "").strip() or UNKNOWN_CALLER
    if caller_id == EXTERNAL_CALLER:
        return True

    if caller_reaches_callee(caller_id, callee):
        return True

    if resolved_mode == "audit":
        _LOG.warning(
            "samus.authz.would_deny mode=audit scope=callee caller=%s "
            "callee=%s path=%s — ALLOWED (audit mode)",
            caller_id,
            callee,
            path or "-",
        )
        return True

    # enforce
    _LOG.warning(
        "samus.authz.denied mode=enforce scope=callee caller=%s callee=%s path=%s",
        caller_id,
        callee,
        path or "-",
    )
    return False


def authorize(
    caller: str | None,
    callee: str,
    capability: str,
    *,
    path: str = "",
    mode: str | None = None,
) -> None:
    """Evaluate the caller→callee grant matrix under the current authz mode.

    - ``off``    -> returns immediately (no evaluation, no log). Caller-authz
                    is dormant; the static ``check_capability`` is the only
                    gate, exactly as pre-R-1.
    - ``audit``  -> evaluates the matrix; on a would-be denial logs a
                    structured WARNING and RETURNS (request allowed).
    - ``enforce``-> evaluates the matrix; on a denial raises HTTPException 403.

    ``caller`` is the VERIFIED caller identity (``request.state.caller_service``
    set by VerifyHMACMiddleware). ``None`` or empty is treated as the
    ``unknown`` sentinel and is denied-by-default in audit/enforce.

    A grant (would-be-allow) is silently permitted in all modes — the matrix
    only ever produces a log line / 403 on a *deny*.
    """
    resolved_mode = mode if mode is not None else authz_mode()
    if resolved_mode == "off":
        return

    caller_id = (caller or "").strip() or UNKNOWN_CALLER

    # External callers reach handlers with their own signature scheme; the
    # matrix never gates them.
    if caller_id == EXTERNAL_CALLER:
        return

    if is_authorized(caller_id, callee, capability):
        return

    # --- would-be denial ---------------------------------------------------
    if resolved_mode == "audit":
        _LOG.warning(
            "samus.authz.would_deny mode=audit caller=%s callee=%s "
            "capability=%s path=%s — ALLOWED (audit mode)",
            caller_id,
            callee,
            capability,
            path or "-",
        )
        return

    # enforce
    _LOG.warning(
        "samus.authz.denied mode=enforce caller=%s callee=%s capability=%s path=%s",
        caller_id,
        callee,
        capability,
        path or "-",
    )
    raise HTTPException(
        status_code=403,
        detail=f"authorization denied: {caller_id} -> {callee}:{capability}",
    )


def resolve_caller(request: Request | None) -> str:
    """Extract the verified caller identity from a request.

    VerifyHMACMiddleware attaches ``request.state.caller_service`` after a
    successful signature check. Exempt/unsigned paths get ``external``.
    A request that somehow reached a handler without the attribute (e.g. a
    test that builds a bare Request) resolves to ``unknown``.
    """
    if request is None:
        return UNKNOWN_CALLER
    state = getattr(request, "state", None)
    caller = getattr(state, "caller_service", None) if state is not None else None
    if not caller:
        return UNKNOWN_CALLER
    return str(caller)


def check_capability_for(
    request: Request | None,
    callee: str,
    capability: str,
) -> None:
    """Request-aware capability + caller-authorization check.

    Combines BOTH layers in one call for route handlers that have the
    ``Request`` in scope:

    1. The static ``check_capability(callee, capability)`` — unchanged,
       always runs, raises 403 if the callee does not expose the capability.
    2. The R-1 caller→callee matrix via ``authorize()`` — runs only in
       ``audit`` / ``enforce`` mode (a no-op in the default ``off`` mode).

    Route files are NOT required to migrate to this function — the existing
    two-arg ``check_capability`` keeps working untouched. This is provided so
    new/updated handlers can opt into real authorization with one call. The
    workcell route files are intentionally left alone in this change set (see
    the R-1 remediation notes): the matrix is enforced at the boundary the
    moment the operator sets ``SAMUS_AUTHZ_MODE``, and the per-route wiring
    can follow incrementally without a flag-day edit across 19 apps.
    """
    check_capability(callee, capability)
    caller = resolve_caller(request)
    path = ""
    if request is not None:
        try:
            path = request.url.path
        except Exception:  # noqa: BLE001 - never let path extraction break authz
            path = ""
    authorize(caller, callee, capability, path=path)
