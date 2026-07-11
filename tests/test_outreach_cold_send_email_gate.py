"""B-5 cold-send email-quality gate.

Two layers under test:

  * :func:`backend.prospecting.contact_validation.is_cold_sendable_email` — the
    reusable predicate that rejects role / system mailboxes and malformed
    addresses so they are never used as a COLD-outreach ``to``.
  * :func:`backend.outreach.cash_engine_entry._select_cold_send_email` — the
    selection path that prefers ``owner_email``, falls back to a sendable
    address in ``contact_emails``, and yields ``""`` (not-cold-sendable) when
    neither is usable.

Motivating real case (2026-06-22): ``bugreport@moatable.com`` was scraped off a
site footer and attached to the "Erik Tejeda" call card. Cold-mailing that
role mailbox burns the SendGrid sender-domain reputation.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.outreach.cash_engine_entry import _select_cold_send_email
from backend.prospecting.contact_validation import is_cold_sendable_email


# --- the predicate ---------------------------------------------------------


def test_role_and_system_mailboxes_are_not_cold_sendable():
    for bad in (
        "bugreport@moatable.com",  # the motivating footer scrape
        "admin@acme.com",
        "support@acme.com",
        "noreply@acme.com",
        "no-reply@acme.com",
        "info@acme.com",
        "postmaster@acme.com",
        "abuse@acme.com",
        "sales@acme.com",
        "contact@acme.com",
        "hello@acme.com",
        "webmaster@acme.com",
        "privacy@acme.com",
        "legal@acme.com",
        "security@acme.com",
        "marketing@acme.com",
        "help@acme.com",
        "team@acme.com",
        "office@acme.com",
        "mailer-daemon@acme.com",
    ):
        assert is_cold_sendable_email(bad) is False, bad


def test_empty_and_malformed_addresses_are_not_cold_sendable():
    for bad in ("", "   ", "not-an-email", "erik@magnolia-.com", "@acme.com"):
        assert is_cold_sendable_email(bad) is False, bad


def test_normal_personal_address_is_cold_sendable():
    for ok in (
        "erik.tejeda@acme.com",
        "etejeda@acme.com",
        "jane@dentistyubacity.com",
        "owner+tag@example.io",
    ):
        assert is_cold_sendable_email(ok) is True, ok


# --- the selection path ----------------------------------------------------


@dataclass
class _FakeProspect:
    prospect_id: str = "p1"
    owner_email: str = ""
    contact_emails: str = ""


def test_select_prefers_a_sendable_owner_email():
    p = _FakeProspect(
        owner_email="erik.tejeda@acme.com",
        contact_emails="info@acme.com; erik.tejeda@acme.com",
    )
    assert _select_cold_send_email(p) == "erik.tejeda@acme.com"


def test_select_falls_back_when_owner_email_is_a_role_mailbox():
    """bugreport@moatable.com sits in owner_email; the gate rejects it and
    falls back to the sendable address in contact_emails."""
    p = _FakeProspect(
        owner_email="bugreport@moatable.com",
        contact_emails="bugreport@moatable.com; erik.tejeda@moatable.com",
    )
    assert _select_cold_send_email(p) == "erik.tejeda@moatable.com"


def test_select_returns_empty_when_nothing_is_sendable():
    """owner_email + every contact_emails entry is a role/system mailbox ->
    not cold-sendable (caller blocks the send)."""
    p = _FakeProspect(
        owner_email="bugreport@moatable.com",
        contact_emails="info@moatable.com; support@moatable.com",
    )
    assert _select_cold_send_email(p) == ""


def test_select_returns_empty_when_both_fields_missing():
    assert _select_cold_send_email(_FakeProspect()) == ""
