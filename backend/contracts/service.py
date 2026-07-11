"""Contract-signing service — generate agreements, handle completion webhooks.

Two generate paths share one finalize (journal + ``contract.sent`` event):
  * ``generate_service_agreement`` — from the stored template (simple/standard).
  * ``generate_proposal_agreement`` — one-off from a per-client proposal PDF
    (the primary path: each proposal keeps its own design + pricing).

``handle_webhook_event`` turns a ``submission.completed`` webhook into a
``contract.signed`` business event keyed to the opportunity/prospect.

Fail-soft: a DocuSeal problem yields ``ContractResult(ok=False, error=...)`` so
the caller skips the send. Durability: every generate + completion is journaled
under ``<artifact_root>/contracts/`` first.
"""
from __future__ import annotations

import base64
import json
import logging
from datetime import date
from pathlib import Path
from typing import Any, Optional

from backend.common import storage
from backend.common.business_events import (
    CONTRACT_SENT,
    CONTRACT_SIGNED,
    emit_business_event,
)
from backend.common.config import get_settings
from backend.common.dates import iso_now

from .client import DocuSealClient, DocuSealError
from .models import (
    ContractResult,
    DocuSealWebhookEvent,
    ProposalAgreementRequest,
    ServiceAgreementRequest,
)

_LOG = logging.getLogger("samus.contracts.service")


def _client() -> DocuSealClient:
    s = get_settings()
    return DocuSealClient(
        api_base=s.docuseal_api_base,
        api_token=s.docuseal_api_token,
        public_base=s.docuseal_public_base,
        timeout_sec=float(s.docuseal_http_timeout_sec),
    )


def _journal(kind: str, record: dict[str, Any]) -> bool:
    try:
        path = storage.root() / "contracts" / f"{kind}_{date.today().isoformat()}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        return True
    except OSError as exc:  # journal failure must not block the flow
        _LOG.warning("contracts journal append failed (%s): %s", kind, exc)
        return False


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def pdf_base64_from_path(path: str | Path) -> str:
    """Read a PDF file and return its base64 (for ProposalAgreementRequest)."""
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


def _finalize(
    client: DocuSealClient,
    submitters: list[dict[str, Any]],
    *,
    prospect_id: str,
    opportunity_id: str,
    company: str,
    email: str,
) -> ContractResult:
    """Shared tail: pick the signer, build the link, journal, emit contract.sent."""
    if not submitters:
        return ContractResult(ok=False, prospect_id=prospect_id, error="docuseal_no_submitter")

    first = submitters[0]
    slug = str(first.get("slug") or "")
    signing_url = client.signing_url_for(slug, str(first.get("embed_src") or ""))
    submission_id = _as_int(first.get("submission_id"))
    submitter_id = _as_int(first.get("id"))

    result = ContractResult(
        ok=bool(signing_url),
        prospect_id=prospect_id,
        signing_url=signing_url,
        submission_id=submission_id,
        submitter_id=submitter_id,
        slug=slug,
        status=str(first.get("status") or "sent"),
        error="" if signing_url else "no_signing_url",
    )

    _journal(
        "contract_sent",
        {
            "ts": iso_now(),
            "prospect_id": prospect_id,
            "opportunity_id": opportunity_id,
            "company": company,
            "email": email,
            "submission_id": submission_id,
            "submitter_id": submitter_id,
            "slug": slug,
            "signing_url": signing_url,
            "status": result.status,
        },
    )

    if result.ok:
        emit_business_event(
            CONTRACT_SENT,
            workcell="contracts",
            prospect_id=prospect_id or None,
            opportunity_id=opportunity_id or None,
            metadata={"submission_id": submission_id, "slug": slug},
        )
    return result


def _template_id() -> Optional[int]:
    return _as_int((get_settings().docuseal_service_agreement_template_id or "").strip() or None)


