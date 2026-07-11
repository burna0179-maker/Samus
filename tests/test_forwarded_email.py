"""Tests for backend.intake.forwarded_email — Gmail forward preamble parser."""

from __future__ import annotations

from backend.intake.forwarded_email import parse_forwarded_body, strip_html


_GMAIL_FWD = """\
Hi Samus - archiving my reply below.

---------- Forwarded message ---------
From: Alex Hartman <ahartman@hustleforge.tech>
Date: Thu, Jul 10, 2026 at 3:45 PM
Subject: Re: Enrollment Operations
To: Kerry Brown <<client-email>@example.com>


Hi Pastor,

Thank you for your honesty, and I completely understand.
"""


def test_parse_gmail_forward_extracts_original_headers():
    r = parse_forwarded_body(_GMAIL_FWD)
    assert r is not None
    assert r.from_addr == "ahartman@hustleforge.tech"
    assert r.to_addr == "<client-email>@example.com"
    assert r.subject == "Re: Enrollment Operations"
    assert r.date.startswith("Thu, Jul 10, 2026")
    assert r.has_recipient() is True


def test_parse_returns_none_for_non_forwarded_body():
    assert parse_forwarded_body("Just a regular email body with no preamble.") is None
    assert parse_forwarded_body("") is None


def test_parse_returns_none_for_empty_body():
    assert parse_forwarded_body("") is None


def test_parse_handles_bare_addresses():
    body = """\
FYI:

---------- Forwarded message ---------
From: ahartman@hustleforge.tech
Date: Yesterday
Subject: Follow-up
To: <client-email>@example.com

Body content
"""
    r = parse_forwarded_body(body)
    assert r is not None
    assert r.from_addr == "ahartman@hustleforge.tech"
    assert r.to_addr == "<client-email>@example.com"


def test_parse_stops_at_blank_line_after_headers():
    body = """\
---------- Forwarded message ---------
From: alex@example.com
To: kerry@example.com
Subject: hello

To: someone-else@example.com in the body should NOT overwrite
"""
    r = parse_forwarded_body(body)
    assert r is not None
    assert r.to_addr == "kerry@example.com"


def test_parse_uses_first_valid_email_in_from_line():
    body = """\
---------- Forwarded message ---------
From: "Alex, the operator" <ahartman@hustleforge.tech>
To: <<client-email>@example.com>
Subject: Re: whatever

Body
"""
    r = parse_forwarded_body(body)
    assert r is not None
    assert r.from_addr == "ahartman@hustleforge.tech"
    assert r.to_addr == "<client-email>@example.com"


# --- HTML-heavy Titan forwards -------------------------------------------


def test_parse_handles_titan_html_forward():
    """Titan wraps the whole forwarded email in HTML — the stripped
    result is a single run-on line. parse_forwarded_body must strip
    HTML and use inline field extractors (not line-anchored regex)."""
    body = (
        "<div><span>Alex Hartman Founder & Principal Consultant HustleForge</span></div>"
        "<div>---------- Forwarded message --------- "
        "From: Alex &lt;ahartman@hustleforge.tech&gt; "
        "Subject: Sample School – Back to School Brigade "
        "Date: Jul 10 2026, at 8:20 am "
        "To: <recipient>@example.com, contact@sample-school.example "
        "Good afternoon Ms. Goodly, My name is Alex Hartman..."
        "</div>"
    )
    r = parse_forwarded_body(body)
    assert r is not None
    assert r.from_addr == "ahartman@hustleforge.tech"
    assert r.to_addr == "<recipient>@example.com"
    # BOTH recipients captured
    assert "<recipient>@example.com" in r.all_to_addrs
    assert "contact@sample-school.example" in r.all_to_addrs
    assert "Sample School" in r.subject


def test_parse_titan_reply_to_known_client():
    """Titan HTML forward of the outbound reply to Kerry."""
    body = (
        "<div>HustleForge Marketing &amp; Technology Partner</div>"
        "<div>---------- Forwarded message --------- "
        "From: Alex &lt;ahartman@hustleforge.tech&gt; "
        "Subject: Enrollment Rush "
        "Date: Jul 10 2026, at 8:38 am "
        "To: <client-email>@example.com "
        "Hi Pastor, Thank you for your honesty..."
        "</div>"
    )
    r = parse_forwarded_body(body)
    assert r is not None
    assert r.to_addr == "<client-email>@example.com"
    assert r.all_to_addrs == ("<client-email>@example.com",)


# --- strip_html ----------------------------------------------------------


def test_strip_html_removes_tags_and_entities():
    got = strip_html("<div>Hello &amp; goodbye <span>world</span></div>")
    assert got == "Hello & goodbye world"


def test_strip_html_drops_script_and_style_blocks_entirely():
    got = strip_html(
        '<div>keep</div><script>alert("no")</script><style>a{color:red}</style><div>this</div>'
    )
    assert "alert" not in got
    assert "color:red" not in got
    assert "keep" in got
    assert "this" in got


def test_strip_html_returns_empty_on_empty():
    assert strip_html("") == ""
    assert strip_html(None) == ""  # type: ignore[arg-type]


def test_strip_html_leaves_plain_text_alone():
    assert strip_html("plain content") == "plain content"
