"""Outreach workcell FastAPI service."""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from fastapi import Depends, HTTPException, Request

from backend.common.app_factory import create_base_app
from backend.common.capabilities import check_capability
from backend.common.models import TaskEnvelope
from backend.common.rate_limit import rate_limit_dependency

from . import closer
from . import objection as objection_module
from . import social_adapter
from .models import (
    OutreachAdvanceRequest,
    OutreachLogRequest,
    OutreachMessageRequest,
)
from .service import advance_call, get_metrics, log_outcome, send_message
from .social_adapter import SocialPost, compose_post_via_llm, send_post

_LOG = logging.getLogger("samus.outreach.app")


def _parse(model_cls, payload: Any):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail=f"expected_{model_cls.__name__}_object")
    try:
        return model_cls.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid_{model_cls.__name__}: {exc}")


_EMAIL_RE = None  # compiled lazily in _valid_unsub_email


def _valid_unsub_email(raw: str) -> str | None:
    """Light validation for the public opt-out param: bounded length, one @,
    plausible domain. Returns the normalised (lowercased, stripped) address
    or None. Deliberately permissive — rejecting a weird-but-real address on
    an OPT-OUT path is worse than suppressing a junk string."""
    global _EMAIL_RE
    if _EMAIL_RE is None:
        import re

        _EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[A-Za-z]{2,}$")
    email = (raw or "").strip().lower()
    if not email or len(email) > 320 or not _EMAIL_RE.match(email):
        return None
    return email


def _record_opt_out(email: str) -> bool:
    """Append to the shared suppression file + the opt-out ledger. Idempotent
    on the suppression file. Returns False only on a write fault."""
    try:
        import json as _json
        import os as _os

        from backend.common.dates import iso_now
        from .morning_batch import _artifact_root

        root = _os.path.join(_artifact_root(), "outreach")
        _os.makedirs(root, exist_ok=True)
        supp_path = _os.path.join(root, "emailed_emails.txt")
        existing: set[str] = set()
        try:
            with open(supp_path, "r", encoding="utf-8") as fh:
                existing = {line.strip().lower() for line in fh if line.strip()}
        except FileNotFoundError:
            pass
        if email not in existing:
            with open(supp_path, "a", encoding="utf-8") as fh:
                fh.write(email + "\n")
        with open(_os.path.join(root, "opt_outs.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(_json.dumps({"ts": iso_now(), "email": email,
                                  "source": "unsubscribe_page"}) + "\n")
        return True
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("opt-out record failed for %s: %s", email, exc)
        return False


