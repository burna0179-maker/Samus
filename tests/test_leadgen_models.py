"""Tests for backend.leadgen.models."""
from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_lead_request_strips_strings():
    from backend.leadgen.models import LeadRequest

    req = LeadRequest(
        company="  Acme Co  ",
        domain="  acme.com  ",
        industry="  finance  ",
        employee_count=50,
        annual_revenue_usd=2_500_000,
        geo="  US  ",
        signals=["manual_ops"],
    )
    assert req.company == "Acme Co"
    assert req.domain == "acme.com"
    assert req.industry == "finance"
    assert req.geo == "US"


def test_lead_request_validates_bounds():
    from backend.leadgen.models import LeadRequest

    with pytest.raises(ValidationError):
        LeadRequest(
            company="X",
            domain="a.com",
            industry="finance",
            employee_count=0,
            annual_revenue_usd=0,
            geo="US",
        )

    with pytest.raises(ValidationError):
        LeadRequest(
            company="OK Co",
            domain="x",
            industry="finance",
            employee_count=10,
            annual_revenue_usd=1,
            geo="US",
        )


def test_lead_score_accepts_valid_payload():
    from backend.leadgen.models import LeadScore

    score = LeadScore(
        company="Acme",
        normalized_domain="acme.com",
        segment="smb",
        score=72,
        tier="high",
        matched_signals=["manual_ops"],
        reasons=["industry=finance"],
        recommendations=["Multi-thread outreach"],
    )
    assert score.tier == "high"
    assert score.segment == "smb"
    assert 0 <= score.score <= 100


def test_lead_score_rejects_bad_tier():
    from backend.leadgen.models import LeadScore

    with pytest.raises(ValidationError):
        LeadScore(
            company="Acme",
            normalized_domain="acme.com",
            segment="smb",
            score=10,
            tier="impossible",  # type: ignore[arg-type]
        )
