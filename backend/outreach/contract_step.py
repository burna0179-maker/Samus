"""Campaign contract step — generate a service-agreement signing link and send it.

The reusable seam a campaign invokes to close with a signed contract: it calls
``contracts.generate_service_agreement`` for the prospect, then delivers the
signing link through the normal outreach email path (``send_message``), so the
send is subject to the same harm-gate + daily-cap + attribution as any campaign
message.

Fail-soft end to end: if DocuSeal is down/misconfigured no email is sent and the
caller gets ``{"ok": False, "error": ...}`` — a campaign step that cannot
produce a link never sends a broken one.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.contracts import (
    ContractParty,
    ProposalAgreementRequest,
    ServiceAgreementRequest,
)
from backend.contracts.service import (
    generate_proposal_agreement,
    generate_service_agreement,
    pdf_base64_from_path,
)

from .models import OutreachMessageRequest
from .service import send_message

_LOG = logging.getLogger("samus.outreach.contract_step")

_DEFAULT_SUBJECT = "Your Hustleforge service agreement"
_TEMPLATE_ID = "service_agreement_v1"


def _email_html(company: str, name: str, signing_url: str) -> str:
    greeting = f"Hi {name}," if name else "Hi,"
    org = f" for {company}" if company else ""
    return (
        f"<p>{greeting}</p>"
        f"<p>Your service agreement{org} is ready to sign — about a minute, no "
        f"account needed.</p>"
        f'<p><a href="{signing_url}" style="display:inline-block;padding:12px 20px;'
        f"background:#7c6cff;color:#fff;border-radius:8px;text-decoration:none;"
        f'font-weight:600">Review &amp; sign</a></p>'
        f'<p>Or open this link: <a href="{signing_url}">{signing_url}</a></p>'
        f"<p>&mdash; Hustleforge</p>"
    )


def _email_text(company: str, name: str, signing_url: str) -> str:
    greeting = f"Hi {name}," if name else "Hi,"
    org = f" for {company}" if company else ""
    return (
        f"{greeting}\n\nYour service agreement{org} is ready to sign "
        f"(about a minute, no account needed):\n{signing_url}\n\n- Hustleforge"
    )


def send_service_agreement_email(
    *,
    prospect_id: str,
    email: str,
    name: str = "",
    company: str = "",
    scope: str = "",
    price_usd: str = "",
    term: str = "",
    notes: str = "",
    opportunity_id: str = "",
    campaign_id: str = "",
    variant_arm_id: str = "",
    subject: str | None = None,
) -> dict[str, Any]:
    """Campaign step: generate the signing link and email it to the prospect."""
    if not (email or "").strip():
        return {"ok": False, "error": "email_required", "prospect_id": prospect_id}

    contract = generate_service_agreement(
        ServiceAgreementRequest(
            prospect_id=prospect_id,
            party=ContractParty(email=email.strip(), name=name),
            company=company,
            scope=scope,
            price_usd=price_usd,
            term=term,
            notes=notes,
            opportunity_id=opportunity_id,
            send_email=False,  # delivered via the outreach path below
        )
    )
    if not contract.ok or not contract.signing_url:
        _LOG.warning("contract step: no link for %s (%s)", prospect_id, contract.error)
        return {
            "ok": False,
            "error": contract.error or "no_signing_url",
            "prospect_id": prospect_id,
        }

    try:
        send_result = send_message(
            OutreachMessageRequest(
                prospect_id=prospect_id,
                channel="email",
                template_id=_TEMPLATE_ID,
                to=email.strip(),
                subject=subject or _DEFAULT_SUBJECT,
                body=_email_text(company, name, contract.signing_url),
                html_body=_email_html(company, name, contract.signing_url),
                company=company,
                campaign_id=campaign_id,
                variant_arm_id=variant_arm_id,
            )
        )
    except Exception as exc:  # noqa: BLE001 — harm-gate/cap raise: link made, send blocked
        _LOG.warning("contract step: send blocked for %s: %s", prospect_id, exc)
        return {
            "ok": False,
            "error": f"send_blocked: {exc}",
            "prospect_id": prospect_id,
            "signing_url": contract.signing_url,
            "submission_id": contract.submission_id,
        }

    return {
        "ok": True,
        "prospect_id": prospect_id,
        "signing_url": contract.signing_url,
        "submission_id": contract.submission_id,
        "message_id": send_result.get("message_id") if isinstance(send_result, dict) else None,
    }


def send_proposal_agreement_email(
    *,
    prospect_id: str,
    email: str,
    name: str = "",
    company: str = "",
    pdf_base64: str = "",
    pdf_url: str = "",
    pdf_path: str = "",
    document_name: str = "Service Agreement",
    fields: list | None = None,
    values: dict | None = None,
    opportunity_id: str = "",
    campaign_id: str = "",
    variant_arm_id: str = "",
    subject: str | None = None,
) -> dict[str, Any]:
    """Campaign step (PDF path): turn a per-client proposal PDF into a signing
    request and email the link. ``pdf_path`` is read + base64-encoded here;
    otherwise pass ``pdf_base64`` or a ``pdf_url``."""
    if not (email or "").strip():
        return {"ok": False, "error": "email_required", "prospect_id": prospect_id}
    if pdf_path and not pdf_base64:
        try:
            pdf_base64 = pdf_base64_from_path(pdf_path)
        except OSError as exc:
            return {"ok": False, "error": f"pdf_read_failed: {exc}", "prospect_id": prospect_id}

    contract = generate_proposal_agreement(
        ProposalAgreementRequest(
            prospect_id=prospect_id,
            party=ContractParty(email=email.strip(), name=name),
            company=company,
            document_name=document_name,
            pdf_base64=pdf_base64,
            pdf_url=pdf_url,
            fields=fields or [],
            values=values or {},
            opportunity_id=opportunity_id,
            send_email=False,
        )
    )
    if not contract.ok or not contract.signing_url:
        _LOG.warning("proposal step: no link for %s (%s)", prospect_id, contract.error)
        return {
            "ok": False,
            "error": contract.error or "no_signing_url",
            "prospect_id": prospect_id,
        }

    try:
        send_result = send_message(
            OutreachMessageRequest(
                prospect_id=prospect_id,
                channel="email",
                template_id=_TEMPLATE_ID,
                to=email.strip(),
                subject=subject or _DEFAULT_SUBJECT,
                body=_email_text(company, name, contract.signing_url),
                html_body=_email_html(company, name, contract.signing_url),
                company=company,
                campaign_id=campaign_id,
                variant_arm_id=variant_arm_id,
            )
        )
    except Exception as exc:  # noqa: BLE001 — harm-gate/cap raise: link made, send blocked
        _LOG.warning("proposal step: send blocked for %s: %s", prospect_id, exc)
        return {
            "ok": False,
            "error": f"send_blocked: {exc}",
            "prospect_id": prospect_id,
            "signing_url": contract.signing_url,
            "submission_id": contract.submission_id,
        }

    return {
        "ok": True,
        "prospect_id": prospect_id,
        "signing_url": contract.signing_url,
        "submission_id": contract.submission_id,
        "message_id": send_result.get("message_id") if isinstance(send_result, dict) else None,
    }
