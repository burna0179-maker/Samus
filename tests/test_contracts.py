"""Tests for the DocuSeal contract-signing integration (backend.contracts)."""

from __future__ import annotations

import httpx
import pytest

from backend.common.config import reload_settings
from backend.contracts import client as client_mod
from backend.contracts import service as service_mod
from backend.contracts.client import DocuSealClient, DocuSealError
from backend.contracts.models import (
    ContractParty,
    DocuSealField,
    DocuSealFieldArea,
    DocuSealWebhookEvent,
    ProposalAgreementRequest,
    ServiceAgreementRequest,
)
from backend.contracts.signature import (
    DocuSealSignatureError,
    verify_docuseal_webhook,
)


@pytest.fixture(autouse=True)
def _reset_settings():
    """Each test may set DOCUSEAL_* env; drop the cached Settings afterward."""
    yield
    reload_settings()


# --- signature ------------------------------------------------------------


def test_verify_webhook_ok():
    verify_docuseal_webhook("s3cr3t", "s3cr3t")  # no raise


def test_verify_webhook_mismatch():
    with pytest.raises(DocuSealSignatureError):
        verify_docuseal_webhook("wrong", "s3cr3t")


def test_verify_webhook_missing_header():
    with pytest.raises(DocuSealSignatureError):
        verify_docuseal_webhook(None, "s3cr3t")


def test_verify_webhook_secret_unset_fails_closed():
    with pytest.raises(DocuSealSignatureError):
        verify_docuseal_webhook("anything", "")


# --- models ---------------------------------------------------------------


def test_external_id_prefix_opportunity():
    req = ServiceAgreementRequest(
        prospect_id="pr_1", party=ContractParty(email="a@b.co"), opportunity_id="op_9"
    )
    assert req.external_id() == "opp:op_9"


def test_external_id_prefix_prospect_only():
    req = ServiceAgreementRequest(prospect_id="pr_1", party=ContractParty(email="a@b.co"))
    assert req.external_id() == "pr:pr_1"


def test_template_values_drops_empty_and_merges_overrides():
    req = ServiceAgreementRequest(
        prospect_id="pr_1",
        party=ContractParty(email="a@b.co", name="Ann"),
        company="Acme",
        scope="Rescue",
        price_usd="500",
        values={"Price": "750", "Custom": "x"},
    )
    vals = req.template_values()
    assert vals["Company"] == "Acme"
    assert vals["Client Name"] == "Ann"
    assert vals["Price"] == "750"  # explicit override wins
    assert vals["Custom"] == "x"
    assert "Term" not in vals  # empty dropped


def test_webhook_from_wire_wrapped_completed():
    e = DocuSealWebhookEvent.from_wire(
        {
            "event_type": "submission.completed",
            "data": {"submission": {"id": 7, "status": "completed", "external_id": "opp:op_9"}},
        }
    )
    assert e.is_completed()
    assert e.submission_id == 7
    assert e.external_id == "opp:op_9"


def test_webhook_from_wire_form_completed_flat():
    e = DocuSealWebhookEvent.from_wire(
        {
            "event_type": "form.completed",
            "data": {"id": 3, "email": "a@b.co", "status": "completed"},
        }
    )
    assert e.is_completed()
    assert e.submitter_id == 3


def test_webhook_non_terminal_not_completed():
    e = DocuSealWebhookEvent.from_wire({"event_type": "form.viewed", "data": {"status": "opened"}})
    assert not e.is_completed()


# --- client (httpx mocked) ------------------------------------------------


class _FakeResp:
    def __init__(self, status=200, json_data=None, text=""):
        self.status_code = status
        self._json = json_data
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "err",
                request=httpx.Request("POST", "http://x"),
                response=httpx.Response(self.status_code, text=self.text),
            )

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _FakeClient:
    def __init__(self, resp, capture):
        self._resp = resp
        self._capture = capture

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None, headers=None):
        self._capture.update(url=url, json=json, headers=headers)
        return self._resp


def _patch_httpx(monkeypatch, resp, capture):
    monkeypatch.setattr(client_mod.httpx, "Client", lambda **kw: _FakeClient(resp, capture))


def test_client_create_submission_builds_body_and_parses(monkeypatch):
    capture: dict = {}
    resp = _FakeResp(json_data=[{"id": 11, "submission_id": 7, "slug": "abc123", "status": "sent"}])
    _patch_httpx(monkeypatch, resp, capture)

    c = DocuSealClient(
        api_base="http://ds:3000/api", api_token="tok", public_base="https://sign.example"
    )
    out = c.create_submission(
        template_id=1000001,
        submitter=ContractParty(email="a@b.co", name="Ann"),
        values={"Company": "Acme"},
        external_id="opp:op_9",
    )
    assert capture["url"] == "http://ds:3000/api/submissions"
    assert capture["headers"]["X-Auth-Token"] == "tok"
    body = capture["json"]
    assert body["template_id"] == 1000001
    assert body["submitters"][0]["email"] == "a@b.co"
    assert body["submitters"][0]["external_id"] == "opp:op_9"
    assert body["submitters"][0]["values"] == {"Company": "Acme"}
    assert out[0]["slug"] == "abc123"
    assert c.signing_url_for("abc123") == "https://sign.example/s/abc123"


