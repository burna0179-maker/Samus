"""ComplianceGuard (CAN-SPAM/FTC) unit tests + send_email integration tests."""
from __future__ import annotations

from typing import Any

import pytest

import backend.common.compliance_guard as cg

_UNSUB = "https://hustleforge.tech/unsubscribe"
_POSTAL = "HustleForge LLC, 123 D St, Marysville, CA 95901"
_FROM = "ahartman@hustleforge.tech"


def _set_env(
    monkeypatch,
    *,
    mode: str = "off",
    unsub: str = _UNSUB,
    postal: str = _POSTAL,
    from_email: str = _FROM,
    reply_to: str = "",
    from_domains: str = "",
    backend: str = "sendgrid",
) -> None:
    monkeypatch.setenv("SAMUS_COMPLIANCE_GUARD_MODE", mode)
    monkeypatch.setenv("SAMUS_UNSUBSCRIBE_URL", unsub)
    monkeypatch.setenv("SAMUS_SENDER_POSTAL_ADDRESS", postal)
    monkeypatch.setenv("SENDGRID_FROM_EMAIL", from_email)
    monkeypatch.setenv("SENDGRID_REPLY_TO", reply_to)
    monkeypatch.setenv("SAMUS_COMPLIANCE_FROM_DOMAINS", from_domains)
    monkeypatch.setenv("EMAIL_BACKEND", backend)
    from backend.common.settings import reload_settings
    reload_settings()


@pytest.fixture(autouse=True)
def _not_suppressed(monkeypatch):
    """Default: nobody is suppressed. Tests that need a hit override this."""
    monkeypatch.setattr(cg, "is_email_suppressed", lambda email: False)


def _compliant_body() -> str:
    return f"Hi there — quick note about your site.\n\n---\n{_POSTAL}\nUnsubscribe: {_UNSUB}"


def _msg(**kw: Any) -> cg.ComplianceMessage:
    base = dict(
        to="lead@example.com",
        subject="Quick question about your business",
        body=_compliant_body(),
        from_addr=_FROM,
        kind="commercial",
    )
    base.update(kw)
    return cg.ComplianceMessage(**base)


# ---------------------------------------------------------------------------
# evaluate() — commercial structural rules
# ---------------------------------------------------------------------------

def test_compliant_commercial_message_is_ok(monkeypatch):
    _set_env(monkeypatch)
    v = cg.evaluate(_msg())
    assert v.ok is True
    assert v.reasons == ()
    assert v.score == pytest.approx(1.0)
    assert "List-Unsubscribe" in v.headers


def test_missing_unsubscribe_blocks(monkeypatch):
    _set_env(monkeypatch)
    v = cg.evaluate(_msg(body=f"No way out here.\n{_POSTAL}"))
    assert v.ok is False
    assert "missing_unsubscribe" in v.reasons


def test_postal_unconfigured_blocks(monkeypatch):
    _set_env(monkeypatch, postal="")
    v = cg.evaluate(_msg())
    assert v.ok is False
    assert "postal_address_unconfigured" in v.reasons


def test_postal_configured_but_absent_from_body_blocks(monkeypatch):
    _set_env(monkeypatch)
    body = f"Hi there.\n\nUnsubscribe: {_UNSUB}"  # has unsub, no postal text
    v = cg.evaluate(_msg(body=body))
    assert v.ok is False
    assert "postal_address_missing_from_body" in v.reasons


def test_empty_subject_blocks(monkeypatch):
    _set_env(monkeypatch)
    v = cg.evaluate(_msg(subject="   "))
    assert v.ok is False
    assert "empty_subject" in v.reasons


def test_reply_to_opt_out_instruction_satisfies_unsubscribe(monkeypatch):
    # No configured URL; a reply-to-opt-out instruction is a valid mechanism.
    _set_env(monkeypatch, unsub="")
    body = f"Hi there.\n\n{_POSTAL}\nReply STOP to unsubscribe at any time."
    v = cg.evaluate(_msg(body=body))
    assert "missing_unsubscribe" not in v.reasons


