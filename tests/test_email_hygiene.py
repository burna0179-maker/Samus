"""Tests for backend.outreach.email_hygiene — garbage-recipient guard."""

from __future__ import annotations

import pytest

from backend.outreach.email_hygiene import is_bad_email, is_good_email


@pytest.mark.parametrize(
    "bad",
    [
        "logo-dark@2x.png",  # image filename (real scrape leak)
        "ima-quickquote-car-lg@2x.webp",  # image filename
        "_@astro.cysg4tlv.css",  # css asset
        "bundle@app.js",  # js asset
        "user@domain.com",  # placeholder
        "test@test.com",
        "example@example.com",
        "support@webador.com",  # website-builder support, not the business
        "",  # empty
        "noatsign",  # no @
        "a@b",  # no dot in domain
        "a@@b.com",  # double @
        "has space@x.com",  # whitespace
        "x@y.123",  # numeric tld
    ],
)
def test_rejects_garbage(bad):
    assert is_bad_email(bad) is True


@pytest.mark.parametrize(
    "good",
    [
        "kary@sapphiremarketinggroup.net",
        "help@adept-solutions.net",
        "info@alliantnetworking.com",
        "joshua@snellingbkkg.com",
        "riverahvac1@yahoo.com",
        "john@gmail.com",  # valid FORMAT (owner-mismatch is separate)
        "owner@some-local-biz.co",
    ],
)
def test_accepts_real_business_emails(good):
    assert is_bad_email(good) is False
    assert is_good_email(good) is True
