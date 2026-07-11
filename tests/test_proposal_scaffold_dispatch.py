"""Inbound deal funnel — Unit S (proposal side): an approved proposal dispatches
a proposal-pack render job to the scaffold workcell.

``generate_proposal`` fires ``_dispatch_proposal_pack_to_scaffold`` only when the
result is ``approved`` (DELIVERED) — a needs_review skeleton or an out_of_scope
proposal has nothing worth packaging. The dispatch is a best-effort
``generate_assets`` TaskEnvelope at the gateway's ``/dispatch/scaffold``;
``signed_post_json_sync`` is stubbed so every test stays offline.
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
    class _S:
        gateway_urls = {"gateway": gateway_url} if gateway_url else {}

    s = _S()
    s.shared_hmac_key = shared_hmac_key
    import backend.proposal.service as svc

    monkeypatch.setattr(svc, "get_settings", lambda: s)


class _Resp:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = ""


def _capture_posts(monkeypatch):
    """Capture every signed_post_json_sync the proposal service makes."""
    posts: list[tuple] = []
    import backend.proposal.service as svc

    def _fake(base_url, path, payload, **kw):
        posts.append((base_url, path, payload))
        return _Resp(200)

    monkeypatch.setattr(svc, "signed_post_json_sync", _fake)
    return posts


def _approved_intake():
    """An intake whose three wants all resolve to TEMPLATE_REGISTRY templates,
    so the compiled workflow validates -> status='approved'."""
    from backend.proposal.models import OnboardingIntake

    return OnboardingIntake(
        client_name="Acme HVAC",
        business_goal="route inbound leads into the CRM automatically",
        triggers_wanted=["form_submitted"],
        actions_wanted=["create_contact"],
        notifications_wanted=["slack_message"],
    )


def _empty_intake():
    """No wants -> empty workflow -> validation fails 'empty_workflow' ->
    status='needs_review' (the Unit P auto-draft path)."""
    from backend.proposal.models import OnboardingIntake

    return OnboardingIntake(
        client_name="Bare Co",
        business_goal="operator to detail the automation scope",
    )


def _scaffold_posts(posts):
    return [p for p in posts if p[1] == "/dispatch/scaffold"]


def test_approved_proposal_dispatches_proposal_pack(tmp_path, monkeypatch):
    """An approved proposal fires exactly one /dispatch/scaffold envelope."""
    _reset_idempotency(monkeypatch)
    _stub_settings(monkeypatch)
    monkeypatch.setenv("SAMUS_PROPOSAL_AUDIT_PATH", str(tmp_path / "p.jsonl"))
    posts = _capture_posts(monkeypatch)

    from backend.proposal.models import ProposalRequest
    from backend.proposal.service import generate_proposal

    result = generate_proposal(
        ProposalRequest(
            task_id="t-appr",
            intake=_approved_intake(),
        )
    )
    assert result.status == "approved"

    scaffold = _scaffold_posts(posts)
    assert len(scaffold) == 1
    base, path, envelope = scaffold[0]
    assert base == "http://gateway.local"
    assert envelope["metadata"]["action"] == "generate_assets"
    body = envelope["payload"]
    assert body["asset_type"] == "proposal_pack"
    assert body["client"] == "Acme HVAC"
    assert body["title"] == "Proposal Pack: Acme HVAC"
    # The compiled workflow's node descriptions seeded the goal list.
    assert body["goals"]
    assert body["inputs"]["proposal_task_id"] == "t-appr"


def test_needs_review_proposal_does_not_dispatch_scaffold(tmp_path, monkeypatch):
    """A needs_review skeleton (empty workflow) has nothing to package."""
    _reset_idempotency(monkeypatch)
    _stub_settings(monkeypatch)
    monkeypatch.setenv("SAMUS_PROPOSAL_AUDIT_PATH", str(tmp_path / "p.jsonl"))
    posts = _capture_posts(monkeypatch)

    from backend.proposal.models import ProposalRequest
    from backend.proposal.service import generate_proposal

    result = generate_proposal(
        ProposalRequest(
            task_id="t-skel",
            intake=_empty_intake(),
        )
    )
    assert result.status == "needs_review"
    assert _scaffold_posts(posts) == []


def test_scaffold_payload_validates_as_scaffold_request(tmp_path, monkeypatch):
    """The dispatched payload round-trips through scaffold's ScaffoldRequest."""
    _reset_idempotency(monkeypatch)
    _stub_settings(monkeypatch)
    monkeypatch.setenv("SAMUS_PROPOSAL_AUDIT_PATH", str(tmp_path / "p.jsonl"))
    posts = _capture_posts(monkeypatch)

    from backend.proposal.models import ProposalRequest
    from backend.proposal.service import generate_proposal

    generate_proposal(
        ProposalRequest(
            task_id="t-val",
            intake=_approved_intake(),
            opportunity_id="op_acme",
        )
    )
    body = _scaffold_posts(posts)[0][2]["payload"]

    from backend.scaffold.models import ScaffoldRequest

    sr = ScaffoldRequest.model_validate(body)
    assert sr.asset_type == "proposal_pack"
    assert sr.inputs["opportunity_id"] == "op_acme"


def test_scaffold_dispatch_skipped_when_gateway_unset(tmp_path, monkeypatch):
    """No gateway URL -> the scaffold dispatch short-circuits."""
    _reset_idempotency(monkeypatch)
    _stub_settings(monkeypatch, gateway_url="")
    monkeypatch.setenv("SAMUS_PROPOSAL_AUDIT_PATH", str(tmp_path / "p.jsonl"))
    posts = _capture_posts(monkeypatch)

    from backend.proposal.models import ProposalRequest
    from backend.proposal.service import generate_proposal

    result = generate_proposal(
        ProposalRequest(
            task_id="t-nourl",
            intake=_approved_intake(),
        )
    )
    assert result.status == "approved"
    assert posts == []


def test_scaffold_dispatch_failure_does_not_break_generate(tmp_path, monkeypatch):
    """signed_post_json_sync raising must not bubble out of generate_proposal."""
    _reset_idempotency(monkeypatch)
    _stub_settings(monkeypatch)
    monkeypatch.setenv("SAMUS_PROPOSAL_AUDIT_PATH", str(tmp_path / "p.jsonl"))

    import backend.proposal.service as svc

    def _raising(*a, **k):
        raise RuntimeError("simulated gateway outage")

    monkeypatch.setattr(svc, "signed_post_json_sync", _raising)

    from backend.proposal.models import ProposalRequest
    from backend.proposal.service import generate_proposal

    result = generate_proposal(
        ProposalRequest(
            task_id="t-boom",
            intake=_approved_intake(),
        )
    )
    assert result.status == "approved"  # producer unaffected by the outage
