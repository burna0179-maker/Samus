"""Inbound deal funnel — Unit S (scaffold side): a rendered scaffold asset is
registered back into samus-crm as an Artifact.

``generate_scaffold`` fires ``_dispatch_artifact_to_crm`` only when
``ScaffoldRequest.inputs`` carries an owner linkage (``opportunity_id`` or
``prospect_id``) — a standalone sandbox/preview render registers nothing. The
dispatch is a best-effort ``create_artifact`` TaskEnvelope at the gateway's
``/dispatch/crm``; ``signed_post_json_sync`` is stubbed so tests stay offline.
"""

from __future__ import annotations


def _reset_idempotency(monkeypatch):
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod
    import backend.scaffold.logic as logic_mod

    fresh = IdempotencyStore()
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    monkeypatch.setattr(logic_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)


def _stub_settings(monkeypatch, *, gateway_url="http://gateway.local", shared_hmac_key="secret"):
    class _S:
        gateway_urls = {"gateway": gateway_url} if gateway_url else {}

    s = _S()
    s.shared_hmac_key = shared_hmac_key
    import backend.scaffold.logic as logic

    monkeypatch.setattr(logic, "get_settings", lambda: s)


class _Resp:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = ""


def _capture_posts(monkeypatch):
    posts: list[tuple] = []
    import backend.scaffold.logic as logic

    def _fake(base_url, path, payload, **kw):
        posts.append((base_url, path, payload))
        return _Resp(200)

    monkeypatch.setattr(logic, "signed_post_json_sync", _fake)
    return posts


def _request(monkeypatch, **inputs):
    """Build a proposal_pack ScaffoldRequest with the given inputs linkage."""
    from backend.scaffold.models import ScaffoldRequest

    return ScaffoldRequest(
        asset_type="proposal_pack",
        title="Proposal Pack: Acme HVAC",
        client="Acme HVAC",
        brand_voice="professional and direct",
        offer="route inbound leads into the CRM",
        goals=["capture the lead", "create the contact", "alert the team"],
        inputs=inputs,
    )


def test_scaffold_with_opportunity_linkage_registers_artifact(tmp_path, monkeypatch):
    """inputs.opportunity_id -> one create_artifact dispatch, owner=opportunity."""
    _reset_idempotency(monkeypatch)
    _stub_settings(monkeypatch)
    monkeypatch.setenv("SAMUS_SCAFFOLD_AUDIT_PATH", str(tmp_path / "s.jsonl"))
    posts = _capture_posts(monkeypatch)

    from backend.scaffold.logic import generate_scaffold

    generate_scaffold(_request(monkeypatch, opportunity_id="op_acme_7"))

    assert len(posts) == 1
    base, path, envelope = posts[0]
    assert base == "http://gateway.local"
    assert path == "/dispatch/crm"
    assert envelope["metadata"]["action"] == "create_artifact"
    body = envelope["payload"]
    assert body["kind"] == "proposal"  # proposal_pack -> "proposal"
    assert body["owner_entity_kind"] == "opportunity"
    assert body["owner_entity_id"] == "op_acme_7"
    assert body["source"] == "scaffold"
    assert body["inline_data"]["asset_type"] == "proposal_pack"
    assert body["inline_data"]["document"].startswith("# Proposal Pack")


def test_scaffold_with_prospect_only_uses_prospect(tmp_path, monkeypatch):
    """prospect_id alone -> owner=prospect."""
    _reset_idempotency(monkeypatch)
    _stub_settings(monkeypatch)
    monkeypatch.setenv("SAMUS_SCAFFOLD_AUDIT_PATH", str(tmp_path / "s.jsonl"))
    posts = _capture_posts(monkeypatch)

    from backend.scaffold.logic import generate_scaffold

    generate_scaffold(_request(monkeypatch, prospect_id="pr_acme"))

    assert len(posts) == 1
    body = posts[0][2]["payload"]
    assert body["owner_entity_kind"] == "prospect"
    assert body["owner_entity_id"] == "pr_acme"


def test_scaffold_without_linkage_skips_dispatch(tmp_path, monkeypatch):
    """No owner linkage in inputs -> standalone render, nothing registered."""
    _reset_idempotency(monkeypatch)
    _stub_settings(monkeypatch)
    monkeypatch.setenv("SAMUS_SCAFFOLD_AUDIT_PATH", str(tmp_path / "s.jsonl"))
    posts = _capture_posts(monkeypatch)

    from backend.scaffold.logic import generate_scaffold

    payload = generate_scaffold(_request(monkeypatch))  # empty inputs
    assert posts == []
    assert payload["document"]  # the asset still rendered


def test_scaffold_artifact_payload_validates_as_create_artifact_request(
    tmp_path,
    monkeypatch,
):
    """The dispatched payload round-trips through CRM's CreateArtifactRequest
    (extra='forbid', owner fields min_length=1)."""
    _reset_idempotency(monkeypatch)
    _stub_settings(monkeypatch)
    monkeypatch.setenv("SAMUS_SCAFFOLD_AUDIT_PATH", str(tmp_path / "s.jsonl"))
    posts = _capture_posts(monkeypatch)

    from backend.scaffold.logic import generate_scaffold

    generate_scaffold(_request(monkeypatch, opportunity_id="op_v"))

    from backend.crm.models import CreateArtifactRequest

    car = CreateArtifactRequest.model_validate(posts[0][2]["payload"])
    assert car.kind == "proposal"
    assert car.owner_entity_id == "op_v"


def test_scaffold_crm_dispatch_failure_does_not_break_generate(tmp_path, monkeypatch):
    """A transport failure inside the dispatch is swallowed — generate_scaffold
    still returns the rendered asset."""
    _reset_idempotency(monkeypatch)
    _stub_settings(monkeypatch)
    monkeypatch.setenv("SAMUS_SCAFFOLD_AUDIT_PATH", str(tmp_path / "s.jsonl"))

    import backend.scaffold.logic as logic

    def _raising(*a, **k):
        raise RuntimeError("simulated gateway outage")

    monkeypatch.setattr(logic, "signed_post_json_sync", _raising)

    from backend.scaffold.logic import generate_scaffold

    payload = generate_scaffold(_request(monkeypatch, opportunity_id="op_x"))
    assert payload["document"]  # render unaffected by the CRM hiccup
    assert payload["asset_type"] == "proposal_pack"
