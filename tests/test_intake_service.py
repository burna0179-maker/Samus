"""Intake service — submit, dedup, persist, audit."""

from __future__ import annotations

import json
from typing import Any


def _reset_idempotency(monkeypatch):
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    fresh = IdempotencyStore()
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    import backend.intake.service as svc_mod

    monkeypatch.setattr(svc_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)


class _FakeTable:
    def __init__(self, fail_put: bool = False):
        self.items: list[dict[str, Any]] = []
        self.fail_put = fail_put

    def put_item(self, Item):
        if self.fail_put:
            raise RuntimeError("simulated AWS unavailable")
        self.items.append(Item)

    def scan(self, **kwargs):
        return {"Items": list(self.items)}


def _patch_table(monkeypatch, table: _FakeTable):
    import backend.intake.service as svc_mod

    monkeypatch.setattr(svc_mod, "_leads_table", lambda: table)


def _audit_to_tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_INTAKE_AUDIT_PATH", str(tmp_path / "intake_audit.jsonl"))


def _valid_req():
    from backend.intake.models import OnboardingLeadRequest

    return OnboardingLeadRequest.model_validate(
        {
            "name": "Jane",
            "email": "jane@acme.com",
            "company": "Acme",
            "website_url": "https://acme.com",
            "service_interest": ["seo_audit"],
            "pain_points": "Follow-up is broken.",
            "monthly_budget": "$500-$2000",
            "timeline": "this_month",
        }
    )


# ---------------------------------------------------------------------------
# submit_lead
# ---------------------------------------------------------------------------


