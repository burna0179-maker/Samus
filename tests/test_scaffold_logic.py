"""Tests for backend.scaffold.logic."""

from __future__ import annotations


def _reset_idempotency(monkeypatch):
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    fresh = IdempotencyStore()
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    import backend.scaffold.logic as logic_mod

    monkeypatch.setattr(logic_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    return fresh


def test_build_positioning_defaults():
    from backend.scaffold.logic import build_positioning

    pos = build_positioning({})
    assert pos["problem"] == "manual work and coordination drag"
    assert "automation-first" in pos["mechanism"]
    assert pos["outcome"]


def test_build_offer_headline():
    from backend.scaffold.logic import build_offer, build_positioning

    pos = build_positioning({"industry": "finance"})
    offer = build_offer(pos, "Automation Pilot")
    assert offer["headline"].startswith("Automation Pilot:")
    assert offer["price_anchor"] == "$500-$5,000 depending on scope"


def test_build_sequence_four_steps():
    from backend.scaffold.logic import build_offer, build_positioning, build_sequence

    pos = build_positioning({})
    offer = build_offer(pos, "Pilot")
    seq = build_sequence(offer, "direct")
    assert len(seq) == 4
    assert [s["step"] for s in seq] == [1, 2, 3, 4]
    assert all("message" in s for s in seq)


def test_generate_scaffold_caches(tmp_path, monkeypatch):
    fresh = _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_SCAFFOLD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.scaffold.logic import generate_scaffold
    from backend.scaffold.models import ScaffoldRequest

    req = ScaffoldRequest(
        asset_type="proposal_pack",
        title="Operations Pilot",
        client="Acme",
        brand_voice="direct",
        offer="Automation Pilot",
        goals=["reduce manual ops"],
        inputs={"industry": "finance"},
    )
    a = generate_scaffold(req)
    b = generate_scaffold(req)
    assert a == b
    assert "document" in a
    assert "Proposal Pack" in a["document"]
    assert fresh.exists("scaffold:proposal_pack:acme:operations pilot")


def _patch_settings(monkeypatch, **flags):
    import types
    import backend.scaffold.logic as logic_mod

    base = dict(
        taste_audit_enabled=False,
        taste_audit_block_on_fail=False,
    )
    base.update(flags)
    monkeypatch.setattr(logic_mod, "get_settings", lambda: types.SimpleNamespace(**base))


def test_taste_audit_dormant_by_default(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_SCAFFOLD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _patch_settings(monkeypatch)  # both flags off
    from backend.scaffold.logic import generate_scaffold
    from backend.scaffold.models import ScaffoldRequest

    req = ScaffoldRequest(
        asset_type="proposal_pack",
        title="Ops Pilot",
        client="Acme",
        brand_voice="direct",
        offer="Pilot",
        goals=[],
        inputs={},
    )
    out = generate_scaffold(req)
    assert "taste_audit" not in out  # dormant: nothing attached


def test_taste_audit_attaches_when_enabled(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_SCAFFOLD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _patch_settings(monkeypatch, taste_audit_enabled=True)
    from backend.scaffold.logic import generate_scaffold
    from backend.scaffold.models import ScaffoldRequest

    req = ScaffoldRequest(
        asset_type="proposal_pack",
        title="Ops Pilot",
        client="Acme",
        brand_voice="direct",
        offer="Pilot",
        goals=[],
        inputs={},
    )
    out = generate_scaffold(req)
    assert "taste_audit" in out
    assert out["taste_audit"]["enforced"] is False
    assert out["taste_audit"]["ship_blocked"] is False


def test_taste_audit_block_on_fail_flags_shipping(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_SCAFFOLD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _patch_settings(monkeypatch, taste_audit_enabled=True, taste_audit_block_on_fail=True)
    from backend.scaffold.logic import generate_scaffold
    from backend.scaffold.models import ScaffoldRequest

    # An em-dash in the title flows into the rendered document -> hard taste fail.
    req = ScaffoldRequest(
        asset_type="proposal_pack",
        title="Ops — Pilot",
        client="Acme",
        brand_voice="direct",
        offer="Pilot",
        goals=[],
        inputs={},
    )
    out = generate_scaffold(req)
    assert out["taste_audit"]["passed"] is False
    assert out["taste_audit"]["enforced"] is True
    assert out["taste_audit"]["ship_blocked"] is True