def test_thread_prefix_subject_is_soft_warning_not_block(monkeypatch):
    _set_env(monkeypatch)
    v = cg.evaluate(_msg(subject="Re: our conversation"))
    assert v.ok is True  # warning does not block
    assert "subject_thread_prefix" in v.warnings
    assert v.score < 1.0


def test_from_domain_unconfigured_is_soft_warning(monkeypatch):
    _set_env(monkeypatch)  # configured sender domain is hustleforge.tech
    v = cg.evaluate(_msg(from_addr="random@gmail.com"))
    assert v.ok is True
    assert "from_domain_unconfigured" in v.warnings


def test_from_domain_extra_allowlist_suppresses_warning(monkeypatch):
    _set_env(monkeypatch, from_domains="gmail.com")
    v = cg.evaluate(_msg(from_addr="random@gmail.com"))
    assert "from_domain_unconfigured" not in v.warnings


# ---------------------------------------------------------------------------
# evaluate() — transactional exemption + suppression (all kinds)
# ---------------------------------------------------------------------------

def test_transactional_exempt_from_unsub_and_postal(monkeypatch):
    _set_env(monkeypatch)
    v = cg.evaluate(_msg(kind="transactional", body="Your receipt: $149. Thanks!"))
    assert v.ok is True
    assert v.reasons == ()
    assert v.headers == {}  # no List-Unsubscribe on transactional mail