def create_app():
    # /unsubscribe is the public CAN-SPAM opt-out surface — reachable through
    # the ingress WITHOUT HMAC (an email recipient cannot sign requests).
    # It is rate-limited and does nothing but suppress an address.
    app = create_base_app(
        service_name="outreach",
        # /docuseal/webhook is the DocuSeal completion callback: a DocuSeal
        # delivery cannot sign an inter-service HMAC, so it is carved out of
        # VerifyHMACMiddleware and authenticated by the shared-secret header
        # instead (see the route below).
        hmac_exempt_paths=("/unsubscribe", "/docuseal/webhook"),
    )

    @app.get(
        "/unsubscribe",
        dependencies=[Depends(rate_limit_dependency("outreach_unsubscribe"))],
    )
    async def unsubscribe(e: str = "") -> Any:
        """One-click opt-out (the URL every outbound footer carries).

        Built 2026-07-03: SAMUS_UNSUBSCRIBE_URL had pointed at a route no
        service ever served — every previously sent footer linked to a 404.
        Suppression is the SAME file morning_batch/run_campaign dedup
        against, so an opt-out takes effect on the very next batch.
        """
        from fastapi.responses import HTMLResponse

        email = _valid_unsub_email(e)
        if email is None:
            return HTMLResponse(
                "<html><body style='font-family:sans-serif;max-width:32em;margin:4em auto'>"
                "<h2>Unsubscribe</h2><p>That link is missing or has an invalid "
                "email address. Reply to any of our emails with the word "
                "<b>unsubscribe</b> and we'll remove you promptly.</p></body></html>",
                status_code=400,
            )
        ok = _record_opt_out(email)
        if not ok:
            return HTMLResponse(
                "<html><body style='font-family:sans-serif;max-width:32em;margin:4em auto'>"
                "<h2>Unsubscribe</h2><p>Something went wrong on our end. Reply to "
                "any of our emails with the word <b>unsubscribe</b> and we'll "
                "remove you promptly.</p></body></html>",
                status_code=500,
            )
        _LOG.info("opt-out recorded via unsubscribe page")
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;max-width:32em;margin:4em auto'>"
            "<h2>You're unsubscribed</h2><p>We've removed <b>" + email + "</b> "
            "from our outreach list. You won't hear from us again.</p></body></html>"
        )

    @app.post("/docuseal/webhook")
    async def docuseal_webhook(request: Request) -> dict[str, Any]:
        """Inbound DocuSeal completion webhook (internal docker network).

        VerifyHMACMiddleware is carved out (a DocuSeal delivery cannot sign an
        inter-service HMAC); auth is the shared-secret ``X-Docuseal-Secret``
        header, verified fail-closed — the same posture as the Stripe/Vapi
        webhooks. A completion emits a ``contract.signed`` business event.
        """
        # Auth is the shared-secret header verified below (fail-closed), plus
        # the route is carved out of VerifyHMACMiddleware. No dedicated
        # capability entry: capabilities.py is a manifested (immutable-baseline)
        # file, so a new cap there would require a signed baseline reseed.
        from backend.common.config import get_settings
        from backend.contracts.models import DocuSealWebhookEvent
        from backend.contracts.service import handle_webhook_event
        from backend.contracts.signature import (
            WEBHOOK_SECRET_HEADER,
            DocuSealSignatureError,
            verify_docuseal_webhook,
        )

        secret = (get_settings().docuseal_webhook_secret or "").strip()
        try:
            verify_docuseal_webhook(
                request.headers.get(WEBHOOK_SECRET_HEADER),
                secret,
                query_secret=request.query_params.get("secret"),
            )
        except DocuSealSignatureError as exc:
            _LOG.warning("docuseal webhook rejected: %s", exc)
            raise HTTPException(
                status_code=403, detail=f"docuseal_webhook_unauthorized: {exc}"
            ) from exc
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="body must be a JSON object")
        event = DocuSealWebhookEvent.from_wire(body)
        result = handle_webhook_event(event)
        if event.is_completed() and event.slug:
            from backend.campaigns.contract_wire import trigger_campaign_on_signing
            result["campaign_wire"] = trigger_campaign_on_signing(
                slug=event.slug,
                submission_id=event.submission_id,
                email=event.email,
            )
        return result

    # M7 — /work dispatches the outbound-action verbs (send_message,
    # send_social_post, compose_and_send_social_post) and the LLM-backed
    # closer / objection steps. /advance runs the closer-step LLM path
    # directly. Both get a per-caller rate limit so a runaway loop / a
    # compromised caller cannot fire unbounded sends or LLM calls. Env-tunable
    # via SAMUS_RATE_LIMIT_OUTREACH_WORK_PER_MINUTE / ..._OUTREACH_ADVANCE_...
    @app.post(
        "/work",
        dependencies=[Depends(rate_limit_dependency("outreach_work"))],
    )
    async def work(request: Request) -> dict[str, Any]:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="expected_json_object")
        try:
            envelope = TaskEnvelope.model_validate(body)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid_envelope: {exc}")
        action = (envelope.metadata or {}).get("action") or "advance_call"
        if action == "advance_call":
            check_capability("outreach", "advance_call")
            return advance_call(_parse(OutreachAdvanceRequest, envelope.payload)).model_dump()
        if action == "log_outcome":
            check_capability("outreach", "log_outcome")
            return log_outcome(_parse(OutreachLogRequest, envelope.payload)).model_dump()
        if action == "send_message":
            check_capability("outreach", "send_message")
            return send_message(_parse(OutreachMessageRequest, envelope.payload))
        if action == "send_contract":
            # Reuses the registered "send_message" capability (this action sends
            # a contract email through the same seam). A dedicated "send_contract"
            # cap would require a signed immutable-baseline update to
            # capabilities.py, so it is intentionally not added here.
            check_capability("outreach", "send_message")
            from .contract_step import send_service_agreement_email

            p = envelope.payload if isinstance(envelope.payload, dict) else {}
            return send_service_agreement_email(
                prospect_id=str(p.get("prospect_id") or ""),
                email=str(p.get("email") or ""),
                name=str(p.get("name") or ""),
                company=str(p.get("company") or ""),
                scope=str(p.get("scope") or ""),
                price_usd=str(p.get("price_usd") or ""),
                term=str(p.get("term") or ""),
                notes=str(p.get("notes") or ""),
                opportunity_id=str(p.get("opportunity_id") or ""),
                campaign_id=str(p.get("campaign_id") or ""),
                variant_arm_id=str(p.get("variant_arm_id") or ""),
                subject=(str(p.get("subject")) if p.get("subject") else None),
            )
        if action == "send_proposal":
            # Per-proposal PDF path: turn a client's proposal PDF into a signing
            # request + email the link. Reuses the "send_message" capability
            # (see send_contract above for why no dedicated cap).
            check_capability("outreach", "send_message")
            from .contract_step import send_proposal_agreement_email

            p = envelope.payload if isinstance(envelope.payload, dict) else {}
            return send_proposal_agreement_email(
                prospect_id=str(p.get("prospect_id") or ""),
                email=str(p.get("email") or ""),
                name=str(p.get("name") or ""),
                company=str(p.get("company") or ""),
                pdf_base64=str(p.get("pdf_base64") or ""),
                pdf_url=str(p.get("pdf_url") or ""),
                pdf_path=str(p.get("pdf_path") or ""),
                document_name=str(p.get("document_name") or "Service Agreement"),
                fields=p.get("fields") if isinstance(p.get("fields"), list) else None,
                values=p.get("values") if isinstance(p.get("values"), dict) else None,
                opportunity_id=str(p.get("opportunity_id") or ""),
                campaign_id=str(p.get("campaign_id") or ""),
                variant_arm_id=str(p.get("variant_arm_id") or ""),
                subject=(str(p.get("subject")) if p.get("subject") else None),
            )
        if action == "handle_objection":
            check_capability("outreach", "handle_objection")
            payload = envelope.payload or {}
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="expected_handle_objection_object")
            transcript_text = payload.get("transcript_text")
            if not isinstance(transcript_text, str):
                raise HTTPException(
                    status_code=422,
                    detail="handle_objection requires transcript_text (str)",
                )
            intel = payload.get("intel")
            if intel is not None and not isinstance(intel, dict):
                raise HTTPException(
                    status_code=422,
                    detail="handle_objection intel must be a dict or omitted",
                )
            _LOG.info("handle_objection transcript_len=%d has_intel=%s",
                      len(transcript_text), intel is not None)
            return objection_module.handle_objection(transcript_text, intel)
        if action == "advance_call_state":
            check_capability("outreach", "advance_call_state")
            payload = envelope.payload or {}
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="advance_call_state_payload_must_be_object")
            state = payload.get("state")
            if not isinstance(state, str):
                raise HTTPException(status_code=422, detail="advance_call_state_requires_state_string")
            user_input = payload.get("user_input")
            if not isinstance(user_input, str):
                raise HTTPException(status_code=422, detail="advance_call_state_requires_user_input_string")
            intel = payload.get("intel")
            if not isinstance(intel, dict) or "products" not in intel:
                raise HTTPException(status_code=422, detail="advance_call_state_requires_intel_with_products_key")
            objection_result: dict | None = payload.get("objection_result")
            _LOG.info(
                "advance_call_state state=%s user_input_len=%d has_objection_result=%s",
                state,
                len(user_input),
                objection_result is not None,
            )
            return closer.run_closer_step(state, user_input, intel, objection_result)
        if action == "send_social_post":
            check_capability("outreach", "send_social_post")
            payload = envelope.payload or {}
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="expected_send_social_post_object")
            platform = payload.get("platform")
            if platform not in ("linkedin", "facebook"):
                raise HTTPException(
                    status_code=422,
                    detail="send_social_post requires platform in {linkedin, facebook}",
                )
            body_text = payload.get("body")
            if not isinstance(body_text, str) or not body_text.strip():
                raise HTTPException(
                    status_code=422,
                    detail="send_social_post requires non-empty body (str)",
                )
            post = SocialPost(
                platform=platform,
                body=body_text,
                link=str(payload.get("link") or ""),
                image_url=str(payload.get("image_url") or ""),
                scheduled_at=str(payload.get("scheduled_at") or ""),
                tags=list(payload.get("tags") or []),
                stake_sentence=str(payload.get("stake_sentence") or ""),
            )
            result = send_post(post)
            _LOG.info(
                "send_social_post platform=%s sent=%s dry_run=%s post_id=%s",
                platform, result.sent, result.dry_run, result.post_id,
            )
            return asdict(result)
        if action == "compose_and_send_social_post":
            check_capability("outreach", "compose_and_send_social_post")
            payload = envelope.payload or {}
            if not isinstance(payload, dict):
                raise HTTPException(
                    status_code=400, detail="expected_compose_and_send_social_post_object"
                )
            platform = payload.get("platform")
            if platform not in ("linkedin", "facebook"):
                raise HTTPException(
                    status_code=422,
                    detail="compose_and_send_social_post requires platform in {linkedin, facebook}",
                )
            intel = payload.get("intel")
            if not isinstance(intel, dict):
                raise HTTPException(
                    status_code=422,
                    detail="compose_and_send_social_post requires intel (dict)",
                )
            post = compose_post_via_llm(
                intel,
                platform,
                stake_sentence=str(payload.get("stake_sentence") or ""),
            )
            result = send_post(post)
            _LOG.info(
                "compose_and_send_social_post platform=%s sent=%s dry_run=%s",
                platform, result.sent, result.dry_run,
            )
            return {**asdict(result), "composed_body": post.body}
        # --- Growth-enrichment Phase D/E (dormant: DRY-RUN / plan-only) -------
        if action == "repurpose_blog_post":
            check_capability("outreach", "repurpose_blog_post")
            from backend.social.dispatch import handle_repurpose

            payload = envelope.payload if isinstance(envelope.payload, dict) else {}
            return handle_repurpose(payload)
        if action == "plan_social_calendar":
            check_capability("outreach", "plan_social_calendar")
            from backend.social.dispatch import handle_plan

            payload = envelope.payload if isinstance(envelope.payload, dict) else {}
            return handle_plan(payload)
        if action == "dispatch_social_calendar":
            check_capability("outreach", "dispatch_social_calendar")
            from backend.social.dispatch import handle_dispatch

            payload = envelope.payload if isinstance(envelope.payload, dict) else {}
            return handle_dispatch(payload)
        if action == "plan_nurture":
            check_capability("outreach", "plan_nurture")
            from backend.outreach.sequences import handle_plan_nurture

            payload = envelope.payload if isinstance(envelope.payload, dict) else {}
            return handle_plan_nurture(payload)
        raise HTTPException(status_code=400, detail=f"unknown_action: {action}")

    @app.post(
        "/advance",
        dependencies=[Depends(rate_limit_dependency("outreach_advance"))],
    )
    async def advance(request: Request) -> dict[str, Any]:
        check_capability("outreach", "advance_call")
        body = await request.json()
        return advance_call(_parse(OutreachAdvanceRequest, body)).model_dump()

    @app.post("/outcome")
    async def outcome(request: Request) -> dict[str, Any]:
        check_capability("outreach", "log_outcome")
        body = await request.json()
        return log_outcome(_parse(OutreachLogRequest, body)).model_dump()

    @app.get("/metrics_snapshot")
    async def metrics_snapshot() -> dict[str, Any]:
        """Standalone read-only observability endpoint — aggregate outreach
        metrics (top objections, best products by close rate, angle
        win-rates) for an operator or monitoring poll.

        Deliberately not consumed programmatically: callers of ``log_outcome``
        already receive a fresh ``metrics_snapshot`` embedded in the
        ``OutreachOutcome`` response, so the write path needs nothing from
        here. This GET exists for the same operator-driven observability use
        as ``finance``'s ``/snapshot`` and ``crm``'s ``/feedback/snapshot``.

        Capability note: outreach has no dedicated read capability in the
        static registry, so this reuses ``log_outcome`` — the capability that
        owns the metrics surface. The check still gates the endpoint; it is
        not an authorization gap.
        """
        check_capability("outreach", "log_outcome")
        return get_metrics().model_dump()

    return app


app = create_app()