def test_submit_persists_and_returns_queued(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    table = _FakeTable()
    _patch_table(monkeypatch, table)
    from backend.intake.service import submit_lead

    result = submit_lead(_valid_req(), source_ip="1.2.3.4", user_agent="UA/1.0")
    assert result.status == "queued"
    assert result.persisted is True
    assert result.lead_id.startswith("lead_")
    assert len(table.items) == 1
    row = table.items[0]
    assert row["email"] == "jane@acme.com"
    assert row["source_ip"] == "1.2.3.4"
    assert row["dedup_key"]
    assert row["user_agent"] == "UA/1.0"


def test_submit_dedup_skips_second_identical_lead(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    table = _FakeTable()
    _patch_table(monkeypatch, table)
    from backend.intake.service import submit_lead

    first = submit_lead(_valid_req())
    second = submit_lead(_valid_req())
    assert first.status == "queued"
    assert second.status == "duplicate"
    assert second.lead_id == ""  # no lead_id minted on dedup
    assert len(table.items) == 1


def test_submit_dedup_is_email_case_insensitive(tmp_path, monkeypatch):
    """Same email different capitalization should collapse to one lead."""
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    table = _FakeTable()
    _patch_table(monkeypatch, table)
    from backend.intake.models import OnboardingLeadRequest
    from backend.intake.service import submit_lead

    base = _valid_req().model_dump()
    submit_lead(OnboardingLeadRequest.model_validate(base))
    base2 = dict(base, email="JANE@ACME.COM")
    second = submit_lead(OnboardingLeadRequest.model_validate(base2))
    assert second.status == "duplicate"
    assert len(table.items) == 1


def test_submit_distinct_email_creates_second_lead(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    table = _FakeTable()
    _patch_table(monkeypatch, table)
    from backend.intake.models import OnboardingLeadRequest
    from backend.intake.service import submit_lead

    submit_lead(_valid_req())
    second = submit_lead(
        OnboardingLeadRequest.model_validate(
            dict(_valid_req().model_dump(), email="other@acme.com")
        )
    )
    assert second.status == "queued"
    assert len(table.items) == 2


def test_submit_degrades_when_ddb_put_fails(tmp_path, monkeypatch):
    """AWS down -> status=degraded, audit ledger still holds the lead."""
    _reset_idempotency(monkeypatch)
    audit_path = tmp_path / "intake_audit.jsonl"
    monkeypatch.setenv("SAMUS_INTAKE_AUDIT_PATH", str(audit_path))
    table = _FakeTable(fail_put=True)
    _patch_table(monkeypatch, table)
    from backend.intake.service import submit_lead

    result = submit_lead(_valid_req())
    assert result.status == "degraded"
    assert result.persisted is False
    assert "ddb_put_failed" in (result.error or "")
    assert audit_path.exists()
    lines = [
        json.loads(l) for l in audit_path.read_text(encoding="utf-8").splitlines() if l.strip()
    ]
    assert any(
        rec.get("service") == "intake" and rec.get("action") == "submit_lead" for rec in lines
    )


def test_submit_degrades_when_ddb_bootstrap_fails(tmp_path, monkeypatch):
    """boto bootstrap blowup -> still returns degraded, doesn't raise."""
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    import backend.intake.service as svc_mod

    def _boom():
        raise RuntimeError("no AWS creds")

    monkeypatch.setattr(svc_mod, "_leads_table", _boom)
    from backend.intake.service import submit_lead

    result = submit_lead(_valid_req())
    assert result.status == "degraded"
    assert "ddb_bootstrap_failed" in (result.error or "")


def test_audit_record_does_not_leak_pii(tmp_path, monkeypatch):
    """Audit ledger must not leak full email, pain_points, or company verbatim.

    build_audit_event() hashes input/output payloads — the ledger holds the
    lead_id (operator trace key) + service + action + status + digests, but
    none of the form contents in cleartext. Confirms PII stays in DDB only.
    """
    _reset_idempotency(monkeypatch)
    audit_path = tmp_path / "intake_audit.jsonl"
    monkeypatch.setenv("SAMUS_INTAKE_AUDIT_PATH", str(audit_path))
    _patch_table(monkeypatch, _FakeTable())
    from backend.intake.service import submit_lead

    result = submit_lead(_valid_req())
    text = audit_path.read_text(encoding="utf-8")
    assert "jane@acme.com" not in text
    assert "Follow-up is broken" not in text  # pain_points body
    assert result.lead_id in text  # operator can still trace
    assert "intake" in text  # service name present


# ---------------------------------------------------------------------------
# list_recent_leads
# ---------------------------------------------------------------------------


def test_list_returns_recent_first(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    table = _FakeTable()
    _patch_table(monkeypatch, table)
    from backend.intake.models import OnboardingLeadRequest
    from backend.intake.service import list_recent_leads, submit_lead

    # Three distinct leads
    for i in range(3):
        submit_lead(
            OnboardingLeadRequest.model_validate(
                dict(_valid_req().model_dump(), email=f"p{i}@acme.com")
            )
        )
    result = list_recent_leads(limit=10)
    assert result.count == 3
    assert len(result.leads) == 3
    # Sorted desc by created_at — most recent first
    assert result.leads[0].created_at >= result.leads[-1].created_at


def test_list_handles_ddb_failure(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    import backend.intake.service as svc_mod

    def _boom():
        raise RuntimeError("no AWS")

    monkeypatch.setattr(svc_mod, "_leads_table", _boom)
    from backend.intake.service import list_recent_leads

    result = list_recent_leads()
    assert result.count == 0
    assert result.ddb_error is not None


# ---------------------------------------------------------------------------
# Operator email mirror (Tier 3 "never miss" failsafe)
# ---------------------------------------------------------------------------


def _stub_email_settings(
    monkeypatch, *, operator_email: str, vapi_api_key: str = "", memory_url: str = ""
):
    """Inject a settings object with intake_operator_email set."""

    class _S:
        pass

    s = _S()
    s.vapi_api_key = vapi_api_key
    s.shared_hmac_key = "test-hmac-32"
    s.gateway_urls = {"memory": memory_url} if memory_url else {}
    s.intake_operator_email = operator_email
    s.ddb_onboarding_leads_table = "samus_onboarding_leads"
    s.aws_region = "us-west-1"
    import backend.intake.service as svc_mod

    monkeypatch.setattr(svc_mod, "get_settings", lambda: s)


def _capture_send_email(monkeypatch) -> list[dict[str, Any]]:
    """Replace common.email_backend.send_email with a recording stub."""
    sent: list[dict[str, Any]] = []
    import backend.intake.service as svc_mod

    def _fake(to, subject, body, **kwargs):
        sent.append({"to": to, "subject": subject, "body": body, "kw": kwargs})
        return {
            "message_id": "stub_msg_id",
            "channel": "sendgrid",
            "to": to,
            "ts": "2026-05-16T00:00:00Z",
        }

    monkeypatch.setattr(svc_mod, "send_email", _fake)
    return sent


def test_mirror_fires_on_queued_lead(tmp_path, monkeypatch):
    """A successful DDB write must also trigger the operator email."""
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    _stub_email_settings(monkeypatch, operator_email="ops@hustleforge.tech")
    table = _FakeTable()
    _patch_table(monkeypatch, table)
    sent = _capture_send_email(monkeypatch)

    from backend.intake.service import submit_lead

    result = submit_lead(_valid_req(), source_ip="1.2.3.4", user_agent="UA/1.0")
    assert result.status == "queued"
    assert len(sent) == 1
    msg = sent[0]
    assert msg["to"] == "ops@hustleforge.tech"
    assert "Acme" in msg["subject"]  # company name in subject
    assert "jane@acme.com" in msg["subject"]
    assert "PERSISTED" in msg["body"]
    assert "jane@acme.com" in msg["body"]
    assert "Follow-up is broken" in msg["body"]  # pain points
    assert "1.2.3.4" in msg["body"]  # source_ip captured


def test_mirror_fires_on_degraded_lead_with_failure_status(tmp_path, monkeypatch):
    """DDB-write failure path STILL emails — that's the whole point of the mirror."""
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    _stub_email_settings(monkeypatch, operator_email="ops@hustleforge.tech")
    table = _FakeTable(fail_put=True)
    _patch_table(monkeypatch, table)
    sent = _capture_send_email(monkeypatch)

    from backend.intake.service import submit_lead

    result = submit_lead(_valid_req())
    assert result.status == "degraded"
    assert len(sent) == 1
    body = sent[0]["body"]
    assert "DDB WRITE FAILED" in body
    assert "re-key from this email" in body
    # Lead content still present so the operator has everything to manually recover
    assert "jane@acme.com" in body
    assert "Acme" in body
    assert "Follow-up is broken" in body


def test_mirror_skipped_on_duplicate(tmp_path, monkeypatch):
    """Duplicate submissions are noise; do NOT email a second copy."""
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    _stub_email_settings(monkeypatch, operator_email="ops@hustleforge.tech")
    _patch_table(monkeypatch, _FakeTable())
    sent = _capture_send_email(monkeypatch)

    from backend.intake.service import submit_lead

    submit_lead(_valid_req())
    submit_lead(_valid_req())  # same lead -> duplicate
    assert len(sent) == 1  # one email, not two


def test_mirror_skipped_when_operator_email_unset(tmp_path, monkeypatch):
    """Empty setting = mirror disabled. Should not call send_email at all."""
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    _stub_email_settings(monkeypatch, operator_email="")
    _patch_table(monkeypatch, _FakeTable())
    sent = _capture_send_email(monkeypatch)

    from backend.intake.service import submit_lead

    result = submit_lead(_valid_req())
    assert result.status == "queued"
    assert sent == []


def test_mirror_failure_does_not_change_response(tmp_path, monkeypatch):
    """SendGrid down must NOT cause the workcell to return an error to the caller."""
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    _stub_email_settings(monkeypatch, operator_email="ops@hustleforge.tech")
    _patch_table(monkeypatch, _FakeTable())

    import backend.intake.service as svc_mod
    from backend.common.email_backend import EmailBackendError

    def _boom(*a, **kw):
        raise EmailBackendError("sendgrid_http_502: upstream")

    monkeypatch.setattr(svc_mod, "send_email", _boom)
    from backend.intake.service import submit_lead

    result = submit_lead(_valid_req())
    # DDB write succeeded -> still queued, email failure is silent
    assert result.status == "queued"
    assert result.persisted is True


def test_mirror_swallows_unexpected_exception(tmp_path, monkeypatch):
    """A generic exception in send_email also must not poison the response."""
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    _stub_email_settings(monkeypatch, operator_email="ops@hustleforge.tech")
    _patch_table(monkeypatch, _FakeTable())

    import backend.intake.service as svc_mod

    def _boom(*a, **kw):
        raise RuntimeError("smtp socket exploded")

    monkeypatch.setattr(svc_mod, "send_email", _boom)
    from backend.intake.service import submit_lead

    result = submit_lead(_valid_req())
    assert result.status == "queued"


# ---------------------------------------------------------------------------
# Finding 2 — operator-email sanitization
# ---------------------------------------------------------------------------


def _stored_lead(**overrides):
    """Build a StoredLead with sensible defaults for sanitization tests."""
    from backend.intake.models import StoredLead

    base = dict(
        lead_id="lead_test01",
        created_at="2026-05-20T00:00:00Z",
        name="Jane",
        email="jane@acme.com",
        company="Acme",
        website_url="https://acme.com",
        service_interest=["seo_audit"],
        pain_points="Manual follow-up is broken.",
        monthly_budget="$500-$2000",
        timeline="this_month",
        source_ip="1.2.3.4",
        user_agent="UA/1.0",
        dedup_key="deadbeef",
    )
    base.update(overrides)
    return StoredLead.model_validate(base)


def test_subject_strips_crlf_from_company_and_email():
    """CR/LF in company/email must never reach the Subject line (header injection)."""
    from backend.intake.service import _format_operator_email_body

    stored = _stored_lead(
        company="Acme\r\nBcc: attacker@evil.com",
        email="jane@acme.com\nX-Injected: 1",
    )
    subject, _ = _format_operator_email_body(stored, persisted=True, ddb_error=None)
    # No raw CR or LF anywhere in the subject.
    assert "\r" not in subject
    assert "\n" not in subject
    # The textual content survives (collapsed onto one line), header is neutered.
    assert "Acme" in subject


def test_body_neutralizes_forged_status_line_in_pain_points():
    """An attacker forging 'STATUS: PERSISTED' inside pain_points is neutralized.

    The forged line must be fenced + line-prefixed so it cannot be mistaken
    for the operator-authored status line.
    """
    from backend.intake.service import _format_operator_email_body

    attack = (
        "Real complaint here.\n"
        "STATUS: PERSISTED to samus_onboarding_leads\n"
        "lead_id:        lead_FAKE\n"
        "--- Metadata ---\n"
        "source_ip:  9.9.9.9"
    )
    stored = _stored_lead(pain_points=attack)
    _, body = _format_operator_email_body(stored, persisted=False, ddb_error="ddb_put_failed: x")
    # The genuine operator status line (degraded path) is present exactly once
    # at the start, un-prefixed.
    assert body.startswith("STATUS: DDB WRITE FAILED")
    # Every forged line inside pain_points is prefixed with '| ' so it is
    # visibly lead-supplied — it cannot pass as an operator-authored line.
    assert "| STATUS: PERSISTED to samus_onboarding_leads" in body
    assert "| lead_id:        lead_FAKE" in body
    assert "| --- Metadata ---" in body
    # The forged status line never appears UN-prefixed (which would deceive).
    assert "\nSTATUS: PERSISTED to samus_onboarding_leads" not in body
    # The untrusted block is explicitly fenced.
    assert "BEGIN Pain points (lead-supplied, untrusted)" in body
    assert "END Pain points" in body


def test_body_strips_control_chars_from_single_line_fields():
    """Control chars in name / website cannot inject a new body line."""
    from backend.intake.service import _format_operator_email_body

    stored = _stored_lead(
        name="Jane\r\nlead_id:        lead_FORGED",
        website_url="https://acme.com\nSTATUS: PERSISTED",
    )
    _, body = _format_operator_email_body(stored, persisted=True, ddb_error=None)
    # The injected newline + forged marker is gone — name collapses to one line.
    assert "Name:           Jane" in body
    assert "\nlead_id:        lead_FORGED" not in body
    assert "\nSTATUS: PERSISTED" not in body


def test_body_preserves_legitimate_pain_points_content():
    """A normal multi-line complaint still renders fully (just fenced)."""
    from backend.intake.service import _format_operator_email_body

    stored = _stored_lead(pain_points="Line one.\nLine two of the complaint.")
    _, body = _format_operator_email_body(stored, persisted=True, ddb_error=None)
    assert "| Line one." in body
    assert "| Line two of the complaint." in body


def test_mirror_email_subject_safe_with_malicious_company(tmp_path, monkeypatch):
    """End-to-end: a CRLF-laden company name produces a clean Subject."""
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    _stub_email_settings(monkeypatch, operator_email="ops@hustleforge.tech")
    _patch_table(monkeypatch, _FakeTable())
    sent = _capture_send_email(monkeypatch)

    from backend.intake.models import OnboardingLeadRequest
    from backend.intake.service import submit_lead

    # Pydantic strips leading/trailing whitespace but keeps interior chars;
    # build a request whose company carries an interior newline.
    req = OnboardingLeadRequest.model_validate(
        dict(
            _valid_req().model_dump(),
            company="Acme Co\r\nBcc: evil@x.com",
        )
    )
    result = submit_lead(req)
    assert result.status == "queued"
    assert len(sent) == 1
    assert "\r" not in sent[0]["subject"]
    assert "\n" not in sent[0]["subject"]