def test_suppressed_recipient_blocks_commercial(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setattr(cg, "is_email_suppressed", lambda email: True)
    v = cg.evaluate(_msg())
    assert v.ok is False
    assert "recipient_suppressed" in v.reasons


def test_suppressed_recipient_blocks_even_transactional(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setattr(cg, "is_email_suppressed", lambda email: True)
    v = cg.evaluate(_msg(kind="transactional", body="Your receipt."))
    assert v.ok is False
    assert "recipient_suppressed" in v.reasons


# ---------------------------------------------------------------------------
# headers / mode / coercion helpers
# ---------------------------------------------------------------------------

def test_list_unsubscribe_https_is_one_click(monkeypatch):
    _set_env(monkeypatch, reply_to="support@hustleforge.tech")
    from backend.common.config import get_settings
    headers = cg.build_list_unsubscribe_headers(get_settings(), "commercial")
    assert f"<{_UNSUB}>" in headers["List-Unsubscribe"]
    assert "mailto:support@hustleforge.tech" in headers["List-Unsubscribe"]
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_list_unsubscribe_empty_for_transactional(monkeypatch):
    _set_env(monkeypatch)
    from backend.common.config import get_settings
    assert cg.build_list_unsubscribe_headers(get_settings(), "transactional") == {}


def test_mailto_only_unsub_has_no_one_click(monkeypatch):
    _set_env(monkeypatch, unsub="mailto:optout@hustleforge.tech")
    from backend.common.config import get_settings
    headers = cg.build_list_unsubscribe_headers(get_settings(), "commercial")
    assert "List-Unsubscribe" in headers
    assert "List-Unsubscribe-Post" not in headers  # no one-click without http(s)


@pytest.mark.parametrize("raw,expected", [
    ("off", "off"), ("audit", "audit"), ("enforce", "enforce"),
    ("ENFORCE", "enforce"), ("garbage", "off"), ("", "off"),
])
def test_get_mode_resolves(monkeypatch, raw, expected):
    _set_env(monkeypatch, mode=raw)
    assert cg.get_mode() == expected


@pytest.mark.parametrize("raw,expected", [
    ("transactional", "transactional"), ("TRANSACTIONAL", "transactional"),
    ("commercial", "commercial"), ("", "commercial"), (None, "commercial"),
    ("nonsense", "commercial"),
])
def test_coerce_kind(raw, expected):
    assert cg.coerce_kind(raw) == expected


# ---------------------------------------------------------------------------
# send_email integration — the three modes
# ---------------------------------------------------------------------------

def _capture_sendgrid(monkeypatch) -> dict:
    cap: dict = {}

    def _fake(**kw: Any) -> dict[str, str]:
        cap.update(kw)
        return {"message_id": "x", "channel": "email", "to": kw.get("to", ""), "ts": "t"}

    import backend.common.email_backend as adapter
    monkeypatch.setattr(adapter, "send_email_via_sendgrid", _fake)
    return cap


def test_send_off_mode_never_consults_guard(monkeypatch):
    _set_env(monkeypatch, mode="off")
    # Even a suppressed recipient is NOT blocked when the guard is off.
    monkeypatch.setattr(cg, "is_email_suppressed", lambda email: True)
    cap = _capture_sendgrid(monkeypatch)
    from backend.common.email_backend import send_email
    out = send_email("lead@example.com", "s", "b")
    assert out["message_id"] == "x"          # send went through
    assert cap.get("headers") is None        # no headers injected in off mode


def test_send_audit_mode_injects_headers_without_blocking(monkeypatch):
    _set_env(monkeypatch, mode="audit")
    cap = _capture_sendgrid(monkeypatch)
    from backend.common.email_backend import send_email
    # Non-compliant commercial body (no postal/unsub) — audit must NOT block.
    out = send_email("lead@example.com", "Subject", "bare body, not compliant")
    assert out["message_id"] == "x"
    assert "List-Unsubscribe" in (cap.get("headers") or {})  # headers still injected


def test_send_enforce_blocks_noncompliant_commercial(monkeypatch):
    _set_env(monkeypatch, mode="enforce")
    _capture_sendgrid(monkeypatch)
    from backend.common.email_backend import EmailBackendError, send_email
    with pytest.raises(EmailBackendError) as ei:
        send_email("lead@example.com", "Subject", "bare body, not compliant")
    assert "compliance_block" in str(ei.value)


def test_send_enforce_allows_compliant_commercial(monkeypatch):
    _set_env(monkeypatch, mode="enforce")
    cap = _capture_sendgrid(monkeypatch)
    from backend.common.email_backend import send_email
    out = send_email("lead@example.com", "Subject", _compliant_body())
    assert out["message_id"] == "x"
    assert "List-Unsubscribe" in (cap.get("headers") or {})


def test_send_enforce_allows_transactional_bare_body(monkeypatch):
    _set_env(monkeypatch, mode="enforce")
    cap = _capture_sendgrid(monkeypatch)
    from backend.common.email_backend import send_email
    out = send_email(
        "buyer@example.com", "Your receipt", "Thanks for your purchase.",
        message_kind="transactional",
    )
    assert out["message_id"] == "x"
    assert cap.get("headers") is None  # transactional gets no List-Unsubscribe


def test_send_enforce_blocks_suppressed_even_transactional(monkeypatch):
    _set_env(monkeypatch, mode="enforce")
    monkeypatch.setattr(cg, "is_email_suppressed", lambda email: True)
    _capture_sendgrid(monkeypatch)
    from backend.common.email_backend import EmailBackendError, send_email
    with pytest.raises(EmailBackendError) as ei:
        send_email("buyer@example.com", "Your receipt", "Thanks.", message_kind="transactional")
    assert "recipient_suppressed" in str(ei.value)


def test_send_caller_headers_merge_with_guard_headers(monkeypatch):
    _set_env(monkeypatch, mode="audit")
    cap = _capture_sendgrid(monkeypatch)
    from backend.common.email_backend import send_email
    send_email(
        "lead@example.com", "Subject", _compliant_body(),
        headers={"X-Campaign": "spring"},
    )
    hdrs = cap.get("headers") or {}
    assert hdrs.get("X-Campaign") == "spring"     # caller header preserved
    assert "List-Unsubscribe" in hdrs             # guard header added
