"""Typed boundary for the DocuSeal contract-signing flow.

Kept tolerant of DocuSeal API/webhook shape variations (self-hosted payloads
differ slightly by version) while giving the rest of Samus stable types.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ContractParty(BaseModel):
    """The customer who signs. ``role`` must match a role in the template."""

    email: str
    name: str = ""
    role: str = "First Party"


class ServiceAgreementRequest(BaseModel):
    """Inputs to generate one service-agreement signing request.

    ``template_values()`` prefills the template's named fields; DocuSeal ignores
    keys the template does not define, so extra keys are harmless.
    """

    prospect_id: str
    party: ContractParty
    company: str = ""
    scope: str = ""
    price_usd: str = ""
    term: str = ""
    notes: str = ""
    # Explicit field prefills, merged over (and winning against) the derived set.
    values: dict[str, Any] = Field(default_factory=dict)
    # Default False: Samus delivers the link itself through the outreach path.
    # Set True to have DocuSeal email the signer directly instead.
    send_email: bool = False
    # Correlates the DocuSeal submission back to the CRM opportunity on webhook.
    opportunity_id: str = ""

    def template_values(self) -> dict[str, Any]:
        derived = {
            "Company": self.company,
            "Client Name": self.party.name,
            "Client Email": self.party.email,
            "Scope of Work": self.scope,
            "Price": self.price_usd,
            "Term": self.term,
            "Notes": self.notes,
        }
        merged = {k: v for k, v in derived.items() if v}
        merged.update(self.values or {})
        return merged

    def external_id(self) -> str:
        """Correlation id echoed back on the completion webhook. Prefixed so the
        handler routes a completion to an opportunity vs a bare prospect."""
        if self.opportunity_id:
            return f"opp:{self.opportunity_id}"
        return f"pr:{self.prospect_id}"


class ContractResult(BaseModel):
    """Outcome of generating a signing request. ``ok`` gates downstream send."""

    ok: bool
    prospect_id: str = ""
    signing_url: str = ""
    submission_id: Optional[int] = None
    submitter_id: Optional[int] = None
    slug: str = ""
    status: str = ""
    error: str = ""


class DocuSealFieldArea(BaseModel):
    """Placement of a signer field on a PDF page (1-indexed ``page``).

    Coordinates follow DocuSeal's convention. Only needed for UNTAGGED PDFs; a
    document carrying ``{{...}}`` text tags is auto-placed by DocuSeal instead.
    """

    x: float
    y: float
    w: float
    h: float
    page: int = 1


class DocuSealField(BaseModel):
    """One signer field placed on a proposal PDF (signature, date, text, select).

    ``options`` applies to ``select`` / ``radio`` fields (e.g. the payment-option
    choice). Supply ``areas`` for untagged PDFs; omit for tag-detected fields.
    """

    name: str
    type: str = "signature"
    role: str = "First Party"
    required: bool = True
    areas: list[DocuSealFieldArea] = Field(default_factory=list)
    options: Optional[list[str]] = None

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "role": self.role,
            "required": self.required,
        }
        if self.areas:
            out["areas"] = [a.model_dump() for a in self.areas]
        if self.options:
            out["options"] = self.options
        return out


class ProposalAgreementRequest(BaseModel):
    """Turn an existing proposal PDF into a signing request (one-off).

    The PDF is provided as base64 (``pdf_base64``) or a fetchable ``pdf_url``.
    ``fields`` places the client signer block for untagged PDFs; leave empty when
    the PDF carries DocuSeal text tags (DocuSeal auto-detects them).
    """

    prospect_id: str
    party: ContractParty
    document_name: str = "Service Agreement"
    pdf_base64: str = ""
    pdf_url: str = ""
    fields: list[DocuSealField] = Field(default_factory=list)
    values: dict[str, Any] = Field(default_factory=dict)
    send_email: bool = False
    company: str = ""
    opportunity_id: str = ""

    def has_document(self) -> bool:
        return bool((self.pdf_base64 or "").strip() or (self.pdf_url or "").strip())

    def field_wire(self) -> list[dict[str, Any]]:
        return [f.to_wire() for f in self.fields]

    def external_id(self) -> str:
        if self.opportunity_id:
            return f"opp:{self.opportunity_id}"
        return f"pr:{self.prospect_id}"


class DocuSealWebhookEvent(BaseModel):
    """Parsed DocuSeal webhook (``submission.completed`` / ``form.completed``)."""

    event_type: str = ""
    submission_id: Optional[int] = None
    submitter_id: Optional[int] = None
    email: str = ""
    status: str = ""
    external_id: str = ""
    slug: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_wire(cls, body: dict[str, Any]) -> "DocuSealWebhookEvent":
        """Parse a DocuSeal webhook body, tolerant of wrapped vs flat shapes.

        Self-hosted DocuSeal posts either ``{"event_type", "data": {...}}`` or a
        flatter object; this handles both and never raises on missing keys.
        """
        if not isinstance(body, dict):
            return cls(raw={"_nonobject": str(body)[:200]})
        event_type = str(body.get("event_type") or body.get("event") or "")
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        submission = data.get("submission") if isinstance(data.get("submission"), dict) else {}

        raw_sid = data.get("submission_id") or submission.get("id") or (
            data.get("id") if event_type.startswith("submission") else None
        )
        try:
            submission_id = int(raw_sid) if raw_sid is not None else None
        except (TypeError, ValueError):
            submission_id = None

        raw_subm = data.get("submitter_id") or (
            data.get("id") if event_type.startswith("form") else None
        )
        try:
            submitter_id = int(raw_subm) if raw_subm is not None else None
        except (TypeError, ValueError):
            submitter_id = None

        return cls(
            event_type=event_type,
            submission_id=submission_id,
            submitter_id=submitter_id,
            email=str(data.get("email") or ""),
            status=str(data.get("status") or submission.get("status") or ""),
            external_id=str(data.get("external_id") or submission.get("external_id") or ""),
            slug=str(data.get("slug") or ""),
            raw=body,
        )

    def is_completed(self) -> bool:
        return (
            self.event_type in ("submission.completed", "form.completed")
            or self.status.strip().lower() in ("completed", "signed")
        )
