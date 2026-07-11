"""Smoke tests for the proposal workcell — pipeline + service."""
from __future__ import annotations


def _reset_idempotency(monkeypatch):
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod
    fresh = IdempotencyStore()
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    import backend.proposal.service as svc_mod
    monkeypatch.setattr(svc_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)


def test_registry_has_six_canonical_templates():
    from backend.proposal.templates import TEMPLATE_REGISTRY
    expected = {"slack_notify", "email_send", "gsheet_write",
                "form_trigger", "webhook_in", "crm_write"}
    assert expected.issubset(TEMPLATE_REGISTRY.keys())


def test_plan_dedupes_overlapping_wants():
    from backend.proposal.models import OnboardingIntake
    from backend.proposal.pipeline import plan_workflow
    intake = OnboardingIntake(
        client_name="Acme", business_goal="g",
        triggers_wanted=["form_submitted", "form_submitted"],
        actions_wanted=["send_email", "create_contact"],
        notifications_wanted=["slack_message"],
    )
    plan = plan_workflow(intake)
    assert plan.triggers == ["form_submitted"]
    assert plan.actions == ["send_email", "create_contact"]


def test_service_happy_path(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_PROPOSAL_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.proposal.models import OnboardingIntake, ProposalRequest
    from backend.proposal.service import generate_proposal

    result = generate_proposal(ProposalRequest(
        task_id="t1",
        intake=OnboardingIntake(
            client_name="Acme", business_goal="route leads to CRM",
            triggers_wanted=["form_submitted"],
            actions_wanted=["create_contact"],
            notifications_wanted=["slack_message"],
        ),
    ))
    assert result.status == "approved"
    assert result.stage.value == "delivered"
    assert result.refund_protocol is False
    assert result.workflow is not None
    assert result.workflow.total_steps == 3


def test_service_overflow_refunds(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_PROPOSAL_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.proposal.models import OnboardingIntake, ProposalRequest
    from backend.proposal.service import generate_proposal

    result = generate_proposal(ProposalRequest(
        task_id="t-over",
        intake=OnboardingIntake(
            client_name="Acme", business_goal="too much",
            triggers_wanted=["form_submitted", "webhook"],
            actions_wanted=["send_email", "create_contact", "append_row"],
            notifications_wanted=["slack_message", "team_alert", "channel_post"],
            tools_available=["slack", "ses", "hubspot", "google_sheets"],
        ),
    ))
    assert result.status == "out_of_scope"
    assert result.refund_protocol is True
