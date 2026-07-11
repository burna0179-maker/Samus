"""Phase 5 — proposal workcell dispatches artifact registration to samus-crm.

When ``generate_proposal`` returns and the request carries either an
``opportunity_id`` or a ``prospect_id``, the service fires a best-effort
POST /crm/artifacts dispatch. The dispatch is guarded so a CRM outage or
mis-config never breaks proposal generation.
"""

from __future__ import annotations


def _reset_idempotency(monkeypatch):
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    fresh = IdempotencyStore()
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    import backend.proposal.service as svc_mod

    monkeypatch.setattr(svc_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)


def _stub_settings(monkeypatch, *, gateway_url="http://gateway.local", shared_hmac_key="secret"):
    """Producers dispatch via the gateway -> SQS path, so they need a
    gateway URL in settings.gateway_urls (not a direct crm URL)."""

    class _S:
        gateway_urls = {"gateway": gateway_url}

    s = _S()
    s.shared_hmac_key = shared_hmac_key
    import backend.proposal.service as svc

    monkeypatch.setattr(svc, "get_settings", lambda: s)


def _capture_dispatches(monkeypatch):
    captured: list[dict] = []
    import backend.proposal.service as svc

    monkeypatch.setattr(svc, "_dispatch_artifact_to_crm", lambda payload: captured.append(payload))
    return captured


def _intake():
    from backend.proposal.models import OnboardingIntake

    return OnboardingIntake(
        client_name="Acme",
        business_goal="route leads to CRM",
        triggers_wanted=["form_submitted"],
        actions_wanted=["create_contact"],
        notifications_wanted=["slack_message"],
    )


def test_generate_proposal_with_opportunity_id_fires_dispatch(
    tmp_path,
    monkeypatch,
):
    _reset_idempotency(monkeypatch)
    _stub_settings(monkeypatch)
    monkeypatch.setenv("SAMUS_PROPOSAL_AUDIT_PATH", str(tmp_path / "p.jsonl"))
    captured = _capture_dispatches(monkeypatch)

    from backend.proposal.models import ProposalRequest
    from backend.proposal.service import generate_proposal

    result = generate_proposal(
        ProposalRequest(
            task_id="t-opp",
            intake=_intake(),
            opportunity_id="op_acme_42",
        )
    )
    assert result.status == "approved"
    assert len(captured) == 1
    payload = captured[0]
    assert payload["kind"] == "proposal"
    assert payload["owner_entity_kind"] == "opportunity"
    assert payload["owner_entity_id"] == "op_acme_42"
    assert payload["source"] == "proposal"
    assert payload["created_by"] == "samus-proposal"
    assert payload["inline_data"]["task_id"] == "t-opp"


def test_generate_proposal_with_prospect_only_uses_prospect(
    tmp_path,
    monkeypatch,
):
    _reset_idempotency(monkeypatch)
    _stub_settings(monkeypatch)
    monkeypatch.setenv("SAMUS_PROPOSAL_AUDIT_PATH", str(tmp_path / "p.jsonl"))
    captured = _capture_dispatches(monkeypatch)

    from backend.proposal.models import ProposalRequest
    from backend.proposal.service import generate_proposal

    generate_proposal(
        ProposalRequest(
            task_id="t-pr",
            intake=_intake(),
            prospect_id="pr_acme",
        )
    )
    assert len(captured) == 1
    assert captured[0]["owner_entity_kind"] == "prospect"
    assert captured[0]["owner_entity_id"] == "pr_acme"


def test_generate_proposal_without_crm_linkage_skips_dispatch(
    tmp_path,
    monkeypatch,
):
    _reset_idempotency(monkeypatch)
    _stub_settings(monkeypatch)
    monkeypatch.setenv("SAMUS_PROPOSAL_AUDIT_PATH", str(tmp_path / "p.jsonl"))
    captured = _capture_dispatches(monkeypatch)

    from backend.proposal.models import ProposalRequest
    from backend.proposal.service import generate_proposal

    # Neither opportunity_id nor prospect_id -> bare proposal, no dispatch.
    generate_proposal(ProposalRequest(task_id="t-bare", intake=_intake()))
    assert captured == []


def test_dispatch_helper_skips_when_gateway_url_unset(tmp_path, monkeypatch):
    """No gateway URL -> helper short-circuits without dispatching."""
    _reset_idempotency(monkeypatch)
    _stub_settings(monkeypatch, gateway_url="", shared_hmac_key="secret")
    monkeypatch.setenv("SAMUS_PROPOSAL_AUDIT_PATH", str(tmp_path / "p.jsonl"))

    fired: list = []
    import backend.proposal.service as svc

    monkeypatch.setattr(svc, "signed_post_json_sync", lambda *a, **kw: fired.append((a, kw)))

    from backend.proposal.models import ProposalRequest
    from backend.proposal.service import generate_proposal

    generate_proposal(
        ProposalRequest(
            task_id="t-no-url",
            intake=_intake(),
            opportunity_id="op_x",
        )
    )
    assert fired == []  # helper short-circuited; no enqueue


def test_dispatch_failure_does_not_break_generate(tmp_path, monkeypatch):
    """signed_post_json_sync raising must not bubble up out of generate."""
    _reset_idempotency(monkeypatch)
    _stub_settings(monkeypatch)
    monkeypatch.setenv("SAMUS_PROPOSAL_AUDIT_PATH", str(tmp_path / "p.jsonl"))

    import backend.proposal.service as svc

    def _raising(*args, **kwargs):
        raise RuntimeError("simulated gateway outage")

    monkeypatch.setattr(svc, "signed_post_json_sync", _raising)

    from backend.proposal.models import ProposalRequest
    from backend.proposal.service import generate_proposal

    result = generate_proposal(
        ProposalRequest(
            task_id="t-fail",
            intake=_intake(),
            opportunity_id="op_x",
        )
    )
    assert result.status == "approved"  # producer unaffected by CRM hiccup
