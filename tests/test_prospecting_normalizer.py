"""Domain + company-name normalization."""

from __future__ import annotations


def test_normalize_domain_strips_scheme_and_www():
    from backend.prospecting.normalizer import normalize_domain

    assert normalize_domain("https://www.example.com/") == "example.com"
    assert normalize_domain("http://Acme.COM") == "acme.com"
    assert normalize_domain("nav-accounts.com") == "nav-accounts.com"


def test_normalize_domain_handles_empty():
    from backend.prospecting.normalizer import normalize_domain

    assert normalize_domain("") == ""
    assert normalize_domain("   ") == ""


def test_normalize_company():
    from backend.prospecting.normalizer import normalize_company

    assert normalize_company("nav accounts") == "Harbor Ledger Accounting"
    assert normalize_company("  acme roofing  ") == "Acme Roofing"
    assert normalize_company("") == ""
