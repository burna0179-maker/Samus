"""DocuSeal API client — create signing requests, resolve signing links.

Two creation paths:
  * ``create_submission`` — from a stored template (``template_id``).
  * ``create_submission_from_pdf`` — one-off from a proposal PDF (base64 or
    URL). Signer fields come from explicit ``fields`` when provided; otherwise
    DocuSeal auto-detects ``{{field;role=...;type=...}}`` text tags in the
    document (how Samus-generated proposals will carry their signer block).

Talks to the self-hosted instance (``docuseal_api_base``, default the in-network
``http://samus-docuseal:3000/api``) with the ``X-Auth-Token`` API key. Sync +
fail-loud to the service layer, which converts errors to
``ContractResult(ok=False, error=...)`` so a DocuSeal outage never crashes a
campaign send.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from .models import ContractParty

_LOG = logging.getLogger("samus.contracts.docuseal")


class DocuSealError(Exception):
    """DocuSeal API call failed (transport, HTTP status, or bad response)."""


class DocuSealClient:
    def __init__(
        self,
        *,
        api_base: str,
        api_token: str,
        public_base: str = "",
        timeout_sec: float = 15.0,
    ) -> None:
        self._api_base = (api_base or "").rstrip("/")
        self._token = api_token or ""
        self._public_base = (public_base or "").rstrip("/")
        self._timeout = httpx.Timeout(timeout_sec)

    def _headers(self) -> dict[str, str]:
        return {
            "X-Auth-Token": self._token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def signing_url_for(self, slug: str, embed_src: str = "") -> str:
        """Customer-facing signing URL. Prefer our controlled public base over
        DocuSeal's echoed ``embed_src`` so links always use our domain."""
        slug = (slug or "").strip()
        if self._public_base and slug:
            return f"{self._public_base}/s/{slug}"
        return embed_src or ""

    # -- internals ---------------------------------------------------------

    def _submitter_payload(
        self, submitter: ContractParty, values: Optional[dict[str, Any]], external_id: str
    ) -> dict[str, Any]:
        sub: dict[str, Any] = {"role": submitter.role or "First Party", "email": submitter.email.strip()}
        if submitter.name:
            sub["name"] = submitter.name
        if external_id:
            sub["external_id"] = external_id
        if values:
            sub["values"] = values
        return sub

    def _submit(self, path: str, body: dict[str, Any]) -> list[dict[str, Any]]:
        """POST to a submissions endpoint; return the submitter list."""
        if not self._api_base or not self._token:
            raise DocuSealError("docuseal_client_unconfigured")
        url = f"{self._api_base}{path}"
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.post(url, json=body, headers=self._headers())
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            detail = exc.response.text[:300] if exc.response is not None else str(exc)
            _LOG.warning("docuseal %s HTTP %s: %s", path, status, detail)
            raise DocuSealError(f"docuseal_http_{status}: {detail}") from exc
        except httpx.HTTPError as exc:
            _LOG.warning("docuseal %s transport error: %s", path, exc)
            raise DocuSealError(f"docuseal_transport_error: {exc}") from exc

        try:
            data = resp.json()
        except ValueError as exc:
            raise DocuSealError(f"docuseal_non_json_response: {resp.text[:200]}") from exc

        if isinstance(data, dict) and isinstance(data.get("submitters"), list):
            return data["submitters"]
        if isinstance(data, list):
            return data
        raise DocuSealError(f"docuseal_unexpected_response: {str(data)[:200]}")

    # -- public API --------------------------------------------------------

    def create_submission(
        self,
        *,
        template_id: int,
        submitter: ContractParty,
        values: Optional[dict[str, Any]] = None,
        send_email: bool = False,
        external_id: str = "",
        order: str = "preserved",
        expire_at: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """POST /submissions for a stored ``template_id``."""
        if not template_id:
            raise DocuSealError("template_id_required")
        if not (submitter.email or "").strip():
            raise DocuSealError("submitter_email_required")
        body: dict[str, Any] = {
            "template_id": int(template_id),
            "send_email": bool(send_email),
            "order": order,
            "submitters": [self._submitter_payload(submitter, values, external_id)],
        }
        if expire_at:
            body["expire_at"] = expire_at
        return self._submit("/submissions", body)

    def create_submission_from_pdf(
        self,
        *,
        document_name: str,
        submitter: ContractParty,
        fields: Optional[list[dict[str, Any]]] = None,
        pdf_base64: str = "",
        pdf_url: str = "",
        values: Optional[dict[str, Any]] = None,
        send_email: bool = False,
        external_id: str = "",
        order: str = "preserved",
        expire_at: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """POST /submissions/pdf — one-off from a proposal PDF (base64 or URL).

        ``fields`` (DocuSeal field dicts) place the signer block for untagged
        PDFs; omit it to let DocuSeal auto-detect ``{{...}}`` text tags.
        """
        file_ref = (pdf_base64 or "").strip() or (pdf_url or "").strip()
        if not file_ref:
            raise DocuSealError("pdf_source_required")
        if not (submitter.email or "").strip():
            raise DocuSealError("submitter_email_required")
        document: dict[str, Any] = {"name": document_name or "agreement.pdf", "file": file_ref}
        if fields:
            document["fields"] = fields
        body: dict[str, Any] = {
            "name": document_name or "Agreement",
            "documents": [document],
            "send_email": bool(send_email),
            "order": order,
            "submitters": [self._submitter_payload(submitter, values, external_id)],
        }
        if expire_at:
            body["expire_at"] = expire_at
        return self._submit("/submissions/pdf", body)