def test_client_unconfigured_raises():
    c = DocuSealClient(api_base="", api_token="")
    with pytest.raises(DocuSealError):
        c.create_submission(template_id=1, submitter=ContractParty(email="a@b.co"))


def test_client_http_error_wrapped(monkeypatch):
    capture: dict = {}
    _patch_httpx(monkeypatch, _FakeResp(status=422, text="bad template"), capture)
    c = DocuSealClient(api_base="http://ds:3000/api", api_token="tok")
    with pytest.raises(DocuSealError) as ei:
        c.create_submission(template_id=1, submitter=ContractParty(email="a@b.co"))
    assert "422" in str(ei.value)


# --- service --------------------------------------------------------------


def _enable(monkeypatch, template_id="1000001"):
    monkeypatch.setenv("DOCUSEAL_ENABLED", "true")
    monkeypatch.setenv("DOCUSEAL_API_TOKEN", "tok")
    monkeypatch.setenv("DOCUSEAL_PUBLIC_BASE", "https://sign.example")
    monkeypatch.setenv("DOCUSEAL_SERVICE_AGREEMENT_TEMPLATE_ID", template_id)
    reload_settings()


def test_generate_disabled_returns_error(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCUSEAL_ENABLED", "false")
    reload_settings()
    r = service_mod.generate_service_agreement(
        ServiceAgreementRequest(prospect_id="pr_1", party=ContractParty(email="a@b.co"))
    )
    assert not r.ok
    assert r.error == "docuseal_disabled"


def test_generate_happy_path_emits_contract_sent(monkeypatch, tmp_path):
    _enable(monkeypatch)
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))

    events: list = []
    monkeypatch.setattr(
        service_mod,
        "emit_business_event",
        lambda et, **kw: events.append((et, kw)) or {},
    )
    monkeypatch.setattr(
        service_mod.DocuSealClient,
        "create_submission",
        lambda self, **kw: [{"id": 11, "submission_id": 7, "slug": "abc", "status": "sent"}],
    )

    r = service_mod.generate_service_agreement(
        ServiceAgreementRequest(
            prospect_id="pr_1",
            party=ContractParty(email="a@b.co", name="Ann"),
            company="Acme",
            scope="Rescue",
            price_usd="500",
            opportunity_id="op_9",
        )
    )
    assert r.ok
    assert r.signing_url == "https://sign.example/s/abc"
    assert r.submission_id == 7
    assert events and events[0][0] == service_mod.CONTRACT_SENT
    assert events[0][1]["opportunity_id"] == "op_9"


def test_generate_docuseal_error_is_soft(monkeypatch, tmp_path):
    _enable(monkeypatch)
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))

    def _boom(self, **kw):
        raise DocuSealError("docuseal_http_500: down")

    monkeypatch.setattr(service_mod.DocuSealClient, "create_submission", _boom)
    r = service_mod.generate_service_agreement(
        ServiceAgreementRequest(prospect_id="pr_1", party=ContractParty(email="a@b.co"))
    )
    assert not r.ok
    assert "docuseal_http_500" in r.error


