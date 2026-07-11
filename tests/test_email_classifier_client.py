"""Tests for the ``client_correspondence`` prong of email_classifier.classify."""

from __future__ import annotations

from unittest.mock import patch

from backend.crm.client_directory import KnownClient
from backend.intake.email_classifier import classify
from backend.intake.gmail_poller import ParsedInboundEmail


def _mk_email(from_addr: str, subject: str = "Re: hello", body: str = "") -> ParsedInboundEmail:
    return ParsedInboundEmail(
        message_id="<t@test>",
        from_addr=from_addr,
        from_display=from_addr,
        to_addrs=["samushustleforge@gmail.com"],
        subject=subject,
        date_header="",
        body_text=body,
        body_format="text",
        attachment_names=[],
    )


_KERRY = KnownClient(
    email="<client-email>@example.com",
    client_id="sample_school",
    campaign_id="sample_school_enrollment_2026",
    template_id="school_enrollment_campaign",
    role="approval_contact",
    display_name="Kerry Brown",
    vertical="education",
    docuseal_slug="bJ1CqmfM2vbjv9",
    yaml_path="/opt/samus/clients/sample_school/campaign.yaml",
)


def test_known_client_wins_over_business():
    email = _mk_email("<client-email>@example.com", "Question about the plan")
    with patch("backend.crm.client_directory.lookup_client", return_value=_KERRY):
        cls = classify(email)
    assert cls.category == "client_correspondence"
    assert cls.confidence == 1.0
    assert cls.client_id == "sample_school"
    assert cls.campaign_id == "sample_school_enrollment_2026"
    assert cls.client_role == "approval_contact"


def test_known_client_wins_over_billing_keywords():
    # subject that would otherwise trip the bill classifier — a signed client
    # emailing about their invoice is a customer-service issue, not a bill to us
    email = _mk_email(
        "<client-email>@example.com", "Question about my invoice", "invoice 1234 $3550"
    )
    with patch("backend.crm.client_directory.lookup_client", return_value=_KERRY):
        cls = classify(email)
    assert cls.category == "client_correspondence"


def test_known_client_wins_over_calendar_keywords():
    email = _mk_email("<client-email>@example.com", "Rescheduling our meeting")
    with patch("backend.crm.client_directory.lookup_client", return_value=_KERRY):
        cls = classify(email)
    assert cls.category == "client_correspondence"


def test_unknown_sender_falls_through_to_normal_classifier():
    email = _mk_email("prospect@example.com", "Interested in your services")
    with patch("backend.crm.client_directory.lookup_client", return_value=None):
        cls = classify(email)
    assert cls.category != "client_correspondence"


def test_directory_import_error_falls_through():
    email = _mk_email("<client-email>@example.com", "Hello")
    with patch(
        "backend.crm.client_directory.lookup_client",
        side_effect=RuntimeError("directory broken"),
    ):
        cls = classify(email)
    # Never raises; falls through to the regular classifier
    assert cls.category != "client_correspondence"


def test_classification_dict_includes_client_fields():
    email = _mk_email("<client-email>@example.com")
    with patch("backend.crm.client_directory.lookup_client", return_value=_KERRY):
        cls = classify(email)
    d = cls.to_dict()
    assert d["category"] == "client_correspondence"
    assert d["client_id"] == "sample_school"
    assert d["campaign_id"] == "sample_school_enrollment_2026"
    assert d["client_role"] == "approval_contact"


# --- outbound (operator-forwarded) branch ------------------------------------

_FWD_BODY_TO_KERRY = """\
Archiving my reply to Kerry.

---------- Forwarded message ---------
From: Alex Hartman <ahartman@hustleforge.tech>
Date: Thu, Jul 10, 2026 at 3:45 PM
Subject: Re: Enrollment Operations
To: Kerry Brown <<client-email>@example.com>

Hi Pastor, thank you for your honesty...
"""


def test_operator_forward_to_known_client_is_outbound():
    email = _mk_email(
        "ahartman@hustleforge.tech",
        subject="Fwd: Re: Enrollment Operations",
        body=_FWD_BODY_TO_KERRY,
    )
    with (
        patch(
            "backend.crm.client_directory.is_operator_address",
            return_value=True,
        ),
        patch(
            "backend.crm.client_directory.lookup_client",
            side_effect=lambda a: _KERRY if a.lower() == "<client-email>@example.com" else None,
        ),
    ):
        cls = classify(email)
    assert cls.category == "client_correspondence"
    assert cls.direction == "outbound"
    assert cls.client_id == "sample_school"
    assert cls.original_to == "<client-email>@example.com"
    assert cls.original_subject == "Re: Enrollment Operations"


def test_operator_forward_to_unknown_recipient_falls_through():
    body = """\
---------- Forwarded message ---------
From: Alex <ahartman@hustleforge.tech>
To: some-vendor@example.com
Subject: Invoice

body...
"""
    email = _mk_email(
        "ahartman@hustleforge.tech",
        subject="Fwd: Invoice",
        body=body,
    )
    with (
        patch(
            "backend.crm.client_directory.is_operator_address",
            return_value=True,
        ),
        patch(
            "backend.crm.client_directory.lookup_client",
            return_value=None,
        ),
    ):
        cls = classify(email)
    assert cls.category != "client_correspondence"


def test_operator_non_forward_email_does_not_trip_outbound_path():
    email = _mk_email(
        "ahartman@hustleforge.tech",
        subject="ops thoughts",
        body="just a note to myself, no forward preamble",
    )
    with (
        patch(
            "backend.crm.client_directory.is_operator_address",
            return_value=True,
        ),
        patch(
            "backend.crm.client_directory.lookup_client",
            return_value=None,
        ),
    ):
        cls = classify(email)
    assert cls.category != "client_correspondence"


