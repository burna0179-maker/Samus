"""Contact-information validation + misdirection reasoning.

``backend.prospecting.contact_validation`` is the deterministic check that a
contact email is structurally deliverable and actually tied to the business —
the guard against a garbled scrape or a gatekeeper's brush-off address. The
motivating real case (2026-05-21): a receptionist offered ``info@magnolia-.com``
for *Magnolia Modern Dentistry* — a domain label may not end in a hyphen.
"""
from __future__ import annotations

from backend.prospecting.contact_validation import (
    assess_email,
    email_syntax_error,
    is_valid_email_syntax,
)


# --- syntactic validity ----------------------------------------------------


def test_well_formed_addresses_are_valid():
    for ok in (
        "info@magnolia-modern.com",          # interior hyphen is fine
        "manager@dentistyubacity.com",
        "first.last@sub.domain.org",
        "a@b.co",
        "owner+tag@example.io",
    ):
        assert is_valid_email_syntax(ok), ok
        assert email_syntax_error(ok) is None


def test_trailing_hyphen_label_is_malformed():
    """The Magnolia case — a label ending in '-' is not a valid hostname."""
    err = email_syntax_error("info@magnolia-.com")
    assert err is not None
    assert "hyphen" in err
    assert is_valid_email_syntax("info@magnolia-.com") is False


def test_leading_hyphen_label_is_malformed():
    assert is_valid_email_syntax("info@-magnolia.com") is False
    assert "hyphen" in (email_syntax_error("info@-magnolia.com") or "")


def test_other_malformed_addresses_are_rejected():
    for bad in (
        "",                       # empty
        "infomagnolia.com",       # no @
        "info@@magnolia.com",     # two @
        "@magnolia.com",          # empty local
        "info@",                  # empty domain
        "info@magnolia",          # no TLD
        "info@magnolia..com",     # doubled dot / empty label
        "info@.com",              # empty leading label
        "info@magnolia.c",        # TLD too short
        ".info@magnolia.com",     # leading dot in local
    ):
        assert is_valid_email_syntax(bad) is False, bad
        assert email_syntax_error(bad) is not None, bad


# --- assess_email: the malformed / misdirection verdict --------------------


def test_assess_flags_the_magnolia_misdirection():
    """info@magnolia-.com offered for Magnolia Modern Dentistry — malformed,
    and recognisably a garble of the real magnolia-modern.com domain."""
    a = assess_email(
        "info@magnolia-.com",
        business_name="Magnolia Modern Dentistry",
        website_url="https://www.dentistyubacity.com/",
        extra_domains=("magnolia-modern.com",),
    )
    assert a.verdict == "malformed"
    assert a.valid_syntax is False
    assert a.is_trustworthy is False
    joined = " ".join(a.reasons).lower()
    assert "hyphen" in joined
    assert "magnolia-modern.com" in joined  # spotted the resemblance


def test_assess_resembles_business_name_without_an_on_file_domain():
    """Even with no on-file domain, a garbled domain is matched to the name."""
    a = assess_email(
        "info@magnolia-.com",
        business_name="Magnolia Modern Dentistry",
        website_url="https://www.dentistyubacity.com/",
    )
    assert a.verdict == "malformed"
    assert any("magnolia modern dentistry" in r.lower() for r in a.reasons)


# --- assess_email: valid / suspect verdicts --------------------------------


def test_assess_domain_matching_website_is_valid():
    a = assess_email(
        "manager@dentistyubacity.com",
        business_name="Magnolia Modern Dentistry",
        website_url="https://www.dentistyubacity.com/",
    )
    assert a.verdict == "valid"
    assert a.is_trustworthy is True


def test_assess_on_file_domain_is_valid():
    a = assess_email(
        "info@magnolia-modern.com",
        business_name="Magnolia Modern Dentistry",
        extra_domains=("magnolia-modern.com",),
    )
    assert a.verdict == "valid"


def test_assess_unrelated_domain_is_suspect():
    a = assess_email(
        "info@randomcorp.com",
        business_name="Magnolia Modern Dentistry",
        website_url="https://dentistyubacity.com",
    )
    assert a.verdict == "suspect"
    assert a.valid_syntax is True            # syntactically fine...
    assert a.is_trustworthy is False         # ...but not verified
    assert "unrelated" in " ".join(a.reasons).lower()


def test_assess_consumer_mailbox_is_valid_but_noted():
    a = assess_email(
        "magnoliadental@gmail.com",
        business_name="Magnolia Modern Dentistry",
        website_url="https://dentistyubacity.com",
    )
    assert a.verdict == "valid"
    assert "consumer" in " ".join(a.reasons).lower()


def test_assess_with_no_business_context_is_syntax_only():
    a = assess_email("info@whatever.com")
    assert a.verdict == "valid"
    assert "no business context" in " ".join(a.reasons).lower()


# --- enrichment integration: malformed scraped emails are dropped ----------


def test_enrichment_drops_malformed_scraped_email():
    """_extract_emails must not store a structurally-impossible address even
    though the permissive extraction regex matches it."""
    from backend.prospecting.enrichment import _extract_emails

    html = (
        '<a href="mailto:manager@dentistyubacity.com">Email us</a>'
        '<p>Or reach the admin at info@magnolia-.com today.</p>'
    )
    emails = _extract_emails(html)
    assert "manager@dentistyubacity.com" in emails
    assert "info@magnolia-.com" not in emails
    assert all(is_valid_email_syntax(e) for e in emails)


# --- CLI: the on-the-spot check Log-Call.ps1 runs --------------------------


def _run_cli(args):
    """Run the contact_validation CLI, return (exit_code, parsed_json)."""
    import contextlib
    import io
    import json

    from backend.prospecting.contact_validation import main

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(args)
    return code, json.loads(buf.getvalue())


def test_cli_flags_a_malformed_contact_nonzero_exit():
    code, out = _run_cli([
        "--email", "info@magnolia-.com", "--company", "Magnolia Modern Dentistry",
    ])
    assert code == 1                       # non-zero so the caller can branch
    assert out["verdict"] == "malformed"
    assert out["valid_syntax"] is False
    assert out["reasons"]


def test_cli_passes_a_valid_matching_contact():
    code, out = _run_cli([
        "--email", "manager@dentistyubacity.com",
        "--website", "https://www.dentistyubacity.com/",
    ])
    assert code == 0
    assert out["verdict"] == "valid"


def test_cli_flags_a_suspect_offdomain_contact():
    code, out = _run_cli([
        "--email", "info@randomcorp.com",
        "--company", "Magnolia Modern Dentistry",
        "--website", "https://dentistyubacity.com",
    ])
    assert code == 1
    assert out["verdict"] == "suspect"