def test_handle_webhook_completed_emits_signed(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    events: list = []
    monkeypatch.setattr(
        service_mod,
        "emit_business_event",
        lambda et, **kw: events.append((et, kw)) or {},
    )
    e = DocuSealWebhookEvent.from_wire(
        {
            "event_type": "submission.completed",
            "data": {"submission": {"id": 7, "status": "completed", "external_id": "opp:op_9"}},
        }
    )
    out = service_mod.handle_webhook_event(e)
    assert out["completed"] is True
    assert out["opportunity_id"] == "op_9"
    assert events and events[0][0] == service_mod.CONTRACT_SIGNED
    assert events[0][1]["opportunity_id"] == "op_9"


def test_handle_webhook_non_terminal_no_signed_event(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    events: list = []
    monkeypatch.setattr(
        service_mod,
        "emit_business_event",
        lambda et, **kw: events.append((et, kw)) or {},
    )
    e = DocuSealWebhookEvent.from_wire({"event_type": "form.viewed", "data": {"status": "opened"}})
    out = service_mod.handle_webhook_event(e)
    assert out["completed"] is False
    assert events == []


# --- PDF path: client -----------------------------------------------------


def test_client_create_from_pdf_builds_documents_and_fields(monkeypatch):
    capture: dict = {}
    resp = _FakeResp(json_data=[{"id": 11, "submission_id": 8, "slug": "pdf1", "status": "sent"}])
    _patch_httpx(monkeypatch, resp, capture)
    c = DocuSealClient(
        api_base="http://ds:3000/api", api_token="tok", public_base="https://sign.example"
    )
    fields = [
        {
            "name": "Client Signature",
            "type": "signature",
            "role": "Client",
            "areas": [{"x": 0.1, "y": 0.7, "w": 0.3, "h": 0.05, "page": 3}],
        }
    ]
    out = c.create_submission_from_pdf(
        document_name="Conqueror Proposal",
        submitter=ContractParty(email="school@x.edu", name="Head"),
        fields=fields,
        pdf_base64="JVBERi0=",
        external_id="opp:op_7",
    )
    assert capture["url"] == "http://ds:3000/api/submissions/pdf"
    body = capture["json"]
    assert body["documents"][0]["file"] == "JVBERi0="
    assert body["documents"][0]["fields"] == fields
    assert body["submitters"][0]["external_id"] == "opp:op_7"
    assert out[0]["slug"] == "pdf1"
    assert c.signing_url_for("pdf1") == "https://sign.example/s/pdf1"


def test_client_create_from_pdf_no_fields_uses_tag_detection(monkeypatch):
    capture: dict = {}
    _patch_httpx(monkeypatch, _FakeResp(json_data=[{"id": 1, "slug": "s"}]), capture)
    c = DocuSealClient(api_base="http://ds:3000/api", api_token="tok")
    c.create_submission_from_pdf(
        document_name="Doc",
        submitter=ContractParty(email="a@b.co"),
        pdf_url="https://x/doc.pdf",
    )
    assert "fields" not in capture["json"]["documents"][0]  # omitted -> tag detection
    assert capture["json"]["documents"][0]["file"] == "https://x/doc.pdf"


def test_client_create_from_pdf_requires_pdf():
    c = DocuSealClient(api_base="http://ds:3000/api", api_token="tok")
    with pytest.raises(DocuSealError):
        c.create_submission_from_pdf(document_name="D", submitter=ContractParty(email="a@b.co"))


# --- PDF path: models + service -------------------------------------------


def test_proposal_field_wire_and_external_id():
    req = ProposalAgreementRequest(
        prospect_id="pr_1",
        party=ContractParty(email="a@b.co"),
        pdf_base64="x",
        opportunity_id="op_7",
        fields=[
            DocuSealField(
                name="Sig",
                type="signature",
                areas=[DocuSealFieldArea(x=0.1, y=0.7, w=0.3, h=0.05, page=2)],
            )
        ],
    )
    assert req.has_document()
    assert req.external_id() == "opp:op_7"
    wire = req.field_wire()
    assert wire[0]["name"] == "Sig"
    assert wire[0]["areas"][0]["page"] == 2


def test_generate_proposal_no_document_returns_error(monkeypatch):
    monkeypatch.setenv("DOCUSEAL_ENABLED", "true")
    reload_settings()
    r = service_mod.generate_proposal_agreement(
        ProposalAgreementRequest(prospect_id="pr_1", party=ContractParty(email="a@b.co"))
    )
    assert not r.ok
    assert r.error == "pdf_source_required"


def test_generate_proposal_happy_path(monkeypatch, tmp_path):
    _enable(monkeypatch)
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    events: list = []
    monkeypatch.setattr(
        service_mod, "emit_business_event", lambda et, **kw: events.append((et, kw)) or {}
    )
    captured: dict = {}

    def _fake(self, **kw):
        captured.update(kw)
        return [{"id": 5, "submission_id": 8, "slug": "pdfabc", "status": "sent"}]

    monkeypatch.setattr(service_mod.DocuSealClient, "create_submission_from_pdf", _fake)
    r = service_mod.generate_proposal_agreement(
        ProposalAgreementRequest(
            prospect_id="pr_1",
            party=ContractParty(email="school@x.edu", name="Head"),
            company="Sample School",
            pdf_base64="JVBERi0=",
            opportunity_id="op_7",
        )
    )
    assert r.ok
    assert r.signing_url == "https://sign.example/s/pdfabc"
    assert captured["pdf_base64"] == "JVBERi0="
    assert captured["external_id"] == "opp:op_7"
    assert events and events[0][0] == service_mod.CONTRACT_SENT


def test_pdf_base64_from_path(tmp_path):
    import base64 as _b64

    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF-1.4 test")
    assert service_mod.pdf_base64_from_path(p) == _b64.b64encode(b"%PDF-1.4 test").decode()