def test_non_operator_sender_does_not_use_outbound_path_even_with_forward():
    """A stranger's forward that mentions Kerry still routes to Conquerors
    but as INBOUND (content_match), never as OUTBOUND. Only operator-
    from-address emails take the outbound branch."""
    email = _mk_email(
        "stranger@example.com",
        subject="Fwd: something",
        body=_FWD_BODY_TO_KERRY,
    )
    with (
        patch(
            "backend.crm.client_directory.is_operator_address",
            return_value=False,
        ),
        patch(
            "backend.crm.client_directory.lookup_client",
            return_value=None,
        ),
        patch(
            "backend.crm.client_directory.find_client_in_text",
            return_value=_KERRY,
        ),
    ):
        cls = classify(email)
    # Content-based inbound match — never outbound from a stranger
    assert cls.category == "client_correspondence"
    assert cls.direction == "inbound"
    assert "content_match" in cls.tags


# --- content-based association -----------------------------------------------


def test_operator_forward_to_unknown_recipient_but_body_mentions_client():
    """The 'Back to School Brigade' scenario: operator forwarded an
    outbound email whose direct To: is a third party (Beale AFB) but
    the content is clearly about Conquerors."""
    body = (
        "<div>HustleForge Marketing &amp; Technology Partner for Conquerors "
        "Christian School</div>"
        "<div>---------- Forwarded message --------- "
        "From: Alex &lt;ahartman@hustleforge.tech&gt; "
        "Subject: Sample School – Back to School Brigade "
        "Date: Jul 10 2026, at 8:20 am "
        "To: <recipient>@example.com, contact@sample-school.example "
        "Good afternoon Ms. Goodly, My name is Alex Hartman, Founder of "
        "HustleForge, and I am assisting Sample School..."
        "</div>"
    )
    email = _mk_email(
        "ahartman@hustleforge.tech",
        subject="Fwd: Sample School – Back to School Brigade",
        body=body,
    )
    with (
        patch("backend.crm.client_directory.is_operator_address", return_value=True),
        # Direct recipient not a client
        patch(
            "backend.crm.client_directory.lookup_client",
            return_value=None,
        ),
        # But content detection finds Conquerors
        patch(
            "backend.crm.client_directory.find_client_in_text",
            return_value=_KERRY,
        ),
    ):
        cls = classify(email)
    assert cls.category == "client_correspondence"
    assert cls.direction == "outbound"
    assert cls.client_id == "sample_school"


def test_inbound_third_party_email_mentioning_known_client():
    """A vendor emails us about a client — the from-address isn't a
    client contact but the body is clearly about Conquerors."""
    email = _mk_email(
        "sales@some-vendor.example",
        subject="Proposal for Sample School security upgrade",
        body="Hi Alex, per your inquiry about Sample School...",
    )
    with (
        patch("backend.crm.client_directory.is_operator_address", return_value=False),
        patch("backend.crm.client_directory.lookup_client", return_value=None),
        patch(
            "backend.crm.client_directory.find_client_in_text",
            return_value=_KERRY,
        ),
    ):
        cls = classify(email)
    assert cls.category == "client_correspondence"
    assert cls.direction == "inbound"
    assert cls.confidence < 1.0  # content match is less certain than address match
    assert "content_match" in cls.tags


def test_operator_forward_recipient_in_all_to_addrs_but_not_first():
    """The direct To: is a stranger but a KNOWN client is CC'd/second To:."""
    from backend.crm.client_directory import KnownClient

    kc = _KERRY
    other = KnownClient(
        email="contact@sample-school.example",
        client_id="sample_school",
        campaign_id="sample_school_phased_2026",
        template_id="school_phased_maintenance",
        role="liaison",
        display_name="Frank South",
        docuseal_slug="",
    )
    body = (
        "<div>---------- Forwarded message --------- "
        "From: Alex &lt;ahartman@hustleforge.tech&gt; "
        "Subject: Coordination "
        "To: <recipient>@example.com, contact@sample-school.example "
        "Hi Ms. Goodly...</div>"
    )
    email = _mk_email(
        "ahartman@hustleforge.tech",
        subject="Fwd: Coordination",
        body=body,
    )

    def _lookup(addr):
        if addr.lower() == "contact@sample-school.example":
            return other
        return None

    with (
        patch("backend.crm.client_directory.is_operator_address", return_value=True),
        patch("backend.crm.client_directory.lookup_client", side_effect=_lookup),
    ):
        cls = classify(email)
    assert cls.category == "client_correspondence"
    assert cls.direction == "outbound"
    assert cls.client_id == "sample_school"


def test_outbound_classification_dict_marks_direction():
    email = _mk_email(
        "ahartman@hustleforge.tech",
        subject="Fwd: Re: Enrollment Operations",
        body=_FWD_BODY_TO_KERRY,
    )
    with (
        patch(
            "backend.crm.client_directory.is_operator_address",
            return_value=True,
        ),
        patch(
            "backend.crm.client_directory.lookup_client",
            side_effect=lambda a: _KERRY if a.lower() == "<client-email>@example.com" else None,
        ),
    ):
        cls = classify(email)
    d = cls.to_dict()
    assert d["direction"] == "outbound"
    assert d["original_to"] == "<client-email>@example.com"
    assert d["original_subject"] == "Re: Enrollment Operations"