def generate_service_agreement(req: ServiceAgreementRequest) -> ContractResult:
    """Create one signing request from the stored service-agreement template."""
    if not get_settings().docuseal_enabled:
        return ContractResult(ok=False, prospect_id=req.prospect_id, error="docuseal_disabled")
    template_id = _template_id()
    if not template_id:
        return ContractResult(
            ok=False, prospect_id=req.prospect_id, error="template_id_unset_or_invalid"
        )
    client = _client()
    try:
        submitters = client.create_submission(
            template_id=template_id,
            submitter=req.party,
            values=req.template_values(),
            send_email=req.send_email,
            external_id=req.external_id(),
        )
    except DocuSealError as exc:
        _LOG.warning("generate_service_agreement failed for %s: %s", req.prospect_id, exc)
        return ContractResult(ok=False, prospect_id=req.prospect_id, error=str(exc))
    return _finalize(
        client, submitters,
        prospect_id=req.prospect_id, opportunity_id=req.opportunity_id,
        company=req.company, email=req.party.email,
    )


def generate_proposal_agreement(req: ProposalAgreementRequest) -> ContractResult:
    """Create one signing request from a per-client proposal PDF (one-off)."""
    if not get_settings().docuseal_enabled:
        return ContractResult(ok=False, prospect_id=req.prospect_id, error="docuseal_disabled")
    if not req.has_document():
        return ContractResult(ok=False, prospect_id=req.prospect_id, error="pdf_source_required")
    client = _client()
    try:
        submitters = client.create_submission_from_pdf(
            document_name=req.document_name,
            submitter=req.party,
            fields=req.field_wire() or None,
            pdf_base64=req.pdf_base64,
            pdf_url=req.pdf_url,
            values=req.values or None,
            send_email=req.send_email,
            external_id=req.external_id(),
        )
    except DocuSealError as exc:
        _LOG.warning("generate_proposal_agreement failed for %s: %s", req.prospect_id, exc)
        return ContractResult(ok=False, prospect_id=req.prospect_id, error=str(exc))
    return _finalize(
        client, submitters,
        prospect_id=req.prospect_id, opportunity_id=req.opportunity_id,
        company=req.company, email=req.party.email,
    )


def _route_external_id(external_id: str) -> tuple[str, str]:
    """Split a prefixed external_id back into (prospect_id, opportunity_id)."""
    ext = (external_id or "").strip()
    if ext.startswith("opp:"):
        return "", ext[4:]
    if ext.startswith("pr:"):
        return ext[3:], ""
    return ext, ""  # legacy/unprefixed -> treat as a bare prospect_id


def handle_webhook_event(event: DocuSealWebhookEvent) -> dict[str, Any]:
    """Process one DocuSeal webhook. Completion advances the deal via a
    ``contract.signed`` business event; non-terminal events are journaled only.
    """
    prospect_id, opportunity_id = _route_external_id(event.external_id)

    if not event.is_completed():
        _journal(
            "contract_webhook",
            {
                "ts": iso_now(),
                "event_type": event.event_type,
                "submission_id": event.submission_id,
                "external_id": event.external_id,
                "status": event.status,
            },
        )
        return {"received": True, "completed": False, "event_type": event.event_type}

    _journal(
        "contract_signed",
        {
            "ts": iso_now(),
            "event_type": event.event_type,
            "submission_id": event.submission_id,
            "submitter_id": event.submitter_id,
            "external_id": event.external_id,
            "prospect_id": prospect_id,
            "opportunity_id": opportunity_id,
            "email": event.email,
            "status": event.status,
        },
    )

    emit_business_event(
        CONTRACT_SIGNED,
        workcell="contracts",
        prospect_id=prospect_id or None,
        opportunity_id=opportunity_id or None,
        metadata={"submission_id": event.submission_id, "email": event.email},
    )
    return {
        "received": True,
        "completed": True,
        "prospect_id": prospect_id,
        "opportunity_id": opportunity_id,
        "submission_id": event.submission_id,
    }
