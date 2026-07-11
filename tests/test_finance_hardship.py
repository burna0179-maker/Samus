"""Hardship loader."""
from __future__ import annotations


def test_missing_file_returns_empty_context(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_HARDSHIP_PATH", str(tmp_path / "x.yaml"))
    from backend.finance.hardship import load_context
    ctx = load_context()
    assert ctx.registry_loaded is False
    assert ctx.calfresh.approved is False
    assert ctx.banking_vehicles == []


def test_load_calfresh(tmp_path, monkeypatch):
    p = tmp_path / "h.yaml"
    p.write_text(
        "calfresh:\n"
        "  approved: true\n"
        "  case_number: '1234'\n"
        "  recurring_benefits_usd: 200.00\n"
        "  net_countable_income_monthly_usd: 0.00\n"
        "banking_vehicles: []\n"
        "other_evidence: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAMUS_HARDSHIP_PATH", str(p))
    from backend.finance.hardship import load_context
    ctx = load_context()
    assert ctx.registry_loaded is True
    assert ctx.calfresh.approved is True
    assert ctx.calfresh.case_number == "1234"
    assert ctx.calfresh.recurring_benefits_usd == 200


def test_load_banking_vehicle(tmp_path, monkeypatch):
    p = tmp_path / "h.yaml"
    p.write_text(
        "calfresh: {approved: false}\n"
        "banking_vehicles:\n"
        "  - id: v1\n"
        "    bank: 'Test Bank'\n"
        "    card_type: 'Visa Debit'\n"
        "    fdic_insured: true\n"
        "    status: active\n"
        "other_evidence: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAMUS_HARDSHIP_PATH", str(p))
    from backend.finance.hardship import load_context
    ctx = load_context()
    assert len(ctx.banking_vehicles) == 1
    v = ctx.banking_vehicles[0]
    assert v.bank == "Test Bank"
    assert v.status == "active"
    assert v.routing_number is None  # null fields tolerated
