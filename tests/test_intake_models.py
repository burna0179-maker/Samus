"""OnboardingLeadRequest validation — mirrors the HTML form contract."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.intake.models import OnboardingLeadRequest


def _valid_payload(**overrides):
    base = {
        "name": "Jane Smith",
        "email": "jane@yourcompany.com",
        "company": "Acme",
        "website_url": "https://acme.com",
        "service_interest": ["seo_audit", "workflow_rescue"],
        "pain_points": "Our follow-up system is broken.",
        "monthly_budget": "$500-$2000",
        "timeline": "this_month",
    }
    base.update(overrides)
    return base


def test_round_trip_full_payload():
    req = OnboardingLeadRequest.model_validate(_valid_payload())
    assert req.email == "jane@yourcompany.com"
    assert req.service_interest == ["seo_audit", "workflow_rescue"]
    assert req.monthly_budget == "$500-$2000"


def test_optional_company_and_website_default_blank():
    req = OnboardingLeadRequest.model_validate(_valid_payload(company="", website_url=""))
    assert req.company == ""
    assert req.website_url == ""


def test_empty_budget_and_timeline_allowed():
    """The HTML form's placeholder option uses value='' — must round-trip."""
    req = OnboardingLeadRequest.model_validate(_valid_payload(monthly_budget="", timeline=""))
    assert req.monthly_budget == ""
    assert req.timeline == ""


def test_all_eight_service_values_validate():
    """If the HTML form adds/removes a value, this test must change in lockstep."""
    eight = [
        "seo_audit",
        "seo_implementation",
        "seo_optimization",
        "workflow_rescue",
        "workflow_buildout",
        "ai_ops_partner",
        "playbook",
        "not_sure",
    ]
    req = OnboardingLeadRequest.model_validate(_valid_payload(service_interest=eight))
    assert len(req.service_interest) == 8


def test_unknown_service_value_rejected():
    with pytest.raises(ValidationError):
        OnboardingLeadRequest.model_validate(_valid_payload(service_interest=["bogus"]))


def test_unknown_budget_value_rejected():
    with pytest.raises(ValidationError):
        OnboardingLeadRequest.model_validate(_valid_payload(monthly_budget="$10000+"))


def test_unknown_timeline_rejected():
    with pytest.raises(ValidationError):
        OnboardingLeadRequest.model_validate(_valid_payload(timeline="someday"))


def test_email_required_and_validated():
    with pytest.raises(ValidationError):
        OnboardingLeadRequest.model_validate(_valid_payload(email="not-an-email"))
    with pytest.raises(ValidationError):
        OnboardingLeadRequest.model_validate(_valid_payload(email=""))


def test_pain_points_required():
    with pytest.raises(ValidationError):
        OnboardingLeadRequest.model_validate(_valid_payload(pain_points=""))


def test_name_required():
    with pytest.raises(ValidationError):
        OnboardingLeadRequest.model_validate(_valid_payload(name=""))


def test_extra_fields_rejected_to_prevent_smuggling():
    """A spammer can't slip in admin=True via extra JSON keys."""
    with pytest.raises(ValidationError):
        OnboardingLeadRequest.model_validate(_valid_payload(admin=True))


def test_length_caps_enforced():
    too_long_pain = "x" * 2049
    with pytest.raises(ValidationError):
        OnboardingLeadRequest.model_validate(_valid_payload(pain_points=too_long_pain))
    too_long_website = "https://x.com/" + ("a" * 2048)
    with pytest.raises(ValidationError):
        OnboardingLeadRequest.model_validate(_valid_payload(website_url=too_long_website))
