"""CRM Pydantic model validation."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.crm.models import (
    Artifact,
    CallState,
    Contact,
    Conversation,
    ConvertLeadRequest,
    OperatorTask,
    Opportunity,
    Prospect,
)


def test_prospect_subclasses_prospecting_record():
    """Prospect subclasses ProspectRecord so the 32-field shape is inherited
    intact, but with extra='ignore' so reads of legacy DDB rows that carry
    pre-iteration fields (campaign_name, etc.) work without raising."""
    from backend.prospecting.models import ProspectRecord
    assert issubclass(Prospect, ProspectRecord)
    # Reading a legacy row with truly-extra fields must NOT raise:
    p = Prospect.model_validate({
        "prospect_id": "pr_x",
        "company_name": "Acme",
        "campaign_name": "local-marysville",   # legacy, not on ProspectRecord
        "legacy_priority_score": 0.85,          # hypothetical legacy field
    })
    assert p.prospect_id == "pr_x"
    assert p.company_name == "Acme"


def test_prospect_record_strict_path_unchanged():
    """The CSV-export path still uses strict ProspectRecord with extra=forbid.
    Subclassing must NOT have leaked the relaxed config back to the parent."""
    import pytest
    from pydantic import ValidationError
    from backend.prospecting.models import ProspectRecord
    with pytest.raises(ValidationError):
        ProspectRecord.model_validate({
            "prospect_id": "pr_x", "company_name": "Acme",
            "campaign_name": "local-marysville",   # extra on parent -> reject
        })


def test_contact_minimum_field():
    """Only contact_id is required; everything else has a sensible default."""
    c = Contact(contact_id="co_xyz")
    assert c.contact_id == "co_xyz"
    assert c.preferred_channel == "email"     # default
    assert c.do_not_contact is False
    assert c.email == ""


def test_contact_preferred_channel_literal_enforced():
    with pytest.raises(ValidationError):
        Contact(contact_id="co_xyz", preferred_channel="carrier_pigeon")


def test_conversation_status_literal_enforced():
    with pytest.raises(ValidationError):
        Conversation(conversation_id="cv_x", status="mid_air")


def test_call_state_literal_values():
    s = CallState(prospect_id="pr_x", state="dialing", attempt_count=2)
    assert s.state == "dialing"
    assert s.attempt_count == 2
    with pytest.raises(ValidationError):
        CallState(prospect_id="pr_x", state="hovering")


def test_opportunity_stage_literal_enforced():
    op = Opportunity(opportunity_id="op_x", stage="proposal")
    assert op.stage == "proposal"
    with pytest.raises(ValidationError):
        Opportunity(opportunity_id="op_x", stage="thinking_about_it")


def test_opportunity_close_probability_unconstrained_for_now():
    """We accept any float; business validation lives in service layer."""
    op = Opportunity(opportunity_id="op_x", close_probability=1.5)
    assert op.close_probability == 1.5


def test_operator_task_kind_and_status_literals():
    t = OperatorTask(operator_task_id="ot_x", kind="follow_up", status="open")
    assert t.kind == "follow_up"
    with pytest.raises(ValidationError):
        OperatorTask(operator_task_id="ot_x", kind="meditate")
    with pytest.raises(ValidationError):
        OperatorTask(operator_task_id="ot_x", status="purgatory")


def test_artifact_kind_literal_enforced():
    a = Artifact(artifact_id="ar_x", kind="proposal")
    assert a.kind == "proposal"
    with pytest.raises(ValidationError):
        Artifact(artifact_id="ar_x", kind="vibes")


def test_convert_lead_request_requires_lead_id():
    with pytest.raises(ValidationError):
        ConvertLeadRequest(lead_id="")
    req = ConvertLeadRequest(lead_id="lead_abc", assigned_to="ops@x.com")
    assert req.lead_id == "lead_abc"
    assert req.assigned_to == "ops@x.com"


def test_convert_lead_request_rejects_extras():
    """extra='forbid' so misspelled fields surface immediately."""
    with pytest.raises(ValidationError):
        ConvertLeadRequest.model_validate({"lead_id": "x", "asignee": "ops@x.com"})
